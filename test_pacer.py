"""One runnable check: python3 test_pacer.py  (builds a fake ~/.claude in a temp dir; touches nothing real)."""
import json, os, sys, tempfile, time
tmp = tempfile.mkdtemp(); os.environ['CLAUDE_HOME'] = tmp; os.environ['CLAUDE_JSON'] = os.path.join(tmp, 'claude.json')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import limit_pacer as g, backtest as b

now = time.time()
def iso(t): return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(t))
def call(t, rid, ctx, out=500, side=False):
    return {'type': 'assistant', 'timestamp': iso(t), 'requestId': rid, 'isSidechain': side, 'sessionId': 's1',
            'message': {'usage': {'input_tokens': 2, 'cache_creation_input_tokens': 10_000, 'cache_read_input_tokens': ctx, 'output_tokens': out}}}

# --- decide(): the whole policy
rl = lambda u5, u7, left5=3600: {'five_hour': {'used_percentage': u5, 'resets_at': now + left5},
                                 'seven_day': {'used_percentage': u7, 'resets_at': now + 4 * 86400}}
assert g.decide(550_000, rl(30, 20), now)[0] is False             # under pace -> keep full context
assert g.decide(550_000, rl(95, 20), now)[0] is True              # 5h running hot -> compact
assert g.decide(550_000, rl(20, 60), now)[0] is True              # 3 days into the week at 60% -> compact
assert g.decide(550_000, rl(10, 20, left5=5 * 3600 - 300), now)[0] is False  # 10% in the first 5 min: burst, not a trend
assert g.decide(550_000, rl(95, 20, left5=600), now)[0] is False  # 10 min to reset -> don't bother
assert g.decide(850_000, rl(0, 0), now)[0] is True                # near the hard window -> always compact
assert g.decide(550_000, {}, now)[0] is True                      # no data -> fail open (compact at floor)
assert g.decide(168_000, rl(10, 10), now)[0] is True              # 200k-window model under pace: blocking would overflow

# --- scan(): dedupe, incremental read, and the 5h window walk
p = os.path.join(tmp, 'projects', 'x'); os.makedirs(p)
f = os.path.join(p, 's1.jsonl')
with open(f, 'w') as fh:
    for i, t in enumerate([now - 7 * 3600, now - 3600, now - 1800]):
        fh.write(json.dumps(call(t, f'r{i}', 100_000)) + '\n')
    fh.write(json.dumps(call(now - 1800, 'r2', 100_000)) + '\n')        # same requestId logged twice
calls = g.scan(now=now)
assert len(calls) == 3, calls
with open(f, 'a') as fh: fh.write(json.dumps(call(now - 60, 'r3', 100_000)) + '\n')
assert len(g.scan(now=now)) == 4                                     # incremental pick-up of appended line
assert abs(g.five_hour_start(g.scan(now=now), now) - (now - 3600)) < 2   # old call is its own expired window
assert abs(g.five_hour_start(g.scan(now=now), now, anchor=now + 600) - (now + 600 - g.W5)) < 2   # known reset wins
assert abs(g.five_hour_start(g.scan(now=now), now, anchor=now - 5000) - (now - 3600)) < 2       # chain past an old reset

# --- calibrate -> estimate round-trips the reading
c = g.scan(now=now)
cal = g.calibrate_from({'five_hour': {'used_percentage': 40, 'resets_at': now - 3600 + g.W5},
                        'seven_day': {'used_percentage': 10, 'resets_at': now + 3 * 86400}}, c, now)
est = g.estimate(c, now, cal)
assert abs(est['five_hour']['used_percentage'] - 40) < 0.5 and abs(est['seven_day']['used_percentage'] - 10) < 0.5, est

# --- backtest: lockout message parsing and the replay's sign
z = 'America/Los_Angeles'
r = b.parse_reset(b.g._ts('2026-09-06T09:12:00Z'), None, '5', '40', 'am', z)
assert r == b.g._ts('2026-09-06T12:40:00Z'), r
r = b.parse_reset(b.g._ts('2026-09-17T21:56:00Z'), 'Sep 21', '1', None, 'am', z)
assert r == b.g._ts('2026-09-21T08:00:00Z'), r
grow = [(i, (2, 10_000, 10_000 * i, 500, 'claude-opus-5-5')) for i in range(150)]       # a session growing 10k/turn to 1.5M
def total(T):
    s = b.Sim(grow[0][1]); return sum(s.step(c, T) for _, c in grow)
