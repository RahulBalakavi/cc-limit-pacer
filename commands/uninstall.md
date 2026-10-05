---
description: Restore autoCompactWindow and any model/advisor setting the pacer changed
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" uninstall`

Confirm what was restored, then tell the user to finish with `/plugin uninstall cc-limit-pacer@cc-limit-pacer` so the hooks stop running.
