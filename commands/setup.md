---
description: One-time setup — compact at ~830k on 1M-window models (backs up settings.json first)
argument-hint: "[--compact-at TOKENS]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" install:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" install --plugin $ARGUMENTS`

Tell the user in two or three lines what changed (from the output above), and that the pacer hooks are already active through the plugin. If it says to calibrate next, ask for their current 5-hour %, weekly % and weekly reset time from `/usage` (or read them with a usage tool if this session has one), then run `/cc-limit-pacer:calibrate` with them.
