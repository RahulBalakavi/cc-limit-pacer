#!/usr/bin/env python3
"""Render the stats page from made-up sample data (for the README screenshot; no real usage in it).

    python3 docs/sample_stats.py > /tmp/sample.html
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit

D, now = 86400, time.time()
t0 = now - 24 * D
weeks = [dict(start=t0 + i * 7 * D, end=t0 + (i + 1) * 7 * D, done=i < 3, full=True, pct=p, fable_usd=f, usd=0, locked=l)
         for i, (p, f, l) in enumerate([(104, 40, True), (72, 25, False), (91, 60, False), (38, 0, False)])]
series = [(now - 6 * 3600 + i * 600, 5 + 1.1 * i if i < 30 else 8 + 0.6 * (i - 30), 30 + 0.25 * i) for i in range(37)]
r = {'generated': now, 'days': 24, 'weeks': 3.4, 'budgets': {'five_hour': 150, 'seven_day': 1100},
     'real_lockouts': {'5h': 4, 'week': 1, 'hours_locked': 71},
     'policies': {'today': dict(compactions_wk=18.0, lockouts_5h=4, lockouts_week=1, hours_locked=74, usage=1.0, held_back_usd=0),
                  'compact@830k': dict(compactions_wk=11.5, lockouts_5h=4, lockouts_week=1, hours_locked=81, usage=1.03, held_back_usd=0),
                  '830k + pacer': dict(compactions_wk=13.0, lockouts_5h=2, lockouts_week=1, hours_locked=38, usage=0.98, held_back_usd=62)},
     'match': {'hit': 4, 'of': 5, 'missed': [('5h', t0 + 5 * D)], 'other': [], 'extra': 1}, 'post_compact_k': 100,
     'wasted': {'compactions': 70, 'compaction_usd': 58, 'compaction_share': 0.013, 'advisor_usd': 170, 'advisor_share': 0.04,
                'hours_locked': 71, 'weekly_unused_avg_pct': 11, 'weekly_unused_weeks': 2, 'weekly_full_weeks': 3},
     'weekly': weeks, 'current_week': weeks[-1],
     'five_hour': {'windows': 70, 'median_pct': 34, 'p90_pct': 78, 'over_90': 4, 'under_25': 26,
                   'hist': [9, 12, 12, 7, 8, 6, 5, 5, 2, 0, 4]},
     'fable': {'pct_now': 0, 'usd_this_week': 0, 'share': 0.09},
     'pacer': {'runs': 80, 'sessions': 12, 'hot_runs': 0, 'held': 0, 'transitions': [], 'settings_changes': 0, 'errors': 0,
               'p50_ms': 190, 'p95_ms': 640, 'since': now - 6 * 3600, 'verbose': True, 'series': series,
               'installed': now - D, 'state': {'hot': False}},
     'version': '0.5.0', 'total_usd': 4400}
print(audit.render_html(r))
