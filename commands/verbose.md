---
description: Turn verbose hook logging on or off (off by default; on logs every run with full detail for debugging)
argument-hint: "[on|off]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" verbose:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" verbose $ARGUMENTS`

Say in one line whether verbose logging is now on or off. If on, mention `/cc-limit-pacer:report` reads it and that it's worth turning off again after debugging.
