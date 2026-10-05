#!/usr/bin/env python3
"""What this plugin would have saved you, what you wasted, and how much of each limit you actually use.

    python3 audit.py [--days 23 | --since YYYY-MM-DD] [--fable-pct P] [--json] [--html FILE]

Read-only. Replays your transcripts (same engine as simulate.py) and reads the pacer logs.
  saved     compactions / lockouts / hours locked: today vs compact@830k vs 830k + pacer
  wasted    $ spent compacting, hours locked out, weekly allowance left unused at reset, Fable allowance unused
  usage     % of each 5h / weekly window you actually used (budgets from your calibration)
--fable-pct: your current "Weekly · Fable" % from /usage, to size the Fable allowance (it has no other source).
--html writes a self-contained stats page (used by /cc-limit-pacer:stats).
"""
import argparse, datetime as dt, html, json, os, statistics, time
import limit_pacer as g
import backtest as b
import simulate as s

FABLE = ('claude-fable', 'claude-mythos')
W5, W7 = g.W5, g.W7

def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0

def costs(ev):
    """[(ts, $, model)] of everything that counted against your limits, as it really happened."""
    out, last = [], {}
    for e in ev:
        if e[0] == 'call': last[e[2]] = e[3][4]; out.append((e[1], b.cost(*e[3]), e[3][4]))
        elif e[0] == 'side': out.append((e[1], e[2], e[3]))
        elif e[0] == 'advisor': out.append((e[1], e[3], 'advisor'))
    return out, last

def compaction_waste(ev):
    """$ of each real auto-compaction: the summary call reads the whole context, then the cache is rebuilt."""
    model, n, usd = {}, 0, 0.0
    for e in ev:
        if e[0] == 'call': model[e[2]] = e[3][4]
        elif e[0] == 'compact' and e[3] == 'auto':
            m = model.get(e[2]); n += 1
            usd += b.cost(0, 0, e[4], s.SUMMARY, m) + b.cost(0, e[5] or 80_000, 0, 0, m)
    return n, usd

def five_hour_windows(calls):
    """Claude's 5h window opens on the first message after the previous one closed."""
    wins = []
    for ts, c, _ in calls:
        if not wins or ts >= wins[-1][0] + W5: wins.append([ts, 0.0])
        wins[-1][1] += c
    return wins

def pacer_stats():
    recs = []
    for f in (g._p('pacer.jsonl') + '.1', g._p('pacer.jsonl')):
        if os.path.exists(f):
            for line in open(f):
                try: recs.append(json.loads(line))
                except ValueError: pass
    recs.sort(key=lambda r: r['t'])
    errs = sum(1 for _ in open(g._p('errors.jsonl'))) if os.path.exists(g._p('errors.jsonl')) else 0
    ms = [r['ms'] for r in recs if 'ms' in r]
    series = [(r['t'], (r.get('pct') or {}).get('five_hour'), (r.get('pct') or {}).get('seven_day')) for r in recs if r.get('pct')]
    return {'runs': len(recs), 'sessions': len({r.get('session') for r in recs if r.get('session')}),
            'hot_runs': sum(bool(r.get('hot')) for r in recs), 'held': sum(bool(r.get('held')) for r in recs),
            'transitions': [(r['t'], r['transition']) for r in recs if r.get('transition')],
            'settings_changes': sum(bool(r.get('settings')) for r in recs), 'errors': errs,
            'p50_ms': _pct(ms, .5), 'p95_ms': _pct(ms, .95), 'since': recs[0]['t'] if recs else None,
            'verbose': g.config().get('verbose'), 'series': series,
            'installed': g.load_json(g._p('install.json'), {}).get('t'), 'state': g.load_json(g._p('pacer.json'), {})}