assert total(500_000) < total(b.NEVER)                               # compacting a long session saves usage
print('ok')

# --- pacer: hysteresis, step-down, settings write/restore, batch hold (end to end through the hook)
hot_rl = rl(80, 20, left5=2 * 3600)                                  # 80% used, 3h into 5h -> pace 1.33
warm_rl = rl(47, 20, left5=2 * 3600)                                 # pace 0.78, used 47%
assert g.is_hot(hot_rl, now, False) and not g.is_hot(warm_rl, now, False)
assert g.is_hot(rl(62, 20, left5=2.2 * 3600), now, True)            # pace 1.0 > 0.9 exit bar -> stays hot
assert g.step_down('opus[1m]') == 'sonnet[1m]' and g.step_down('claude-fable-5-1') == 'opus[1m]'
assert g.step_down(None) == 'sonnet[1m]' and g.step_down('sonnet') == 'sonnet'

s = {'model': 'opus[1m]', 'advisorModel': 'opus', 'other': 1}; st = {}
ch = g.apply_levers(True, st, s)
assert (s['model'], s['advisorModel'], s['other']) == ('sonnet[1m]', 'off', 1) and len(ch) == 2, (s, ch)
assert g.apply_levers(True, st, s) == []                                    # already applied: no-op (the race fix)
g.apply_levers(False, st, s)
assert (s['model'], s['advisorModel']) == ('opus[1m]', 'opus') and 'wrote' not in st, s
g.apply_levers(True, st, s); s['model'] = 'haiku'                            # user picks a model while hot
ch = g.apply_levers(False, st, s)
assert (s['model'], s['advisorModel']) == ('haiku', 'opus'), s               # their pick survives
assert any(c.get('skipped') for c in ch), ch                                 # ...and the log says why

# spare week: allowance on track to go unused -> new sessions on Fable; hot wins; leaving restores
wk = lambda u7, left_d: {'seven_day': {'used_percentage': u7, 'resets_at': now + left_d * 86400}}
assert g.is_spare(wk(40, 2), now, False) and not g.is_spare(wk(80, 2), now, False)   # 40/0.71 = 56% projected vs 112%
assert not g.is_spare(wk(1, 6), now, False)                                  # a quiet first day is not a spare week
assert g.is_spare(wk(65, 2), now, True) and not g.is_spare(wk(65, 2), now, False)    # 91%: hysteresis band
s = {'model': 'opus[1m]', 'advisorModel': 'opus'}; st = {}
g.apply_levers('spare', st, s); assert s == {'model': 'fable[1m]', 'advisorModel': 'opus'}, s
g.apply_levers('hot', st, s); assert s == {'model': 'sonnet[1m]', 'advisorModel': 'off'}, s   # spare undone first, then step down from opus
g.apply_levers(None, st, s); assert s == {'model': 'opus[1m]', 'advisorModel': 'opus'} and not st, (s, st)
# a step-down left behind with no state is undone back to the install baseline; a deliberate other pick is not
base = {'model': 'opus[1m]', 'advisorModel': 'opus'}
s = {'model': 'sonnet[1m]', 'advisorModel': 'off'}; ch = g.apply_levers(None, {}, s, base)
assert s == base and len(ch) == 2, (s, ch)
s = {'model': 'sonnet[1m]', 'advisorModel': 'opus'}; assert g.apply_levers(None, {}, s, base) == [] and s['model'] == 'sonnet[1m]'

import subprocess
g.save_json(g._p('calibration.json'), {'five_hour': 1e-9, 'seven_day': 1e9, 'five_anchor': now + 3600, 'week_anchor': now + 3 * 86400})
def run_hook(event, cwd, entry='claude-desktop', **env):
    p = subprocess.run([sys.executable, g.__file__, 'pace'], capture_output=True, text=True,
                       input=json.dumps({'hook_event_name': event, 'session_id': 't', 'cwd': cwd}),
                       env={**os.environ, 'CLAUDE_CODE_ENTRYPOINT': entry, **env})
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else {}
out = run_hook('UserPromptSubmit', '/private/tmp/claude-501/x')                 # tiny budget -> hot; temp dir -> held
assert out.get('decision') == 'block' and 'systemMessage' in out, out
assert run_hook('UserPromptSubmit', '/Users/me/project').get('decision') is None   # interactive work is never held
assert run_hook('UserPromptSubmit', '/Users/me/project', entry='sdk-cli').get('decision') == 'block'
assert run_hook('UserPromptSubmit', '/private/tmp/x', LIMIT_PACER_ALLOW='1').get('decision') is None
assert json.load(open(g.SETTINGS))['advisorModel'] == 'off'                    # levers applied by the hook
p = subprocess.run([sys.executable, g.__file__, 'pace'], capture_output=True, text=True, env={**os.environ, 'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli'},
                   input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'cwd': '/private/tmp/x', 'prompt': '/cc-limit-pacer:status'}))
