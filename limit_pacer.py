#!/usr/bin/env python3
"""cc-limit-pacer: fewer compactions AND fewer lockouts for Claude Code.

  * Compaction: `autoCompactWindow` is raised so 1M-window sessions compact at ~830k instead of ~570k.
  * Pacer (SessionStart + UserPromptSubmit hook): estimates your 5-hour and weekly usage pace. While "hot"
    (some window >= 50% used and on pace to run out) it
      - starts NEW sessions one model tier down (Fable -> Opus, Opus -> Sonnet) with the advisor off, and
      - holds automated runs (SDK / `claude -p`, or sessions working in a temp dir) until the window resets.
    When usage cools down it restores your model/advisor settings.
    While the week has room to spare (projected to end below `spare_below`% of the limit) new sessions start on
    `spare_model` (Fable), so allowance that would go unused at reset buys the best model instead.
  * Self-calibrating: every lockout Claude Code writes into a transcript is a 100% reading, and an estimate past
    100% while you are still working proves the budget is higher; both retune the budgets with no /usage input.
  * Usage comes from the statusline's `rate_limits` when fresh (terminal CLI), else from your local transcripts
    priced at API rates (per model, incl. advisor) divided by a budget learned with `calibrate`.

Stdlib only. `python3 limit_pacer.py --help`.
"""
import argparse, collections, datetime as dt, glob, json, os, shutil, sys, time

CLAUDE = os.environ.get('CLAUDE_HOME', os.path.expanduser('~/.claude'))
STATE = os.path.join(CLAUDE, 'state', 'cc-limit-pacer')
_OLD = os.path.join(CLAUDE, 'state', 'autocompact-gate')          # this plugin's name before 0.5.0
if os.path.isdir(_OLD) and not os.path.exists(STATE): os.rename(_OLD, STATE)
SETTINGS = os.path.join(CLAUDE, 'settings.json')
RL_FILE = os.path.join(CLAUDE, 'state', 'rate_limits.json')   # written by the statusline one-liner
HOOK_PATH = os.path.join(CLAUDE, 'hooks', 'limit_pacer.py')
W5, W7 = 5 * 3600, 7 * 86400
CFG_DEFAULTS = {'verbose': False, 'levers': True, 'hold_batch': True, 'use_statusline': True, 'mode': 'enforce', 'floor': 500_000, 'hard': 800_000, 'near_reset_s': 1200, 'min_used_pct': 50, 'keep_below_pace': 1.0,
                'spare_model': 'fable[1m]', 'spare_below': 90}
FIRE_EARLY = 32_000   # Claude Code compacts ~32k below autoCompactWindow (measured at 100k and 600k windows)
RL_FRESH_S = 15 * 60

# ---------------------------------------------------------------- small state helpers

def _p(name): return os.path.join(STATE, name)

def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except (OSError, ValueError):
        return default

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'                      # parallel sessions run this hook concurrently
    with open(tmp, 'w') as f: json.dump(obj, f, indent=1)
    os.replace(tmp, path)

def config(): return {**CFG_DEFAULTS, **load_json(_p('config.json'), {})}

# ---------------------------------------------------------------- pricing + transcript scan

# $/MTok (input, output, cache read), from the Claude API pricing table (2026-09-25). Cache writes: 1.25x (5m) / 2x (1h).
# Weighting by model matters: mid-Sep Fable weeks hit the same limit at ~40% of the tokens Opus 5.5 weeks do.
PRICES = [('claude-fable-5-1', 10, 50, 0.25), ('claude-fable', 10, 50, 1.0), ('claude-mythos', 10, 50, 0.25),
          ('claude-opus-5-5', 4, 20, 0.20), ('claude-opus', 5, 25, 0.5), ('claude-sonnet-5-5', 2, 10, 0.20),
          ('claude-sonnet', 2, 10, 0.2), ('claude-haiku', 1, 5, 0.1)]

def rates(model):
    """(input, output, cache_read) $/token for a model id; unknown -> Opus 5.5."""
    for prefix, i, o, cr in PRICES:
        if (model or '').startswith(prefix): return i / 1e6, o / 1e6, cr / 1e6
    return 4 / 1e6, 20 / 1e6, 0.2 / 1e6

def price(u, model=None):
    """API-equivalent $ cost of one call."""
    i, o, cr = rates(model)
    cc = u.get('cache_creation') or {}
    w1h = cc.get('ephemeral_1h_input_tokens'); w5m = cc.get('ephemeral_5m_input_tokens')
    cw = 2.0 * w1h + 1.25 * w5m if w1h is not None and w5m is not None else 2.0 * u.get('cache_creation_input_tokens', 0)
    return i * (u.get('input_tokens', 0) + cw) + cr * u.get('cache_read_input_tokens', 0) + o * u.get('output_tokens', 0)

