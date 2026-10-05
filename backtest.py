#!/usr/bin/env python3
"""Backtest the gate on YOUR transcripts: find every real lockout, replay it with the gate, report what it buys.

    python3 backtest.py [--days 30] [--floor 500000]

Read-only. For each lockout ("You've hit your session/weekly limit · resets ...") the limit is taken as your real
spend in that window up to the lockout; the window is replayed with the gate choosing per turn whether to compact.
Caveat: the replay can only SHORTEN real sessions (compact earlier); it can't price letting context grow past where
it actually compacted, so it measures the protection side, not the cost of the full-context side.
"""
import argparse, collections, datetime as dt, glob, json, os, re, time
from zoneinfo import ZoneInfo
import limit_pacer as g

SUMMARY, REREAD, NEVER, BASE_CAP = 15_000, 20_000, 10**12, 100_000

def cost(inp, cw, cr, out, model=None):
    i, o, r = g.rates(model)
    return i * (inp + 2.0 * cw) + r * cr + o * out

def load(days):
    """{session: [(ts, (inp, cw, cr, out))]} for main threads, plus [(ts, cost)] for subagents (not gate-governed)."""
    cut = time.time() - days * 86400
    seen, sessions, side = set(), collections.defaultdict(list), []
    for f in glob.glob(os.path.join(g.CLAUDE, 'projects', '**', '*.jsonl'), recursive=True):
        if os.path.getmtime(f) < cut: continue
        for line in open(f, errors='ignore'):
            if '"usage"' not in line: continue
            try: d = json.loads(line)
            except ValueError: continue
            m = d.get('message') or {}; u = m.get('usage'); rid = d.get('requestId') or m.get('id')
            if d.get('type') != 'assistant' or not u or not rid or rid in seen or not d.get('timestamp'): continue
            seen.add(rid)
            ts = g._ts(d['timestamp'])
            if ts < cut: continue
            c = (u.get('input_tokens', 0), u.get('cache_creation_input_tokens', 0), u.get('cache_read_input_tokens', 0), u.get('output_tokens', 0), m.get('model'))
            if d.get('isSidechain') or '/subagents/' in f: side.append((ts, cost(*c)))
            else: sessions[d.get('sessionId') or f].append((ts, c))
    for v in sessions.values(): v.sort()
    return sessions, side

class Sim:
    """One session's counterfactual; step() prices a call when autocompact fires at T (T may change per turn)."""
    def __init__(self, first):
        self.offset, self.prev = 0, 0
        self.floor = min(sum(first[:3]), BASE_CAP) + SUMMARY + REREAD   # fixed overhead (capped: resumed sessions start big) + summary + re-reads

    def step(self, c, T):
        inp, cw, cr, o, mdl = c
        ctx = inp + cw + cr
        if ctx < self.prev * 0.6: self.offset = 0              # a real compaction or /clear happened
        self.prev = ctx
        sim = ctx - self.offset
        if sim > T and self.floor + 30_000 <= T:
            self.offset = ctx - self.floor
            return cost(0, 0, sim, SUMMARY, mdl) + cost(inp, self.floor, 0, o, mdl)   # summary call + cold cache rebuild
        cut = self.offset
        cr2 = max(cr - cut, 0); cut -= cr - cr2
        return cost(inp, max(cw - cut, 0), cr2, o, mdl)

LOCK_RE = re.compile(r"hit your (session|weekly) limit · resets (?:(\w{3} \d{1,2}) at )?(\d{1,2})(?::(\d\d))?(am|pm) \(([^)]+)\)")

def parse_reset(hit_ts, date, hh, mm, ampm, tz):
    z = ZoneInfo(tz); hit = dt.datetime.fromtimestamp(hit_ts, z)
    h = int(hh) % 12 + (12 if ampm == 'pm' else 0)
    if date:
        d = dt.datetime.strptime(f'{date} {hit.year}', '%b %d %Y')
        r = dt.datetime(d.year, d.month, d.day, h, int(mm or 0), tzinfo=z)
    else:
        r = hit.replace(hour=h, minute=int(mm or 0), second=0, microsecond=0)
        if r <= hit: r += dt.timedelta(days=1)
    return r.timestamp()

