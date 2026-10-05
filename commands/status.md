---
description: Current usage pace, whether the pacer is running hot, held runs, and lockouts per week since install
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" status`

Summarize in at most three lines: hot or cool and why, which levers are active (if any), and the lockouts-per-week comparison. Don't repeat the raw output.
