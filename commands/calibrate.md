---
description: Teach the pacer your budget from one /usage reading
argument-hint: <5h %> <weekly %> "<weekly reset YYYY-MM-DD HH:MM>" ["<5h reset YYYY-MM-DD HH:MM>"]
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" calibrate:*)
---

Calibrate cc-limit-pacer with: $ARGUMENTS

1. You need the 5-hour % used, the weekly % used, and the local date and time the weekly limit resets; optionally when the 5-hour window resets. Take them from the arguments. If any are missing, read them with a usage tool if this session has one, otherwise ask the user to copy them from `/usage`.
2. Run: `python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" calibrate --five-hour <5h> --weekly <weekly> --weekly-reset "<YYYY-MM-DD HH:MM>"`, adding `--five-hour-reset "<YYYY-MM-DD HH:MM>"` when you know it.
3. Show the "local estimate now" line and say whether it matches what was entered. A mismatch of more than a few points means usage from another machine or claude.ai, so suggest recalibrating later.
