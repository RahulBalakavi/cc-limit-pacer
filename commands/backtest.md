---
description: List every real lockout in your transcripts and replay each window (read-only)
argument-hint: "[--days N]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/backtest.py":*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/backtest.py" $ARGUMENTS`

Show the table, then say in two lines how much of the lockout time came from one or two windows, and whether compacting earlier would have helped (the percentage columns; under 100% means room was left).