def audit(days=23, fable_pct=None):
    now = time.time()
    cal = g.load_json(g._p('calibration.json'), {})
    if not cal.get('five_hour') or not cal.get('seven_day'):
        raise SystemExit('calibrate first: /cc-limit-pacer:calibrate (or limit_pacer.py calibrate --five-hour P --weekly P --weekly-reset "YYYY-MM-DD HH:MM")')
    B5, B7 = cal['five_hour'], cal['seven_day']
    ev = s.load(days)
    if not ev: raise SystemExit(f'no Claude Code transcripts in the last {days} days')
    real = b.find_lockouts(days)
    wb = s.week_bounds([cal['week_anchor']] + [x['reset'] for x in real if x['kind'] == 'week' and not x['other']], ev[0][1], ev[-1][1])
    weeks = max((ev[-1][1] - ev[0][1]) / W7, 1 / 7)
    post = s.post_compact_size(ev)
    fr = [x['reset'] for x in real if x['kind'] == '5h']
    lev = dict(admit=True, model_step=True, advisor_off=True)
    L = []
    sims = {'today': s.simulate(ev, 'actual', 500_000, B5, B7, wb, post=post, five_resets=fr, locks=L),
            'compact@830k': s.simulate(ev, 'always-full', 500_000, B5, B7, wb, post=post, five_resets=fr),
            '830k + pacer': s.simulate(ev, 'always-full', 500_000, B5, B7, wb, post=post, five_resets=fr, **lev)}
    base = sims['today']['usage'] or 1
    policies = {k: {'compactions_wk': r['compactions'] / weeks, 'lockouts_5h': r['lockouts_5h'], 'lockouts_week': r['lockouts_week'],
                    'hours_locked': r['hours_locked'], 'usage': r['usage'] / base, 'held_back_usd': r['deferred']} for k, r in sims.items()}

    calls, _ = costs(ev)
    total = sum(c for _, c, _ in calls)
    n_comp, comp_usd = compaction_waste(ev)
    adv_usd = sum(c for _, c, m in calls if m == 'advisor')

    # weekly windows: completed ones whose whole span is inside the data, plus the current one
    wk = []
    for a, z in zip(wb, wb[1:]):
        if a < ev[0][1] - 3600 or a > now: continue
        sp = sum(c for ts, c, _ in calls if a <= ts < z)
        fab = sum(c for ts, c, m in calls if a <= ts < z and (m or '').startswith(FABLE))
        wk.append({'start': a, 'end': z, 'done': z <= now, 'full': z - a >= 6 * 86400, 'pct': 100 * sp / B7, 'fable_usd': fab, 'usd': sp,
                   'locked': any(x['kind'] == 'week' and a <= x['t'] < z for x in real)})
    done = [w for w in wk if w['done']]
    cur = next((w for w in wk if not w['done']), None)
    unused = [max(0.0, 100 - w['pct']) for w in done if w['full']]

    fives = five_hour_windows(calls)
    f_pct = [100 * x / B5 for _, x in fives]
    fable = {'pct_now': fable_pct, 'usd_this_week': cur['fable_usd'] if cur else 0,
             'share': sum(c for _, c, m in calls if (m or '').startswith(FABLE)) / (total or 1)}
    if fable_pct and cur and cur['fable_usd'] > 0:
        BF = cur['fable_usd'] / (fable_pct / 100)
        fable['budget_usd'] = BF
        fable['unused_by_week'] = [max(0.0, 100 - 100 * w['fable_usd'] / BF) for w in done if w['full']]

    near = lambda x: any(k == x['kind'] and abs(t - x['t']) < 4 * 3600 for k, t in L)
    mine = [x for x in real if not x['other']]
    match = {'hit': sum(map(near, mine)), 'of': len(mine), 'missed': [(x['kind'], x['t']) for x in mine if not near(x)],
             'other': [(x['kind'], x['t']) for x in real if x['other']],
             'extra': sum(not any(k == x['kind'] and abs(t - x['t']) < 4 * 3600 for x in real) for k, t in L)}
    real_hours = sum(max(0.0, x['reset'] - x['t']) for x in real) / 3600
    return {'generated': now, 'days': days, 'weeks': weeks, 'budgets': {'five_hour': B5, 'seven_day': B7},
            'real_lockouts': {'5h': sum(x['kind'] == '5h' for x in real), 'week': sum(x['kind'] == 'week' for x in real), 'hours_locked': real_hours},
            'policies': policies, 'match': match, 'post_compact_k': post // 1000,
            'wasted': {'compactions': n_comp, 'compaction_usd': comp_usd, 'compaction_share': comp_usd / (total or 1),
                       'advisor_usd': adv_usd, 'advisor_share': adv_usd / (total or 1), 'hours_locked': real_hours,
                       'weekly_unused_avg_pct': statistics.mean(unused) if unused else None,
                       'weekly_unused_weeks': sum(u >= 25 for u in unused), 'weekly_full_weeks': len(unused)},
            'weekly': wk, 'current_week': cur,
            'five_hour': {'windows': len(fives), 'median_pct': _pct(f_pct, .5), 'p90_pct': _pct(f_pct, .9),
                          'over_90': sum(p >= 90 for p in f_pct), 'under_25': sum(p < 25 for p in f_pct),
                          'hist': [sum(lo <= p < lo + 10 for p in f_pct) for lo in range(0, 100, 10)] + [sum(p >= 100 for p in f_pct)]},
            'fable': fable, 'pacer': pacer_stats(), 'version': g.VERSION, 'total_usd': total}

