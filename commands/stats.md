---
description: Stats page — limits now, what the pacer saves, waste, weekly and 5h usage history, pacer activity — as a shareable artifact
argument-hint: "[--days N | --since YYYY-MM-DD] [--fable-pct P]"
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/audit.py" --html ~/.claude/state/cc-limit-pacer/stats.html $ARGUMENTS && echo "plugin root: ${CLAUDE_PLUGIN_ROOT}"`

The stats page is at `~/.claude/state/cc-limit-pacer/stats.html` (a self-contained page).
- If `--fable-pct` was not passed and you have a usage tool (e.g. `get_usage`), read the "Weekly · Fable" % and rerun the command above with `--fable-pct P` so the Fable meter is real.
- If you have an Artifact tool, publish the file as an artifact (icon `chart`, description "CC Limit Pacer usage, savings and waste"); if one was published before for this file, update it in place. Otherwise run `open ~/.claude/state/cc-limit-pacer/stats.html`.
- Then give the link and, in at most three lines, the headline numbers: saved compactions/lockouts, the biggest waste, weekly usage.