def advisor_price(u):
    """Advisor consults ride along as `advisor_message` iterations and are NOT in the top-level usage (~11% extra)."""
    return sum(price(x, x.get('model')) for x in (u.get('iterations') or []) if x.get('type') == 'advisor_message')

def _ts(s): return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()

def scan(days=8, now=None):
    """[(ts, cost)] for every API call in the last `days`, deduped by requestId.
    Incremental: remembers a byte offset per transcript, so repeat calls only read appended lines."""
    now = now or time.time()
    cut = now - days * 86400
    cache = load_json(_p('scan_cache_v3.json'), {})   # v3: per-model $ + advisor
    out, seen, fresh, locks = [], set(), {}, []
    for f in glob.glob(os.path.join(CLAUDE, 'projects', '**', '*.jsonl'), recursive=True):
        try: st = os.stat(f)
        except OSError: continue
        if st.st_mtime < cut: continue
        ent = cache.get(f)
        if not ent or ent['size'] > st.st_size: ent = {'off': 0, 'size': 0, 'calls': []}
        if st.st_size > ent['off']:
            with open(f, 'rb') as fh:
                fh.seek(ent['off']); chunk = fh.read()
            end = chunk.rfind(b'\n') + 1                      # only whole lines; a half-written line waits
            for line in chunk[:end].decode(errors='ignore').splitlines():
                if 'hit your' in line and '"isApiErrorMessage":true' in line: locks.append(line); continue
                if '"usage"' not in line: continue
                try: d = json.loads(line)
                except ValueError: continue
                m = d.get('message') or {}; u = m.get('usage')
                if d.get('type') != 'assistant' or not u or not d.get('timestamp'): continue
                ent['calls'].append([_ts(d['timestamp']), price(u, m.get('model')) + advisor_price(u), d.get('requestId') or m.get('id')])
            ent['off'] += end; ent['size'] = st.st_size
        ent['calls'] = [c for c in ent['calls'] if c[0] >= cut]
        fresh[f] = ent
        for ts, c, rid in ent['calls']:
            if rid in seen: continue
            seen.add(rid); out.append((ts, c))
    save_json(_p('scan_cache_v3.json'), fresh)
    out.sort()
    if locks: learn_from_lockouts(locks, out)
    return out

# ---------------------------------------------------------------- windows + usage estimate

def five_hour_start(calls, now, anchor=None):
    """Claude's 5h window opens on the first message after the previous window closed.
    `anchor` = a known reset time; walking from it keeps us in phase with the real windows."""
    start = anchor - W5 if anchor and anchor - W5 <= now else None
    for ts, _ in calls:
        if start is not None and ts < start: continue
        if ts > now: break
        if start is None or ts >= start + W5: start = ts
    return start if start is not None and now < start + W5 else None