assert 'block' not in p.stdout                                                   # our own commands are never held
print('pacer ok')

# --- logging: every hook run is logged with timing; a crash is logged with a traceback and never blocks the prompt
recs = [json.loads(l) for l in open(g._p('pacer.jsonl'))]
assert all('ms' in r and 'v' in r for r in recs) and any(r.get('transition') == 'hot' for r in recs), recs[-1]
assert all(r['hot'] and 'session' not in r for r in recs), recs   # quiet default: only eventful runs, no per-run detail
assert any(r.get('settings') for r in recs)                                  # lever changes are recorded
open(g._p('pacer.json'), 'w').write('{not json')                             # corrupt state -> load_json default, fine
open(g.SETTINGS, 'w').write('[]')                                            # settings not a dict -> crash inside _pace
p = subprocess.run([sys.executable, g.__file__, 'pace'], capture_output=True, text=True,
                   input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'cwd': '/x'}), env={**os.environ, 'LIMIT_PACER_FORCE': 'hot'})
assert p.returncode == 0 and p.stdout.strip() == '', (p.returncode, p.stdout, p.stderr)
err = [json.loads(l) for l in open(g._p('errors.jsonl'))]
assert 'Traceback' in err[-1]['traceback'], err[-1]
n = len(open(g._p('pacer.jsonl')).readlines())
open(g.SETTINGS, 'w').write('{}'); os.remove(g._p('pacer.json'))
run_hook('SessionStart', '/Users/me/project')                                   # still hot, nothing new happens...
run_hook('SessionStart', '/Users/me/project')
assert len(open(g._p('pacer.jsonl')).readlines()) == n + 1                      # ...only the first (transition) run is logged
run_hook('SessionStart', '/Users/me/project', LIMIT_PACER_VERBOSE='1')
r = json.loads(open(g._p('pacer.jsonl')).readlines()[-1])
assert len(open(g._p('pacer.jsonl')).readlines()) == n + 2 and 'session' in r and 'budgets' in r, r   # verbose: every run, full detail
print('logging ok')

p = subprocess.run([sys.executable, g.__file__, 'report', '--hours', '1'], capture_output=True, text=True)
assert p.returncode == 1 and 'hook errors' in p.stdout and 'NEEDS A LOOK' in p.stdout, p.stdout + p.stderr   # the crash above is surfaced
print('report ok')

# --- audit: counts real compactions + lockouts from transcripts, and the stats page renders
import audit
g.save_json(g._p('calibration.json'), {'five_hour': 50.0, 'seven_day': 500.0, 'five_anchor': now + 3600, 'week_anchor': now + 3 * 86400})
with open(os.path.join(tmp, 'projects', 'x', 's2.jsonl'), 'w') as fh:
    for i in range(6):
        c = call(now - 3 * 3600 + i * 60, f'a{i}', 700_000 if i < 3 else 100_000); c['sessionId'] = 's2'
        c['message']['model'] = 'claude-fable-5-1' if i == 0 else 'claude-opus-5-5'
        fh.write(json.dumps(c, separators=(',', ':')) + '\n')
    fh.write(json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'timestamp': iso(now - 3 * 3600 + 150), 'sessionId': 's2',
                         'compactMetadata': {'trigger': 'auto', 'preTokens': 700_000, 'postTokens': 90_000}}) + '\n')
    fh.write(json.dumps({'type': 'assistant', 'isApiErrorMessage': True, 'timestamp': iso(now - 3600), 'sessionId': 's2',
                         'message': {'content': [{'type': 'text', 'text': "You've hit your session limit · resets 11pm (UTC)"}]}},
                        separators=(',', ':'), ensure_ascii=False) + '\n')
