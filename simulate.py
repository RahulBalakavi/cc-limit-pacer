#!/usr/bin/env python3
"""Counterfactual month: replay ALL your sessions under three policies and count compactions + lockouts.

    python3 simulate.py [--days 30] [--floor 500000]

  today         what really happened (your current autocompact setting)
  compact@830k  compact later on 1M-window models, nothing else
  830k + pacer  this repo: compact@830k + while hot, new sessions one tier down / advisor off / batch runs held

Budgets come from your calibration (`limit_pacer.py calibrate`). A lockout is any 5h / weekly window whose
simulated usage crosses the budget; usage after that point is dropped until the window resets.
Run first with the `actual` row: if it roughly reproduces your real lockouts, the budgets are credible.
"""
import argparse, collections, glob, json, os, time
import limit_pacer as g
import backtest as b

H, HARD, SUMMARY, REREAD = 3600, 800_000, 15_000, 20_000
HEADLESS = set()   # automated sessions (g.is_batch: SDK / claude -p, or working in a temp dir), filled by load()

def load(days):
    """Chronological events: ('call', ts, session, (inp,cw,cr,out)) / ('side', ts, cost) / ('compact', ts, session, trigger, dropped)."""
    cut, seen, ev, headless = time.time() - days * 86400, set(), [], set()
    for f in glob.glob(os.path.join(g.CLAUDE, 'projects', '**', '*.jsonl'), recursive=True):
        if os.path.getmtime(f) < cut: continue
        sub = '/subagents/' in f
        for line in open(f, errors='ignore'):
            if '"compact_boundary"' in line:
                try: d = json.loads(line)
                except ValueError: continue
                m = d.get('compactMetadata') or {}
                ts = g._ts(d['timestamp'])
                if ts >= cut and not d.get('isSidechain') and not sub:
                    ev.append(('compact', ts, d.get('sessionId') or f, m.get('trigger'), m.get('preTokens') or 0, m.get('postTokens') or 0))
                continue
            if '"usage"' not in line: continue
            try: d = json.loads(line)
            except ValueError: continue
            msg = d.get('message') or {}; u = msg.get('usage'); rid = d.get('requestId') or msg.get('id')
            if d.get('type') != 'assistant' or not u or not rid or rid in seen or not d.get('timestamp'): continue
            seen.add(rid); ts = g._ts(d['timestamp'])
            if g.is_batch(d.get('entrypoint'), d.get('cwd')): headless.add(d.get('sessionId') or f)
            if ts < cut: continue
            c = (u.get('input_tokens', 0), u.get('cache_creation_input_tokens', 0), u.get('cache_read_input_tokens', 0), u.get('output_tokens', 0), msg.get('model'))
            ev.append(('side', ts, b.cost(*c), c[4]) if d.get('isSidechain') or sub else ('call', ts, d.get('sessionId') or f, c))
            adv = g.advisor_price(u)
            if adv: ev.append(('advisor', ts, d.get('sessionId') or f, adv))
    ev.sort(key=lambda e: (e[1], e[0] != 'compact'))   # a compaction sorts before the call at the same instant
    HEADLESS.clear(); HEADLESS.update(headless)
    return ev

def week_bounds(anchors, t0, t1):
    """Weekly window boundaries from known reset times, stepping back 7d from each until the previous anchor."""
    pts, prev = set(), t0 - 7 * 86400
    for a in sorted(anchors):
        x = a
        while x > prev: pts.add(x); x -= 7 * 86400
        prev = a
    x = max(anchors)
    while x < t1 + 7 * 86400: pts.add(x); x += 7 * 86400
    return sorted(pts)