# ---------------------------------------------------------------- text

def _shaky(r): m = r['match']; return m['of'] and (m['hit'] < m['of'] / 2 or m['extra'] > m['hit'])

def _cred(r):
    m = r['match']
    s = f"the replay reproduces {m['hit']} of your {m['of']} real lockouts within 4h, plus {m['extra']} that didn't happen"
    if m.get('other'): s += f"; {len(m['other'])} more happened while another weekly lockout was in force, so they came from another account sharing this machine and are left out"
    if m['missed']: s += '; missed ' + ', '.join(f"{k} {dt.datetime.fromtimestamp(t).strftime('%b %d %H:%M')}" for k, t in m['missed'])
    return s + ('. Budgets look off or several accounts share this machine; treat the deltas as rough.' if _shaky(r) else '.')

def _d(t): return dt.datetime.fromtimestamp(t).strftime('%b %d')

def text(r):
    p, w, real = r['policies'], r['wasted'], r['real_lockouts']
    out = [f"cc-limit-pacer {r['version']} audit — since {_d(r['generated'] - r['days'] * 86400)}, {r['days']:.0f} days ({r['weeks']:.1f} weeks)", '',
           'WHAT IT WOULD HAVE SAVED (replay of your sessions)',
           f"  {'policy':14} {'compactions/wk':>14} {'5h lockouts':>11} {'weekly':>7} {'hours locked':>12} {'usage':>6} {'held back':>10}"]
    for k, x in p.items():
        out.append(f"  {k:14} {x['compactions_wk']:>14.1f} {x['lockouts_5h']:>11} {x['lockouts_week']:>7} {x['hours_locked']:>12.1f} {x['usage']:>6.0%} {'$%.0f' % x['held_back_usd']:>10}")
    t, g8 = p['today'], p['830k + pacer']
    out.append(f"  → {g8['compactions_wk'] - t['compactions_wk']:+.1f} compactions/wk, "
               f"{(g8['lockouts_5h'] + g8['lockouts_week']) - (t['lockouts_5h'] + t['lockouts_week']):+d} lockouts, "
               f"{g8['hours_locked'] - t['hours_locked']:+.0f}h locked")
    out.append('  credibility: ' + _cred(r))
    out += ['', 'WHAT YOU WASTED',
            f"  compacting      {w['compactions']} auto-compactions ≈ ${w['compaction_usd']:.0f} API-equivalent ({w['compaction_share']:.1%} of your usage)",
            f"  locked out      {real['5h']} session + {real['week']} weekly lockouts, {w['hours_locked']:.0f}h unable to work",
            f"  advisor         ${w['advisor_usd']:.0f} ({w['advisor_share']:.0%} of usage) on advisor consults"]
    if w['weekly_unused_avg_pct'] is not None:
        out.append(f"  weekly limit    {w['weekly_unused_avg_pct']:.0f}% of the allowance unused at reset on average; "
                   f"{w['weekly_unused_weeks']} of {w['weekly_full_weeks']} full weeks left ≥25% on the table")
    f = r['fable']
    if f.get('pct_now') is not None:
        if f.get('unused_by_week'):
            out.append(f"  Fable           {f['pct_now']:.0f}% used this week; past weeks left {statistics.mean(f['unused_by_week']):.0f}% of the Fable allowance unused")
        else:
            out.append(f"  Fable           {f['pct_now']:.0f}% used this week" + (" — the whole Fable allowance is going unused" if f['pct_now'] < 5 else ''))
    out.append(f"                  Fable is {f['share']:.0%} of your usage over the period")
    out += ['', 'HOW MUCH OF EACH LIMIT YOU USE']
    for x in r['weekly']:
        out.append(f"  {_d(x['start'])} – {_d(x['end'])}  {x['pct']:5.0f}%" + ('  (so far)' if not x['done'] else '') + ('' if x['full'] else '  (short window: reset moved)') + ('  LOCKED' if x['locked'] else ''))
    fh = r['five_hour']
    out.append(f"  5h windows      {fh['windows']} used; median {fh['median_pct']:.0f}%, p90 {fh['p90_pct']:.0f}%; "
               f"{fh['over_90']} ran ≥90%, {fh['under_25']} stayed under 25%")
    pc = r['pacer']
    if pc['runs']:
        out += ['', 'PACER SINCE INSTALL',
                f"  {pc['runs']} hook runs in {pc['sessions']} sessions; hot on {pc['hot_runs']}; held {pc['held']} automated prompts; "
                f"{pc['settings_changes']} settings changes; {pc['errors']} errors; p50 {pc['p50_ms']}ms p95 {pc['p95_ms']}ms"]
    return '\n'.join(out)