r = audit.audit(days=1, fable_pct=0)
assert r['wasted']['compactions'] == 1 and r['wasted']['compaction_usd'] > 0, r['wasted']
assert r['real_lockouts']['5h'] == 1 and 0 < r['fable']['share'] < 1, (r['real_lockouts'], r['fable'])
assert set(r['policies']) == {'today', 'compact@830k', '830k + pacer'} and r['five_hour']['windows'] >= 1
page = audit.render_html(r); txt = audit.text(r)
assert page.startswith('<title>CC Limit Pacer Stats</title>') and 'None' not in page and 'WHAT YOU WASTED' in txt, txt
print('audit ok')

# --- self-calibration: a lockout is a 100% reading; an estimate past 100% while still working raises the budget
import datetime as dt
tl = now - 1800; rs = dt.datetime.fromtimestamp(tl + 2 * 3600, dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
with open(os.path.join(tmp, 'projects', 'x', 's3.jsonl'), 'w') as fh:
    for k in range(2):                                                         # two blocked sessions, one lockout
        fh.write(json.dumps({'type': 'assistant', 'isApiErrorMessage': True, 'timestamp': iso(tl + k), 'sessionId': 's3',
                             'message': {'content': [{'type': 'text', 'text': f"You've hit your session limit · resets {rs:%-I%p} (UTC)".replace('AM', 'am').replace('PM', 'pm')}]}},
                            separators=(',', ':'), ensure_ascii=False) + '\n')
os.remove(g._p('scan_cache_v3.json')); c3 = g.scan(now=now)                   # warm the cache without the lockout seen
w5 = g.spend(c3, rs.timestamp() - g.W5 - 600, tl)
assert w5 > 0, w5
g.save_json(g._p('calibration.json'), {'five_hour': 0.8 * w5, 'seven_day': 500.0})
os.remove(g._p('scan_cache_v3.json')); g.scan(now=now)
cal = json.load(open(g._p('calibration.json')))
assert abs(cal['five_hour'] - 0.9 * w5) < 1e-6 and len(cal['learned']) == 1 and cal['five_anchor'] == rs.timestamp(), (cal, w5)
g.save_json(g._p('calibration.json'), {'five_hour': 0.1 * w5, 'seven_day': 500.0})
os.remove(g._p('scan_cache_v3.json')); g.scan(now=now)
assert json.load(open(g._p('calibration.json')))['five_hour'] == 0.1 * w5      # 10x off: another account's lockout, ignored
cal = {'five_hour': 0.01, 'seven_day': 1e6, 'five_anchor': now + 3600}
rl2 = g.raise_on_overrun(c3, now, cal, g.estimate(c3, now, cal))
assert rl2['five_hour']['used_percentage'] == 100 and cal['five_hour'] > 0.01 and cal['learned'][-1]['from'] == 'overrun', cal
g.save_json(g._p('calibration.json'), {'five_hour': 1e6, 'seven_day': 1e9})   # big enough that the overrun rule stays out
def app_cache(pct, t, acct='a1'):
    g.save_json(g.CLAUDE_JSON, {'oauthAccount': {'accountUuid': 'a1'}, 'cachedUsageUtilization': {'fetchedAtMs': t * 1000, 'accountUuid': acct,
                'utilization': {'five_hour': {'utilization': pct, 'resets_at': iso(rs.timestamp())}, 'seven_day': None}}})
app_cache(40, now, acct='other'); g.usage(now)
assert json.load(open(g._p('calibration.json')))['five_hour'] == 1e6                  # another account's reading: ignored
app_cache(40, now); g.usage(now)
w5n = g.spend(g.scan(now=now), rs.timestamp() - g.W5 - 600, now)
assert abs(json.load(open(g._p('calibration.json')))['five_hour'] - w5n / 0.4) < 1e-6   # real /usage reading replaces the budget
print('calibration ok')

# --- simulate: a 5h window snaps to a known real reset and keeps only the spend that belongs to it
import simulate
T = now - 86400; ev2 = [('side', T, 60.0, None), ('side', T + 3 * 3600, 60.0, None)]
wb2 = [T - 86400, T + 7 * 86400]
assert simulate.simulate(ev2, 'actual', 500_000, 100, 1e9, wb2)['lockouts_5h'] == 1                       # one rolling window: 120 > 100
assert simulate.simulate(ev2, 'actual', 500_000, 100, 1e9, wb2, five_resets=[T + 7 * 3600])['lockouts_5h'] == 0   # real window opened at T+2h
print('simulate ok')
