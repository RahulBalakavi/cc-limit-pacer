---
description: Review what the pacer has changed (model, advisor, compaction, idle-resume guard) and keep, undo or override it
argument-hint: "[keep | undo | set model=… advisor=… compact=600k | auto model|advisor|compact | resume confirm|warn|off | spare on|off | tight 400k]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" adjust:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" adjust $ARGUMENTS`

Show the user the result above in plain words: the mode, each setting with who controls it (the pacer, or them), and the recent adjustments. If they ran it with no arguments, end with one line on what they can do: `keep` (accept the current adjustments as their own), `undo` (restore and leave this mode alone), `set model=… compact=600k` (choose a value; the pacer won't touch it), `auto <name>` (hand it back), `resume confirm|warn|off` (idle-resume guard), `spare on|off` (Fable in spare weeks), `tight 400k` (compaction point while hot). Changes reach new sessions.