# ---------------------------------------------------------------- html

CSS = """
/* Layout: one column of instrument panels; meters first, then the counterfactual, the waste ledger, then history. */
:root{--bg:#f3f4f6;--panel:#ffffff;--ink:#1b2230;--mute:#5d6878;--line:#d9dde4;--accent:#c7801a;--good:#2f7d5b;--bad:#b4433a;--track:#e6e9ee;
--display:'Barlow Condensed','Arial Narrow',sans-serif;--body:'IBM Plex Sans',system-ui,sans-serif;--mono:'IBM Plex Mono',ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#11151c;--panel:#181e27;--ink:#e6e9ef;--mute:#98a2b3;--line:#2a3240;--accent:#e9a23b;--good:#5cbf8f;--bad:#e2776c;--track:#232b37;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#11151c;--panel:#181e27;--ink:#e6e9ef;--mute:#98a2b3;--line:#2a3240;--accent:#e9a23b;--good:#5cbf8f;--bad:#e2776c;--track:#232b37;color-scheme:dark}
body{background:var(--bg);color:var(--ink);font:15px/1.5 var(--body)}
.wrap{max-width:980px;margin:0 auto;padding:28px 16px 48px;display:grid;gap:18px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:end;gap:8px}
h1{font:600 2.4rem/1 var(--display);letter-spacing:.01em;margin:0;text-wrap:balance}
h2{font:600 1.15rem/1.2 var(--display);letter-spacing:.06em;text-transform:uppercase;margin:0 0 12px;color:var(--mute)}
.sub{color:var(--mute);font-size:.9rem}
.chip{font:600 .75rem var(--mono);letter-spacing:.08em;text-transform:uppercase;padding:3px 9px;border-radius:99px;border:1px solid var(--line)}
.chip.hot{color:var(--bad);border-color:var(--bad)} .chip.cool{color:var(--good);border-color:var(--good)}
section{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:18px;min-width:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.meter .n{font:600 2.2rem/1 var(--display);font-variant-numeric:tabular-nums}
.meter .l{color:var(--mute);font-size:.85rem}
.bar{height:8px;background:var(--track);border-radius:4px;position:relative;margin:8px 0 4px;overflow:hidden}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:4px}
.tbl{overflow-x:auto} table{border-collapse:collapse;width:100%;font:14px var(--mono);font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap} th:first-child,td:first-child{text-align:left}
th{font:500 .75rem var(--body);color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
tr.us td{color:var(--accent);font-weight:600}
.delta{margin-top:12px;font-size:.95rem} .delta b{font-variant-numeric:tabular-nums}
.ledger{display:grid;gap:10px} .ledger div{display:grid;grid-template-columns:150px 1fr;gap:12px;align-items:baseline}
.ledger dt{font:600 .8rem var(--body);text-transform:uppercase;letter-spacing:.06em;color:var(--mute)} .ledger dd{margin:0}
@media (max-width:520px){.ledger div{grid-template-columns:1fr;gap:2px}}
.warn{color:var(--bad)} .note{color:var(--mute);font-size:.85rem;margin-top:10px}
svg text{fill:var(--mute);font:11px var(--mono)} svg .ax{stroke:var(--line)} svg .ref{stroke:var(--bad);stroke-dasharray:4 3}
svg .b{fill:var(--accent)} svg .b.lock{fill:var(--bad)} svg .b.part{fill:var(--accent);opacity:.45}
svg .l5{stroke:var(--accent);fill:none;stroke-width:1.6} svg .l7{stroke:var(--good);fill:none;stroke-width:1.6}
.legend{display:flex;gap:14px;font-size:.8rem;color:var(--mute)} .legend span::before{content:'';display:inline-block;width:10px;height:3px;margin-right:6px;vertical-align:middle;background:var(--k)}
"""