def week_start(now, anchor):
    """Weekly window start given any past/future reset epoch (`anchor`); rolling 7d if unknown."""
    if not anchor: return now - W7
    return anchor - ((anchor - now) // W7 + 1) * W7 if anchor > now else anchor + ((now - anchor) // W7) * W7

def spend(calls, start, now): return sum(c for ts, c in calls if start <= ts <= now)

def statusline_rl(now):
    st = load_json(RL_FILE, {})
    rl = st.get('rate_limits') or {}
    return rl if rl and now - st.get('t', 0) < RL_FRESH_S else None

def calibrate_from(rl, calls, now):
    """Learn budget B per window = local spend in window / used%. Keeps an EMA so one odd reading doesn't swing it."""
    cal = load_json(_p('calibration.json'), {})
    for key, span in (('five_hour', W5), ('seven_day', W7)):
        r = rl.get(key) or {}
        pct, reset = r.get('used_percentage'), r.get('resets_at')
        if not reset or pct is None: continue
        cal['week_anchor' if key == 'seven_day' else 'five_anchor'] = reset
        if pct < 5: continue                                   # too little signal to learn a budget from
        local = spend(calls, reset - span - (600 if key == "five_hour" else 0), now)  # 5h resets are rounded up to 10 min
        if local <= 0: continue
        b = local / (pct / 100)
        old = cal.get(key)
        cal[key] = b if not old else 0.7 * old + 0.3 * b
    cal['t'] = now
    save_json(_p('calibration.json'), cal)
    return cal

def learn_from_lockouts(lines, calls):
    """A lockout is an exact reading: 100% of that window's budget was spent by the time it hit."""
    from backtest import LOCK_RE, parse_reset                 # same repo
    cal = load_json(_p('calibration.json'), {})
    seen = cal.setdefault('seen_locks', [])
    for line in lines:
        try: d = json.loads(line); m = LOCK_RE.search(json.dumps(d.get('message'), ensure_ascii=False))
        except ValueError: continue
        if not m: continue
        t = _ts(d['timestamp']); kind = '5h' if m[1] == 'session' else 'week'
        reset = parse_reset(t, m[2], m[3], m[4], m[5], m[6])
        tag = f'{kind}:{round(reset / 600)}'                  # every blocked session logs the same lockout
        if tag in seen: continue
        seen.append(tag)
        key, span = ('five_hour', W5) if kind == '5h' else ('seven_day', W7)
        cal['five_anchor' if kind == '5h' else 'week_anchor'] = reset
        if t < cal.get('week_locked_until', 0): continue      # hit inside our own weekly lockout: another account
        if kind == 'week': cal['week_locked_until'] = reset
        b, old = spend(calls, reset - span - (600 if kind == '5h' else 0), t), cal.get(key)
        # ponytail: a 2x jump is read as another account sharing ~/.claude, not a real budget change; a learned
        # per-account budget would need the account id, which transcripts don't carry.
        if b <= 0 or (old and not 0.5 <= b / old <= 2): continue
        cal[key] = b if not old else 0.5 * old + 0.5 * b
        cal.setdefault('learned', []).append({'t': t, 'from': 'lockout', 'key': key, 'budget': round(cal[key], 2)})
    cal['seen_locks'] = seen[-50:]; cal['learned'] = cal.get('learned', [])[-50:]
    save_json(_p('calibration.json'), cal)

def raise_on_overrun(calls, now, cal, rl):
    """Estimate past 100% while calls are still going through: the limit wasn't hit, so the budget is at least the
    window's spend. Raises budgets that are too low (false 'hot', predicted lockouts that never happen)."""
    if not any(now - ts < 600 for ts, _ in calls[-5:]): return rl
    changed = False
    for key in ('five_hour', 'seven_day'):
        r = rl.get(key)
        if r and r['used_percentage'] > 100:
            cal[key] *= r['used_percentage'] / 100; r['used_percentage'] = 100.0; changed = True
            cal.setdefault('learned', []).append({'t': now, 'from': 'overrun', 'key': key, 'budget': round(cal[key], 2)})
    if changed: cal['learned'] = cal['learned'][-50:]; save_json(_p('calibration.json'), cal)
    return rl

def estimate(calls, now, cal):
    """rate_limits-shaped estimate from local transcripts + calibrated budgets."""
    rl = {}
    s5 = five_hour_start(calls, now, cal.get('five_anchor'))
    if cal.get('five_hour'):
        rl['five_hour'] = ({'used_percentage': 100 * spend(calls, s5 - 600, now) / cal['five_hour'], 'resets_at': s5 + W5}  # same 10-min slack as calibrate_from
                           if s5 else {'used_percentage': 0, 'resets_at': now + W5})
    if cal.get('seven_day'):
        s7 = week_start(now, cal.get('week_anchor'))
        rl['seven_day'] = {'used_percentage': 100 * spend(calls, s7, now) / cal['seven_day'], 'resets_at': s7 + W7}
    return rl

def usage(now=None):
    """(rate_limits, source). Statusline when fresh (and it recalibrates us), else local estimate."""
    now = now or time.time()
    calls = scan(now=now)
    rl = statusline_rl(now) if config()['use_statusline'] else None   # off when several accounts share this ~/.claude
    if rl:
        calibrate_from(rl, calls, now)
        return rl, 'statusline'
    cal = load_json(_p('calibration.json'), {})
    rl = raise_on_overrun(calls, now, cal, estimate(calls, now, cal))
    return (rl, 'local-estimate') if rl else ({}, 'uncalibrated')

BATCH_DIRS = ('/private/tmp/', '/tmp/', '/private/var/folders/', '/var/folders/')

def is_batch(entrypoint, cwd):
    """Automated work we may hold when hot: SDK / `claude -p` runs, or sessions working in a temp/scratch dir
    (fan-outs and eval harnesses: a single fan-out of a few thousand such sessions can trigger a lockout)."""
    return str(entrypoint or '').startswith('sdk') or str(cwd or '').startswith(BATCH_DIRS)

# ---------------------------------------------------------------- decision

def paces(rl, now):
    out = {}
    for key, span in (('five_hour', W5), ('seven_day', W7)):
        r = rl.get(key) or {}
        if r.get('used_percentage') is None or not r.get('resets_at'): continue
        left = r['resets_at'] - now
        elapsed = min(max(1 - left / span, 0.02), 1)          # floor avoids divide-by-~0 right after a reset
        out[key] = (r['used_percentage'] / 100 / elapsed, left)
    return out

def decide(ctx, rl, now, cfg=CFG_DEFAULTS):
    if ctx >= cfg['hard']: return True, 'near hard window'
    if ctx < cfg['floor'] - 30_000: return True, "below floor: this is the model's own window (e.g. a 200k model), never block"
    if not rl: return True, 'no usage data: compact at floor'
    over = {k: round(p, 2) for k, (p, left) in paces(rl, now).items()
            if p >= cfg['keep_below_pace'] and left > cfg['near_reset_s'] and rl[k]['used_percentage'] >= cfg['min_used_pct']}  # early-window bursts aren't a trend
    return (True, f'over pace {over}') if over else (False, 'under pace: keep full context')

# ---------------------------------------------------------------- pacer: levers that switch on only while hot

ENTER = (1.0, 50)   # hot when some window has pace >= 1.0 and used >= 50%
EXIT = (0.9, 45)    # ...and stays hot until no window has pace >= 0.9 and used >= 45% (hysteresis: no flapping)

def is_hot(rl, now, was_hot):
    p_min, used_min = EXIT if was_hot else ENTER
    return any(p >= p_min and rl[k]['used_percentage'] >= used_min for k, (p, _) in paces(rl, now).items())

def is_spare(rl, now, was_spare, below=90):
    """The week is on track to end with allowance left over: projected end-of-week use < `below`% (exit at +5).
    Needs a quarter of the week behind it, so a quiet Monday doesn't read as a spare week."""
    r = rl.get('seven_day') or {}
    if r.get('used_percentage') is None or not r.get('resets_at'): return False
    elapsed = 1 - (r['resets_at'] - now) / W7
    return elapsed >= 0.25 and r['used_percentage'] / elapsed < below + (5 if was_spare else 0)

def step_down(model):
    """One tier cheaper for NEW sessions (a running session keeps its model -- verified)."""
    m = model or ''
    if 'fable' in m or 'mythos' in m: return 'opus[1m]'
    if m.startswith(('opus', 'claude-opus')) or not m: return 'sonnet[1m]'
    return model                                       # sonnet / haiku: leave alone

def baseline():
    """model/advisor before install: recorded by install, else the oldest settings backup (installs before 0.6)."""
    b = load_json(_p('install.json'), {}).get('baseline')
    if b: return b
    baks = sorted(glob.glob(_p('settings.bak-*')))
    return {k: load_json(baks[0], {}).get(k) for k in ('model', 'advisorModel')} if baks else None

def hot_levers(s): return {'model': step_down(s.get('model')), 'advisorModel': 'off'}

def apply_levers(mode, st, s, baseline=None, spare_model='fable[1m]'):
    """Write (or restore) settings `model` / `advisorModel` in `s`; new sessions read them at start.
    mode: 'hot' (step down, advisor off), 'spare' (spare_model), or None (restore). True/False = 'hot'/None.
    Restores only keys still holding the value we wrote, so a user's own /model choice wins.
    `baseline` (the model/advisor at install) undoes a hot signature left behind without our state.
    Returns a list of changes for the log. Caller holds the state lock."""
    mode = {True: 'hot', False: None}.get(mode, mode)
    changes = []
    if st.get('wrote') and st.get('mode', 'hot') != mode:
        for k, v in st['wrote'].items():
            if s.get(k) == v:
                back = st['saved'].get(k)
                changes.append({'key': k, 'from': v, 'to': back})
                if back is None: s.pop(k, None)
                else: s[k] = back
            else:
                changes.append({'key': k, 'skipped': 'user changed it while hot', 'now': s.get(k)})
        st.pop('wrote'); st.pop('saved', None); st.pop('mode', None)
    elif not st.get('wrote') and not mode and baseline and s.get('advisorModel') == 'off' != baseline.get('advisorModel') \
            and s.get('model') == step_down(baseline.get('model')) != baseline.get('model'):
        for k in ('model', 'advisorModel'):                   # orphaned step-down (state lost): back to the baseline
            changes.append({'key': k, 'from': s.get(k), 'to': baseline.get(k), 'why': 'orphaned step-down'})
            if baseline.get(k) is None: s.pop(k, None)
            else: s[k] = baseline[k]
    if mode and not st.get('wrote'):
        wrote = hot_levers(s) if mode == 'hot' else {'model': spare_model}
        st.update(saved={k: s.get(k) for k in wrote}, wrote=wrote, mode=mode)
        for k, v in wrote.items():
            if s.get(k) != v: changes.append({'key': k, 'from': s.get(k), 'to': v})
        s.update(wrote)
    return changes

VERSION = load_json(os.path.join(os.path.dirname(os.path.realpath(__file__)), '.claude-plugin', 'plugin.json'), {}).get('version', 'dev')
LOG_MAX = 5_000_000

def log(name, rec):
    """Append one JSON line; rotate to <name>.1 past LOG_MAX so a day of heavy use can't fill the disk."""
    path = _p(name)
    try:
        if os.path.getsize(path) > LOG_MAX: os.replace(path, path + '.1')
    except OSError:
        pass
    with open(path, 'a') as f: f.write(json.dumps(rec, default=str) + '\n')

def pace_hook():
    """SessionStart / UserPromptSubmit hook. Never raises: any bug is logged to errors.jsonl and the prompt goes through."""
    t0 = time.perf_counter(); ev = {}
    try:
        ev = json.load(sys.stdin)
        out = _pace(ev, t0)
        if out: print(json.dumps(out))
    except Exception as e:
        import traceback
        os.makedirs(STATE, exist_ok=True)
        log('errors.jsonl', {'t': time.time(), 'v': VERSION, 'event': ev.get('hook_event_name'), 'session': ev.get('session_id'),
                             'error': repr(e), 'traceback': traceback.format_exc()})

def _pace(ev, t0):
    import fcntl
    now, cfg = time.time(), config()
    try:
        rl, source = usage(now)
    except Exception as e:                            # a sensor bug must never wedge a session: fail open
        import traceback
        log('errors.jsonl', {'t': now, 'v': VERSION, 'where': 'usage', 'error': repr(e), 'traceback': traceback.format_exc()})
        rl, source = {}, f'error: {e!r}'
    os.makedirs(STATE, exist_ok=True)
    with open(_p('settings.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)              # state + settings are read-modify-write across concurrent sessions
        st = load_json(_p('pacer.json'), {})
        was = st.get('hot', False)
        hot = bool(rl) and is_hot(rl, now, was)
        if os.environ.get('LIMIT_PACER_FORCE') == 'hot': hot = True   # live tests
        was_spare = st.get('spare', False)
        spare = bool(rl) and not hot and bool(cfg.get('spare_model')) and is_spare(rl, now, was_spare, cfg['spare_below'])
        changes = []
        if cfg.get('levers', True):
            s = load_json(SETTINGS, {})
            changes = apply_levers('hot' if hot else 'spare' if spare else None, st, s,
                                   baseline(), cfg.get('spare_model'))
            if any('to' in c for c in changes): save_json(SETTINGS, s)
        st['hot'] = hot; st['spare'] = spare; st['t'] = now
        save_json(_p('pacer.json'), st)
    pc = {k: f"{rl[k]['used_percentage']:.0f}% (pace {p:.1f})" for k, (p, _) in paces(rl, now).items()}
    held = (hot and ev.get('hook_event_name') == 'UserPromptSubmit' and cfg.get('hold_batch', True)
            and is_batch(os.environ.get('CLAUDE_CODE_ENTRYPOINT'), ev.get('cwd'))
            and not os.environ.get('LIMIT_PACER_ALLOW')
            and not str(ev.get('prompt', '')).lstrip().startswith('/cc-limit-pacer'))   # never hold our own commands
    # Quiet by default: only runs where something happened. Verbose (config `verbose` or LIMIT_PACER_VERBOSE=1)
    # logs every run with the full picture, for debugging or a trial week.
    verbose = cfg.get('verbose') or os.environ.get('LIMIT_PACER_VERBOSE') == '1'
    rec = {'t': now, 'v': VERSION, 'event': ev.get('hook_event_name'), 'hot': hot, 'held': held,
           'ms': round((time.perf_counter() - t0) * 1000)}
    if hot != was: rec['transition'] = 'hot' if hot else 'cool'
    elif spare != was_spare: rec['transition'] = 'spare' if spare else 'cool'
    if changes: rec['settings'] = changes
    if held: rec['cwd'] = ev.get('cwd'); rec['entry'] = os.environ.get('CLAUDE_CODE_ENTRYPOINT')
    if source.startswith('error'): rec['source'] = source
    if verbose:
        cal = load_json(_p('calibration.json'), {})
        rec.update({'session': ev.get('session_id'), 'entry': os.environ.get('CLAUDE_CODE_ENTRYPOINT'), 'cwd': ev.get('cwd'),
                    'source': source, 'usage': pc, 'pct': {k: round(v['used_percentage'], 1) for k, v in rl.items()},
                    'resets_in_h': {k: round((v['resets_at'] - now) / 3600, 2) for k, v in rl.items() if v.get('resets_at')},
                    'cal_age_h': round((now - cal['t']) / 3600, 1) if cal.get('t') else None,
                    'budgets': {k: round(cal[k], 2) for k in ('five_hour', 'seven_day') if k in cal}})
    if verbose or len(rec) > 6:                       # 6 = the always-present fields; more means something happened
        log('pacer.jsonl', rec)
    out, msgs = {}, []
    if hot != was:
        msgs.append(f"cc-limit-pacer: {'running hot' if hot else 'back under pace'} — {pc}. "
                    + ('New sessions start one model tier down with the advisor off; batch runs are held.'
                       if hot else 'Model and advisor settings restored.'))
    elif spare != was_spare:
        msgs.append(f"cc-limit-pacer: {'the week has allowance to spare' if spare else 'spare-week mode off'} — {pc}. "
                    + (f"New sessions start on {cfg['spare_model']}." if spare else 'Model setting restored.'))
    if held:
        hot_resets = [rl[k]['resets_at'] for k, (p, _) in paces(rl, now).items() if p >= EXIT[0]]
        resets = min(hot_resets or [v['resets_at'] for v in rl.values() if v.get('resets_at')] or [now])
        out = {'decision': 'block', 'reason': f"cc-limit-pacer: holding automated run — usage {pc}; "
               f"retry after {dt.datetime.fromtimestamp(resets):%a %H:%M} or set LIMIT_PACER_ALLOW=1"}
    if msgs: out['systemMessage'] = ' '.join(msgs)
    return out

# ---------------------------------------------------------------- CLI

STATUSLINE_SNIPPET = ('mkdir -p ~/.claude/state && echo "$input" | jq -c \'{t: now, rate_limits}\' '
                      '> ~/.claude/state/rate_limits.json')
EVENTS = ('SessionStart', 'UserPromptSubmit')
COMPACT_AT = 830_000                                  # tokens; fewer compactions on 1M-window models

def _strip_ours(hooks):
    for e in list(hooks):
        keep = [h for h in hooks[e] if 'limit_pacer' not in json.dumps(h) and 'compact_gate.py' not in json.dumps(h)]
        if keep: hooks[e] = keep
        else: hooks.pop(e)

def cmd_install(a):
    s = load_json(SETTINGS, {})
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(SETTINGS): shutil.copy(SETTINGS, _p(f'settings.bak-{int(time.time())}'))
    prev = load_json(_p('install.json'), {})
    save_json(_p('install.json'), {'prev_autoCompactWindow': prev.get('prev_autoCompactWindow', s.get('autoCompactWindow')),
                                   't': prev.get('t', time.time()),
                                   'baseline': prev.get('baseline') or {k: s.get(k) for k in ('model', 'advisorModel')}})
    save_json(_p('config.json'), config())
    s['autoCompactWindow'] = a.compact_at + FIRE_EARLY
    hooks = s.setdefault('hooks', {}); _strip_ours(hooks)
    if a.plugin:                                       # the plugin ships the hooks; settings only need the window
        if not hooks: s.pop('hooks')
        where = 'hooks provided by the plugin'
    else:
        os.makedirs(os.path.dirname(HOOK_PATH), exist_ok=True)
        here = os.path.dirname(os.path.realpath(__file__))
        for f in ('limit_pacer.py', 'backtest.py'):      # status imports backtest
            shutil.copy(os.path.join(here, f), os.path.join(os.path.dirname(HOOK_PATH), f))
        os.chmod(HOOK_PATH, 0o755)
        for e in EVENTS:
            hooks.setdefault(e, []).append({'hooks': [{'type': 'command', 'command': f'python3 {HOOK_PATH} pace'}]})
        where = f'pacer on {", ".join(EVENTS)}; hook={HOOK_PATH}'
    save_json(SETTINGS, s)
    print(f"installed: compaction at ~{a.compact_at // 1000}k (autoCompactWindow={s['autoCompactWindow']}); {where}")
    sl = (s.get('statusLine') or {}).get('command', '')
    sl_path = os.path.expanduser(sl.split()[0]) if sl else ''
    if not (sl_path and os.path.exists(sl_path) and 'rate_limits.json' in open(sl_path).read()):
        print('\noptional (terminal CLI only): add this line to your statusline script for exact usage readings:\n  '
              + STATUSLINE_SNIPPET)
    if not load_json(_p('calibration.json'), {}).get('seven_day'):
        print('\nnext: calibrate once from /usage (or the app usage card): '
              + ('/cc-limit-pacer:calibrate 30 23 "2026-10-09 11:00"' if a.plugin else
                 f'python3 {HOOK_PATH} calibrate --five-hour 30 --weekly 23 --weekly-reset "2026-10-09 11:00"'))

def cmd_uninstall(a):
    st = load_json(_p('pacer.json'), {})
    if st.get('wrote'):
        s0 = load_json(SETTINGS, {}); apply_levers(False, st, s0); save_json(SETTINGS, s0)
        save_json(_p('pacer.json'), {**st, 'hot': False})
    s = load_json(SETTINGS, {})
    _strip_ours(s.get('hooks', {}))
    prev = load_json(_p('install.json'), {}).get('prev_autoCompactWindow')
    if prev: s['autoCompactWindow'] = prev
    else: s.pop('autoCompactWindow', None)
    save_json(SETTINGS, s)
    print(f'uninstalled; autoCompactWindow restored to {prev or "default"}; model/advisor restored if we had changed them')

def _parse_local(s):
    return dt.datetime.strptime(s, '%Y-%m-%d %H:%M').astimezone().timestamp()

def cmd_calibrate(a):
    now = time.time()
    rl = {'five_hour': {'used_percentage': a.five_hour, 'resets_at': _parse_local(a.five_hour_reset) if a.five_hour_reset
                        else (five_hour_start(scan(now=now), now) or now) + W5},
          'seven_day': {'used_percentage': a.weekly, 'resets_at': _parse_local(a.weekly_reset)}}
    cal = calibrate_from(rl, scan(now=now), now)
    print('budgets (API-equivalent $):', {k: round(cal[k], 2) for k in ('five_hour', 'seven_day') if k in cal})
    est = estimate(scan(now=now), now, cal)
    print('local estimate now:', {k: f"{v['used_percentage']:.0f}%" for k, v in est.items()}, '(should match what you entered)')

def cmd_status(a):
    now = time.time(); st = load_json(_p('pacer.json'), {})
    rl, source = usage(now)
    print(f"usage-source={source}  state={'HOT' if st.get('hot') else 'cool'}"
          + (f"  levers: {st['wrote']}" if st.get('wrote') else ''))
    for k, (p, left) in paces(rl, now).items():
        print(f"  {k:9} used={rl[k]['used_percentage']:5.1f}%  pace={p:4.2f}  resets in {left / 3600:5.1f}h")
    log = [json.loads(l) for l in open(_p('pacer.jsonl'))] if os.path.exists(_p('pacer.jsonl')) else []
    if log:
        print(f"{len(log)} hook runs; hot on {sum(r['hot'] for r in log)}; held {sum(r['held'] for r in log)} automated prompts")
    from backtest import find_lockouts                      # same repo
    since = load_json(_p('install.json'), {}).get('t')
    if since:
        lk = find_lockouts(days=60)
        before = [x for x in lk if since - 30 * 86400 <= x['t'] < since]
        after = [x for x in lk if x['t'] >= since]
        days = max((now - since) / 86400, 1e-9)
        print(f"lockouts: {len(before)} in the 30d before install ({len(before) / 30 * 7:.1f}/wk); "
              f"{len(after)} since install ({len(after) / days * 7:.1f}/wk over {days:.1f}d)")

def cmd_verbose(a):
    cfg = config()
    if a.state: cfg['verbose'] = a.state == 'on'; save_json(_p('config.json'), cfg)
    print(f"verbose logging is {'on' if cfg['verbose'] else 'off'}  ({_p('pacer.jsonl')})")

def cmd_report(a):
    """Bug checklist over the last N hours of hook logs. Exit 1 if anything needs a look."""
    now = time.time(); since = now - a.hours * 3600
    def rows(name):
        out = []
        for n in (name + '.1', name):
            if os.path.exists(_p(n)):
                for l in open(_p(n)):
                    try: r = json.loads(l)
                    except ValueError: continue
                    if r.get('t', 0) >= since: out.append(r)
        return sorted(out, key=lambda r: r['t'])
    runs, errs = rows('pacer.jsonl'), rows('errors.jsonl')
    when = lambda t: dt.datetime.fromtimestamp(t).strftime('%a %H:%M')
    problems = []
    verbose_data = any('source' in r and 'usage' in r for r in runs)
    print(f"cc-limit-pacer {VERSION} — last {a.hours:g}h: {len(runs)} logged hook runs, {len(errs)} errors"
          + ('' if verbose_data else '  (verbose off: only eventful runs are logged; `verbose on` for latency, sensor and coverage checks)'))
    if runs:
        ms = sorted(r['ms'] for r in runs if 'ms' in r) or [0]
        p95 = ms[min(int(len(ms) * .95), len(ms) - 1)]
        print(f"  latency  p50={ms[len(ms) // 2]}ms  p95={p95}ms  max={ms[-1]}ms  ({len(ms)} timed runs)")
        if p95 > 1000: problems.append(f'slow hook: p95 {p95}ms (every prompt waits on it)')
        src = collections.Counter(r['source'].split(':')[0] for r in runs if 'source' in r)
        if src: print('  usage source', dict(src))
        if src.get('error'): problems.append(f"usage sensor failed on {src['error']} runs")
        if src.get('uncalibrated'): problems.append('uncalibrated: the pacer can never run hot — run calibrate')
        age = runs[-1].get('cal_age_h')
        if age and age > 72: problems.append(f'calibration is {age:.0f}h old — recalibrate from /usage')
        last = runs[-1]
        print(f"  now  {'HOT' if last['hot'] else 'cool'}  {last.get('usage', '')}")
        for r in runs:
            if r.get('transition'): print(f"  {when(r['t'])}  -> {r['transition']}  {r.get('usage')}")
            for c in r.get('settings', []):
                print(f"  {when(r['t'])}  settings {c}")
                if c.get('skipped'): problems.append(f"restore skipped for {c['key']} ({c['skipped']}): check settings.json")
        held = [r for r in runs if r.get('held')]
        if held:
            print(f"  held {len(held)} automated prompts, e.g. {sorted({str(r.get('cwd'))[:60] for r in held})[:3]}")
            inter = [r for r in held if not is_batch(r.get('entry'), r.get('cwd'))]
            if inter: problems.append(f'{len(inter)} held prompts were NOT batch work (bug in is_batch)')
        if verbose_data and not any(r.get('event') == 'UserPromptSubmit' for r in runs):
            problems.append('no UserPromptSubmit runs logged: are the plugin hooks loaded? (restart Claude Code)')
    for e in errs[-3:]:
        print(f"  ERROR {when(e['t'])} {e.get('where', e.get('event'))}: {e['error']}")
    if errs: problems.append(f'{len(errs)} hook errors — see {_p("errors.jsonl")}')
    from backtest import find_lockouts                      # same repo
    for x in [x for x in find_lockouts(days=max(a.hours / 24, 1)) if x['t'] >= since]:
        key = 'five_hour' if x['kind'] == '5h' else 'seven_day'
        before = [r for r in runs if x['t'] - 3600 <= r['t'] <= x['t']]
        est = max((r['pct'].get(key, 0) for r in before if 'pct' in r), default=None)
        prior = [r for r in runs if r['t'] <= x['t']]
        was_hot = any(r['hot'] for r in before) or bool(prior and prior[-1]['hot'])   # last known state works in quiet mode too
        print(f"  LOCKOUT {when(x['t'])} {x['kind']}: pacer {'was hot' if was_hot else 'was COOL'}; "
              f"estimated {key} usage before it: {str(est) + '%' if est is not None else 'n/a (verbose off)'}")
        if not was_hot: problems.append(f"lockout at {when(x['t'])} with the pacer cool (missed it)")
        if est is not None and est < 80: problems.append(f"sensor read {est}% just before a {x['kind']} lockout — budget is off, recalibrate")
    print('\nOK — nothing to look at' if not problems else '\nNEEDS A LOOK:\n' + '\n'.join(f'  - {p}' for p in problems))
    return 1 if problems else 0

def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'pace': return pace_hook()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    i = sp.add_parser('install')
    i.add_argument('--compact-at', type=int, default=COMPACT_AT, help='auto-compact when context reaches this many tokens')
    i.add_argument('--plugin', action='store_true', help='settings only; the Claude Code plugin provides the hooks')
    sp.add_parser('uninstall')
    c = sp.add_parser('calibrate', help='teach it your budget from one /usage reading')
    c.add_argument('--five-hour', type=float, required=True, help='5-hour limit %% used')
    c.add_argument('--weekly', type=float, required=True, help='weekly limit %% used')
    c.add_argument('--weekly-reset', required=True, help='local time the week resets, "YYYY-MM-DD HH:MM"')
    c.add_argument('--five-hour-reset', help='local time the 5h window resets (default: inferred from transcripts)')
    sp.add_parser('status')
    v = sp.add_parser('verbose', help='log every hook run in full (on) or only eventful runs (off, default)'); v.add_argument('state', nargs='?', choices=['on', 'off'])
    r = sp.add_parser('report', help='bug checklist over recent hook logs'); r.add_argument('--hours', type=float, default=24)
    a = ap.parse_args()
    sys.exit({'install': cmd_install, 'uninstall': cmd_uninstall, 'calibrate': cmd_calibrate, 'status': cmd_status,
              'report': cmd_report, 'verbose': cmd_verbose}[a.cmd](a) or 0)

if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    main()
