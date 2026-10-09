---
description: Current usage pace, whether the pacer is running hot, held runs, and lockouts per week since install
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" status:*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" calibrate:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" status`

If you have a usage tool (e.g. `get_usage`), first feed its real reading to the pacer so its budgets stay exact: `python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" calibrate --five-hour <5h %> --weekly <weekly all-models %> --weekly-reset "<local YYYY-MM-DD HH:MM>" --five-hour-reset "<local YYYY-MM-DD HH:MM>"`, then rerun status.

Summarize in at most three lines: hot or cool and why, which levers are active (if any), and the lockouts-per-week comparison. Don't repeat the raw output.