def _meter(label, pct, note):
    p = max(0, min(100, pct or 0))
    return (f'<div class="meter"><div class="l">{label}</div><div class="n">{pct:.0f}%</div>'
            f'<div class="bar"><i style="width:{p:.1f}%"></i></div><div class="l">{note}</div></div>')

def _weekly_svg(weeks):
    if not weeks: return '<p class="note">No full weeks in range yet.</p>'
    W, H, L, B = 640, 210, 34, 30
    top = max(110, max(w['pct'] for w in weeks) + 10)
    y = lambda v: H - B - (H - B - 12) * v / top
    bw = (W - L - 10) / len(weeks)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Weekly limit used per week">']
    for v in range(0, int(top) + 1, 25):
        out.append(f'<line class="ax" x1="{L}" x2="{W - 6}" y1="{y(v):.1f}" y2="{y(v):.1f}"/><text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v}</text>')
    out.append(f'<line class="ref" x1="{L}" x2="{W - 6}" y1="{y(100):.1f}" y2="{y(100):.1f}"/>')
    for i, w in enumerate(weeks):
        x = L + i * bw + bw * .18; cls = 'b lock' if w['locked'] else ('b part' if not w['done'] else 'b')
        out.append(f'<rect class="{cls}" x="{x:.1f}" y="{y(w["pct"]):.1f}" width="{bw * .64:.1f}" height="{y(0) - y(w["pct"]):.1f}"><title>{w["pct"]:.0f}%</title></rect>'
                   f'<text x="{x + bw * .32:.1f}" y="{y(w["pct"]) - 5:.1f}" text-anchor="middle">{w["pct"]:.0f}%</text>'
                   f'<text x="{x + bw * .32:.1f}" y="{H - 10}" text-anchor="middle">{_d(w["start"])}</text>')
    return ''.join(out) + '</svg>'

def _hist_svg(hist):
    W, H, L, B = 640, 190, 34, 30
    top = max(hist) or 1
    bw = (W - L - 10) / len(hist)
    y = lambda v: H - B - (H - B - 14) * v / top
    labels = [f'{lo}' for lo in range(0, 100, 10)] + ['100+']
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="5-hour windows by share of the limit used">',
           f'<line class="ax" x1="{L}" x2="{W - 6}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>']
    for i, n in enumerate(hist):
        x = L + i * bw + bw * .12
        out.append(f'<rect class="b{" lock" if i == len(hist) - 1 else ""}" x="{x:.1f}" y="{y(n):.1f}" width="{bw * .76:.1f}" height="{y(0) - y(n):.1f}"/>'
                   f'<text x="{x + bw * .38:.1f}" y="{y(n) - 5:.1f}" text-anchor="middle">{n}</text>'
                   f'<text x="{x + bw * .38:.1f}" y="{H - 10}" text-anchor="middle">{labels[i]}</text>')
    return ''.join(out) + '</svg>'