def find_lockouts(days=30):
    """Real lockouts from the synthetic error messages Claude Code writes into transcripts."""
    cut, found = time.time() - days * 86400, {}
    for f in glob.glob(os.path.join(g.CLAUDE, 'projects', '**', '*.jsonl'), recursive=True):
        if os.path.getmtime(f) < cut: continue
        for line in open(f, errors='ignore'):
            if 'hit your' not in line or '"isApiErrorMessage":true' not in line: continue
            d = json.loads(line); m = LOCK_RE.search(json.dumps(d.get('message'), ensure_ascii=False))
            if not m: continue
            t = g._ts(d['timestamp'])
            if t < cut: continue
            kind = '5h' if m[1] == 'session' else 'week'
            reset = parse_reset(t, m[2], m[3], m[4], m[5], m[6])
            k = (kind, round(reset / 600))
            if k not in found or t < found[k]['t']:
                found[k] = {'kind': kind, 't': t, 'reset': reset, 'start': reset - (g.W5 if kind == '5h' else g.W7)}
    out = sorted(found.values(), key=lambda x: x['t'])
    for x in out:   # hit while an earlier weekly lockout was still in force: another account shares this machine (or limits were reset)
        x['other'] = any(w['kind'] == 'week' and w['t'] < x['t'] < w['reset'] for w in out if w is not x)
    return out

def run(S, side, start, end, policy):
    """Total spend in [start, end] when `policy(ts, spent)` picks the threshold for each in-window call."""
    sims, spent = {}, sum(c for ts, c in side if start <= ts <= end)
    for ts, k, c in sorted((ts, k, c) for k, v in S.items() for ts, c in v if ts <= end):
        sim = sims.setdefault(k, Sim(c))
        if ts < start: sim.step(c, NEVER); continue
        spent += sim.step(c, policy(ts, spent))
    return spent

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=float, default=30)
    ap.add_argument('--floor', type=int, default=500_000)
    a = ap.parse_args()
    print(f'scanning {a.days:g} days of transcripts in {g.CLAUDE}/projects ...')
    S, side = load(a.days)
    lk = [x for x in find_lockouts(a.days) if x['start'] >= time.time() - a.days * 86400]
    print(f'{len(S)} sessions; {len(lk)} lockouts found\n')
    if lk:
        print(f"{'lockout (local time)':22} {'locked':>7} {'500k if hot':>11} {'250k if hot':>11}  result")
        saved = total = 0.0
        for i, x in enumerate(lk):
            if any(y['t'] < x['t'] < y['reset'] and (y['kind'] == x['kind'] or y['kind'] == 'week') for y in lk if y is not x):
                print(f"{x['kind']:4} {dt.datetime.fromtimestamp(x['t']):%b %d %H:%M}   skipped: inside an earlier {'weekly' if x['kind'] == '5h' else x['kind']} lockout "
                      '(different limit or account; its budget can\'t be inferred)'); continue
            start, end, span = x['start'], x['t'], (g.W5 if x['kind'] == '5h' else g.W7)
            B = run(S, side, start, end, lambda ts, sp: NEVER)
            locked_h = (x['reset'] - end) / 3600; total += locked_h
            label = f"{x['kind']:4} {dt.datetime.fromtimestamp(end):%b %d %H:%M}"
            if B <= 0: print(f'{label:22} {locked_h:6.1f}h   (no local usage in window: other devices?)'); continue
            key = 'five_hour' if x['kind'] == '5h' else 'seven_day'
            gate = lambda ts, sp: a.floor if g.decide(0, {key: {'used_percentage': 100 * sp / B, 'resets_at': start + span}}, ts)[0] else NEVER
            r = run(S, side, start, end, gate) / B
            r250 = run(S, side, start, end, lambda ts, sp: 250_000) / B
            burn = B - run(S, side, start, end - 3600, lambda ts, sp: NEVER)   # real spend in the last hour
            extra_h = (1 - r) * B / burn if r < 1 and burn > 0 else 0
            avoided = extra_h >= locked_h
            saved += locked_h if avoided else extra_h
            res = 'AVOIDED' if avoided else (f'{extra_h:.1f}h later' if extra_h >= 0.05 else 'no help')
            print(f'{label:22} {locked_h:6.1f}h {r:>11.0%} {r250:>11.0%}  {res}')
        print(f'\nlocked out {total:.1f}h total; gate would have given back ~{saved:.1f}h '
              f'(spend at lockout as % of the limit; <100% = room left. extrapolated at the last hour\'s burn rate)')
    allc = [c for v in S.values() for c in v]
    actual = sum(cost(*c) for _, c in allc) + sum(c for _, c in side)
    floor = 0.0
    for v in S.values():
        sim = Sim(v[0][1]); floor += sum(sim.step(c, a.floor) for _, c in v)
    floor += sum(c for _, c in side)
    big = sum(1 for v in S.values() if max(sum(c[:3]) for _, c in v) > a.floor)
    print(f'\nif every session compacted at {a.floor // 1000}k: {1 - floor / actual:.0%} less usage over {a.days:g}d '
          f'(only {big} of {len(S)} sessions ever passed {a.floor // 1000}k) — the most headroom the gate can create.')

if __name__ == '__main__':
    main()
