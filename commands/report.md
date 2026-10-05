---
description: Bug checklist for the last day — hook errors, slow runs, missed lockouts, sensor drift, settings changes
argument-hint: "[--hours N]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" report:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" report $ARGUMENTS`

If it says "OK", say so in one line. Otherwise list each item under "NEEDS A LOOK" with the likely cause and the fix. For hook errors, read the latest entry in `~/.claude/state/cc-limit-pacer/errors.jsonl` and quote the traceback's last line.