def _series_svg(series):
    pts = [(t, a, c) for t, a, c in series if a is not None and c is not None]
    if len(pts) < 2: return '<p class="note">Turn on verbose logging (<code>/cc-limit-pacer:verbose on</code>) to chart usage over time.</p>'
    W, H, L, B = 640, 200, 34, 28
    t0, t1 = pts[0][0], pts[-1][0]; top = max(60, max(max(a, c) for _, a, c in pts) + 10)
    x = lambda t: L + (W - L - 10) * (t - t0) / ((t1 - t0) or 1)
    y = lambda v: H - B - (H - B - 12) * v / top
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Estimated usage over time">']
    for v in range(0, int(top) + 1, 25):
        out.append(f'<line class="ax" x1="{L}" x2="{W - 6}" y1="{y(v):.1f}" y2="{y(v):.1f}"/><text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v}</text>')
    out.append(f'<line class="ref" x1="{L}" x2="{W - 6}" y1="{y(50):.1f}" y2="{y(50):.1f}"/>')
    for k, cls in ((1, 'l5'), (2, 'l7')):
        out.append(f'<polyline class="{cls}" points="{" ".join(f"{x(p[0]):.1f},{y(p[k]):.1f}" for p in pts)}"/>')
    fmt = lambda t: dt.datetime.fromtimestamp(t).strftime('%b %d %H:%M')
    out.append(f'<text x="{L}" y="{H - 8}">{fmt(t0)}</text><text x="{W - 6}" y="{H - 8}" text-anchor="end">{fmt(t1)}</text>')
    return ''.join(out) + '</svg>'