def post_compact_size(ev):
    """Median context of the first call after a real auto-compaction: what a compaction actually leaves behind."""
    pending, sizes = set(), []
    for e in ev:
        if e[0] == 'compact' and e[3] == 'auto': pending.add(e[2])
        elif e[0] == 'call' and e[2] in pending:
            pending.discard(e[2]); sizes.append(sum(e[3][:3]))
    sizes.sort()
    return sizes[len(sizes) // 2] if sizes else 80_000

def ev_cost(e):
    return b.cost(*e[3]) if e[0] == 'call' else e[2] if e[0] == 'side' else e[3] if e[0] == 'advisor' else 0.0

def budget_points(ev, real, cal):
    """{'five_hour': [(t, $)], 'seven_day': [...]}: exact readings of each budget over time -- every own lockout
    (spend in its window up to the lockout), every refit the pacer logged, and the calibration now."""
    pts = {'five_hour': [], 'seven_day': []}
    for x in real:
        if x['other']: continue
        sp = sum(ev_cost(e) for e in ev if x['start'] <= e[1] <= x['t'])   # the replay's own window
        if sp > 0: pts['five_hour' if x['kind'] == '5h' else 'seven_day'].append((x['t'], sp))
    for r in cal.get('learned', []): pts[r['key']].append((r['t'], r['budget']))
    for k in pts:
        if cal.get(k): pts[k].append((cal.get('t', time.time()), cal[k]))
        pts[k].sort()
    return pts

def budget_fn(ev, real, pts, kind):
    """The real limit of the window opening at t, in hindsight: a window that locked used its own reading; one that
    didn't had a limit of at least what it spent, and otherwise the nearest reading in time. So the `actual` replay
    locks exactly where you really did, and the policies are compared on the limits you really had."""
    import bisect
    span = 5 * H if kind == '5h' else 7 * 86400
    ts = [e[1] for e in ev]; cum = [0.0]
    for e in ev: cum.append(cum[-1] + ev_cost(e))
    locks = [x for x in real if x['kind'] == kind and not x['other']]
    exact = {x['t']: v for x in locks for t, v in pts if t == x['t']}
    def f(t):
        own = next((x for x in locks if t <= x['t'] < t + span), None)
        if own and own['t'] in exact: return exact[own['t']]
        near = min(pts, key=lambda p: abs(p[0] - t))[1]
        spent = cum[bisect.bisect_left(ts, t + span)] - cum[bisect.bisect_left(ts, t)]
        return max(near, spent * 1.001)
    return f

STEP_DOWN = [('claude-fable', 'claude-opus-5-5'), ('claude-opus', 'claude-sonnet-5-5')]

def simulate(ev, policy, floor, B5, B7, wb, post=None, advisor_off=False, model_step=False, pause_share=None, admit=False, locks=None, five_resets=(), **knobs):
    """Levers (all act only while 'hot' = some window has used >= 50% and pace >= 1):
      advisor_off  drop advisor consults          model_step  sessions STARTED hot run one tier cheaper
      pause_share  pause a session whose last-hour spend >= this share of the 5h budget, until the window resets
      admit        hold HEADLESS sessions (claude -p / SDK) that start hot, until the window resets
    Held/paused work is counted in out['deferred'] and NOT replayed later (so lockout gains are optimistic)."""
    """Replay every event under `policy`; the gate policy calls the installed g.decide() with simulated rate_limits."""
    cfg = {**g.CFG_DEFAULTS, 'floor': floor, 'hard': HARD, **knobs}
    b5f = B5 if callable(B5) else (lambda t: B5)            # a number, or t -> budget for the window opening at t
    b7f = B7 if callable(B7) else (lambda t: B7)
    B5, B7 = b5f(ev[0][1] if ev else 0), b7f(wb[0])
    st = {}                                    # session -> [extra_tokens_vs_reality, post_compact_size, prev_real_ctx]
    w5 = None; s5 = 0.0; locked5 = None; win = []   # win: (ts, cost) in the current 5h window        # current 5h window start, spend, locked-until
    wi = 0; s7 = 0.0; locked7 = None
    out = collections.Counter(); kept_next = set()
    info = {}                                  # session -> {'hot0', 'until', 'recent'}
    def hot(ts):
        span7 = (wb[wi + 1] - wb[wi]) if wi + 1 < len(wb) else 7 * 86400
        rl = {'five_hour': {'used_percentage': 100 * s5 / B5, 'resets_at': w5 + 5 * H},
              'seven_day': {'used_percentage': 100 * s7 / B7 * (7 * 86400 / span7), 'resets_at': wb[wi] + span7}}
        return any(p >= 1 and rl[k]['used_percentage'] >= 50 for k, (p, _) in g.paces(rl, ts).items())

    def wants_compact(policy, sim, ts):
        if policy == 'actual': return False
        if policy == 'always-full': return sim >= HARD
        span7 = (wb[wi + 1] - wb[wi]) if wi + 1 < len(wb) else 7 * 86400
        rl = {'five_hour': {'used_percentage': 100 * s5 / B5, 'resets_at': w5 + 5 * H},
              'seven_day': {'used_percentage': 100 * s7 / B7 * (7 * 86400 / span7), 'resets_at': wb[wi] + span7}}
        return sim >= floor and g.decide(sim, rl, ts, cfg)[0]
    for e in ev:
        ts = e[1]
        while wi + 1 < len(wb) and ts >= wb[wi + 1]: wi += 1; s7 = 0.0; locked7 = None; B7 = b7f(wb[wi])
        real5 = next((r - 5 * H for r in five_resets if r - 5 * H <= ts < r), None)   # inside a known real 5h window
        if w5 is None or ts >= w5 + 5 * H or (real5 is not None and w5 != real5):
            if real5 is not None and w5 is not None and w5 < real5:      # the real window opened while ours was still running:
                s5 = sum(c for t, c in win if t >= real5)                 # re-phase, keeping the spend that belongs to it
            else: s5 = 0.0
            w5, locked5 = real5 if real5 is not None else ts, None
            B5 = b5f(w5)
            win = [(t, c) for t, c in win if t >= w5]
        if (locked5 and ts < locked5) or (locked7 and ts < locked7): continue   # locked out: this work can't happen now
        k_ev = e[2] if e[0] in ('call', 'advisor', 'compact') else None
        if k_ev is not None:
            si = info.get(k_ev)
            if si is None:
                h0 = hot(ts)
                held = admit and h0 and k_ev in HEADLESS
                si = info[k_ev] = {'hot0': h0, 'until': (w5 + 5 * H) if held else 0, 'recent': collections.deque()}
                if held: out['held_sessions'] += 1
            if si['until'] > ts:
                if e[0] == 'call': out['deferred'] += b.cost(*e[3])
                elif e[0] == 'advisor': out['deferred'] += e[3]
                continue
        if e[0] == 'advisor':
            if advisor_off and hot(ts): out['advisor_saved'] += e[3]; continue
            c = e[3]
            s5 += c; s7 += c; out['usage'] += c; win.append((ts, c))
            if s5 >= B5 * 0.9999 and not locked5:   # reaching the budget locks (a lockout's reading is exactly its spend)
                locked5 = w5 + 5 * H; out['lockouts_5h'] += 1; locks is not None and locks.append(('5h', ts)); out['hours_locked'] += (locked5 - ts) / H
            if s7 >= B7 * 0.9999 and not locked7 and wi + 1 < len(wb):
                locked7 = wb[wi + 1]; out['lockouts_week'] += 1; locks is not None and locks.append(('week', ts)); out['hours_locked'] += (locked7 - ts) / H
            continue
        if e[0] == 'compact':
            _, _, k, trig, pre, post = e
            s = st.get(k)
            # keep what a real auto-compaction dropped -- unless it fired at the model's own (small) window,
            # or the policy itself would compact here
            if policy != 'actual' and trig == 'auto' and s is not None and pre >= floor - 30_000 \
                    and not wants_compact(policy, pre + s[0], ts):
                s[0] += max(pre - post, 0); kept_next.add(k)
            else:
                if trig == 'auto': out['compactions'] += 1
                if s: s[0] = 0
            continue
        if e[0] == 'side':
            c = e[2]
        else:
            _, _, k, (inp, cw, cr, o, mdl) = e
            if model_step and info[k]['hot0']:
                mdl = next((to for frm, to in STEP_DOWN if (mdl or '').startswith(frm)), mdl)
            ctx = inp + cw + cr
            s = st.setdefault(k, [0, post or min(ctx, b.BASE_CAP) + SUMMARY + REREAD, ctx])
            if ctx < s[2] * 0.6 and k not in kept_next: s[0] = 0         # /clear or similar: applies in every policy
            s[2] = ctx
            sim = ctx + s[0]
            compact = wants_compact(policy, sim, ts)
            if compact and s[1] + 30_000 <= sim:                           # simulated auto-compaction
                out['compactions'] += 1
                c = b.cost(0, 0, sim, SUMMARY, mdl) + b.cost(inp, s[1], 0, o, mdl)
                s[0] = s[1] - ctx
            elif k in kept_next:                                           # first call after a compaction we skipped:
                c = b.cost(inp, 2_000, sim, o, mdl)                        # cache stays warm instead of a rebuild
            else:
                x = s[0]
                if cr >= cw: cr2, cw2 = cr + x, cw
                else: cr2, cw2 = cr, cw + x
                if cr2 < 0: cw2 += cr2; cr2 = 0
                c = b.cost(inp, max(cw2, 0), cr2, o, mdl)
            kept_next.discard(k)
            if pause_share:
                r = info[k]['recent']; r.append((ts, c))
                while r and r[0][0] < ts - H: r.popleft()
                if sum(x for _, x in r) >= pause_share * B5 and hot(ts):
                    info[k]['until'] = w5 + 5 * H; out['pauses'] += 1; r.clear()
        s5 += c; s7 += c; out['usage'] += c; win.append((ts, c))
        if s5 >= B5 * 0.9999 and not locked5:
            locked5 = w5 + 5 * H; out['lockouts_5h'] += 1; locks is not None and locks.append(('5h', ts)); out['hours_locked'] += (locked5 - ts) / H
        if s7 >= B7 * 0.9999 and not locked7 and wi + 1 < len(wb):
            locked7 = wb[wi + 1]; out['lockouts_week'] += 1; locks is not None and locks.append(('week', ts)); out['hours_locked'] += (locked7 - ts) / H
    return out

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=float, default=30)
    ap.add_argument('--floor', type=int, default=500_000)
    a = ap.parse_args()
    cal = g.load_json(g._p('calibration.json'), {})
    if not cal.get('five_hour') or not cal.get('seven_day'):
        raise SystemExit('calibrate first: python3 limit_pacer.py calibrate --five-hour P --weekly P --weekly-reset "YYYY-MM-DD HH:MM"')
    ev = load(a.days)
    real = [x for x in b.find_lockouts(a.days)]
    anchors = [cal['week_anchor']] + [x['reset'] for x in real if x['kind'] == 'week' and not x['other']]
    wb = week_bounds(anchors, ev[0][1], ev[-1][1])
    weeks = (ev[-1][1] - ev[0][1]) / (7 * 86400)
    print(f"{len(ev)} events over {weeks:.1f} weeks; budgets over time from lockouts + calibration (5h={cal['five_hour']:.3g}, week={cal['seven_day']:.3g} now)")
    print(f"real lockouts found in transcripts: {sum(x['kind'] == '5h' for x in real)} 5h, {sum(x['kind'] == 'week' for x in real)} weekly\n")
    post = post_compact_size(ev)
    fr = [x['reset'] for x in real if x['kind'] == '5h']
    print(f'real compactions leave a median {post // 1000}k context; the simulation compacts to that\n')
    lev = dict(admit=True, model_step=True, advisor_off=True)
    pts = budget_points(ev, real, cal)
    B5, B7 = budget_fn(ev, real, pts['five_hour'], '5h'), budget_fn(ev, real, pts['seven_day'], 'week')
    rows = {'today': simulate(ev, 'actual', a.floor, B5, B7, wb, post=post, five_resets=fr),
            'compact@830k': simulate(ev, 'always-full', a.floor, B5, B7, wb, post=post, five_resets=fr),
            '830k + pacer': simulate(ev, 'always-full', a.floor, B5, B7, wb, post=post, five_resets=fr, **lev)}
    base = rows['today']['usage']
    print(f"{'policy':14} {'compactions/wk':>14} {'5h lockouts':>11} {'weekly':>7} {'hours locked':>12} {'usage':>6} {'held back':>10}")
    for p, r in rows.items():
        print(f"{p:14} {r['compactions'] / weeks:>14.1f} {r['lockouts_5h']:>11} {r['lockouts_week']:>7} {r['hours_locked']:>12.1f} "
              f"{r['usage'] / base:>6.0%} {'$%.0f' % r['deferred']:>10}")

if __name__ == '__main__':
    main()
