---
description: Replay your last month of sessions under each policy (read-only) to see if this plugin helps you
argument-hint: "[--days N]"
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/simulate.py" $ARGUMENTS`

If it asks to calibrate first, say so and offer `/cc-limit-pacer:calibrate`. Otherwise show the policy table as-is, then explain in plain words:
- whether the `today` row's lockout counts roughly match the real lockouts line (if not, the budget is off and the rest is unreliable);
- what `830k + pacer` changes against `today` in compactions per week and hours locked;
- what it costs, meaning the work held back.