def render_html(r):
    e = html.escape
    p, w, real, pc, fh, f = r['policies'], r['wasted'], r['real_lockouts'], r['pacer'], r['five_hour'], r['fable']
    t, g8 = p['today'], p['830k + pacer']
    hot = (pc.get('state') or {}).get('hot')
    last5 = next((a for _, a, _ in reversed(pc['series']) if a is not None), None)
    last7 = next((c for _, _, c in reversed(pc['series']) if c is not None), None)
    cur = r['current_week']
    meters = [_meter('5-hour window (estimate)', last5 or 0, 'from the last hook run') if last5 is not None else '',
              _meter('Weekly · all models', last7 if last7 is not None else (cur or {}).get('pct', 0), 'estimate, this week so far'),
              _meter('Weekly · Fable', f['pct_now'], 'from /usage' + (' — allowance unused' if f['pct_now'] < 5 else ''))
              if f.get('pct_now') is not None else
              _meter('Fable share of usage', 100 * f['share'], 'pass --fable-pct to size the allowance')]
    rows = ''.join(f'<tr class="{"us" if k == "830k + pacer" else ""}"><td>{e(k)}</td><td>{x["compactions_wk"]:.1f}</td><td>{x["lockouts_5h"]}</td>'
                   f'<td>{x["lockouts_week"]}</td><td>{x["hours_locked"]:.0f}</td><td>{x["usage"]:.0%}</td><td>${x["held_back_usd"]:,.0f}</td></tr>'
                   for k, x in p.items())
    sim_n = t['lockouts_5h'] + t['lockouts_week']
    cred = f'<p class="note{" warn" if _shaky(r) else ""}">Credibility: {e(_cred(r))}</p>'
    fable_line = (f'{f["pct_now"]:.0f}% of this week\'s Fable allowance used'
                  + (f'; past weeks left {statistics.mean(f["unused_by_week"]):.0f}% unused on average' if f.get('unused_by_week') else '')
                  + (' — this week\'s Fable allowance is going unused' if f['pct_now'] < 5 else '')
                  if f.get('pct_now') is not None else 'Run with --fable-pct (your “Weekly · Fable” % from /usage) to size it')
    weekly_line = (f'{w["weekly_unused_avg_pct"]:.0f}% of the weekly allowance unused at reset on average; '
                   f'{w["weekly_unused_weeks"]} weeks left a quarter or more on the table' if w['weekly_unused_avg_pct'] is not None else 'Not enough full weeks yet')
    activity = (f'<div class="grid">'
                f'<div class="meter"><div class="l">Hook runs</div><div class="n">{pc["runs"]}</div><div class="l">{pc["sessions"]} sessions</div></div>'
                f'<div class="meter"><div class="l">Ran hot</div><div class="n">{pc["hot_runs"]}</div><div class="l">{len(pc["transitions"])} switches</div></div>'
                f'<div class="meter"><div class="l">Held runs</div><div class="n">{pc["held"]}</div><div class="l">automated prompts deferred</div></div>'
                f'<div class="meter"><div class="l">Latency p95</div><div class="n">{pc["p95_ms"]}<small>ms</small></div><div class="l">p50 {pc["p50_ms"]}ms · {pc["errors"]} errors</div></div>'
                f'</div><div class="legend" style="margin-top:14px"><span style="--k:var(--accent)">5-hour %</span><span style="--k:var(--good)">weekly %</span><span style="--k:var(--bad)">50% (pacer can engage)</span></div>'
                + _series_svg(pc['series'])) if pc['runs'] else '<p class="note">No hook runs logged yet.</p>'
    gen = dt.datetime.fromtimestamp(r['generated']).strftime('%b %d %Y, %H:%M')
    return f"""<title>CC Limit Pacer Stats</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600&family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header><div><h1>CC Limit Pacer</h1><div class="sub">Since {_d(r['generated'] - r['days'] * 86400)} · {r['weeks']:.1f} weeks of sessions · v{e(str(r['version']))} · {gen}</div></div>
<span class="chip {'hot' if hot else 'cool'}">pacer {'hot' if hot else 'cool'}</span></header>
<section><h2>Limits now</h2><div class="grid">{''.join(meters)}</div></section>
<section><h2>What it would have saved</h2>
<div class="tbl"><table><thead><tr><th>Policy</th><th>Compactions / wk</th><th>5h lockouts</th><th>Weekly</th><th>Hours locked</th><th>Usage</th><th>Held back</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="delta">With the pacer: <b>{g8['compactions_wk'] - t['compactions_wk']:+.1f}</b> compactions a week,
<b>{(g8['lockouts_5h'] + g8['lockouts_week']) - sim_n:+d}</b> lockouts, <b>{g8['hours_locked'] - t['hours_locked']:+.0f}h</b> locked out,
at the cost of <b>${g8['held_back_usd']:,.0f}</b> of automated work held until the window reset.</p>{cred}
<p class="note">Replay of every session in the period. Compactions resume at the {r['post_compact_k']}k context your real compactions leave behind. Held work is not replayed later, so lockout gains are optimistic.</p></section>
<section><h2>What you wasted</h2><dl class="ledger">
<div><dt>Compacting</dt><dd>{w['compactions']} auto-compactions ≈ ${w['compaction_usd']:,.0f} API-equivalent, {w['compaction_share']:.1%} of your usage</dd></div>
<div><dt>Locked out</dt><dd>{real['5h']} session and {real['week']} weekly lockouts: {w['hours_locked']:.0f} hours you could not work</dd></div>
<div><dt>Weekly allowance</dt><dd>{weekly_line}</dd></div>
<div><dt>Fable</dt><dd>{fable_line}. Fable was {f['share']:.0%} of your usage over the period.</dd></div>
<div><dt>Advisor</dt><dd>${w['advisor_usd']:,.0f} on advisor consults, {w['advisor_share']:.0%} of usage (not shown in normal usage totals)</dd></div>
</dl></section>
<section><h2>Weekly limit used</h2>{_weekly_svg(r['weekly'])}<p class="note">Each bar is one weekly window, labelled by its start; a window shorter than a week means the reset time moved. Red bars hit the weekly limit; the faded bar is this week so far. Dashed line = 100%.</p></section>
<section><h2>5-hour windows</h2>{_hist_svg(fh['hist'])}
<p class="note">{fh['windows']} windows; median {fh['median_pct']:.0f}% of the limit, p90 {fh['p90_pct']:.0f}%. {fh['over_90']} ran past 90%, {fh['under_25']} stayed under 25%.</p></section>
<section><h2>Pacer activity</h2>{activity}</section>
<p class="note">Dollar figures are API-list-price equivalents of your token use, which is what the plan limits meter. Budgets come from your calibration: 5h ≈ ${r['budgets']['five_hour']:,.0f}, weekly ≈ ${r['budgets']['seven_day']:,.0f}.</p>
</div>"""

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=float, default=23)
    ap.add_argument('--since', help='start date YYYY-MM-DD (local time); overrides --days')
    ap.add_argument('--fable-pct', type=float)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--html')
    a = ap.parse_args()
    if a.since: a.days = (time.time() - dt.datetime.strptime(a.since, '%Y-%m-%d').timestamp()) / 86400
    r = audit(a.days, a.fable_pct)
    if a.html:
        with open(os.path.expanduser(a.html), 'w') as fh: fh.write(render_html(r))
        print(f'wrote {os.path.expanduser(a.html)}')
    if a.json: print(json.dumps({k: v for k, v in r.items() if k != 'pacer'} | {'pacer': {k: v for k, v in r['pacer'].items() if k != 'series'}}, default=str))
    elif not a.html: print(text(r))

if __name__ == '__main__':
    main()
