---
description: Stats dashboard — limits now, what the pacer saves, waste, weekly and 5h usage history, limits over time, pacer activity — refreshed in place
argument-hint: "[--days N | --since YYYY-MM-DD]"
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/audit.py":*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" calibrate:*)
---

Refresh the cc-limit-pacer stats dashboard. $ARGUMENTS are extra `audit.py` options.

1. **Real usage first.** If you have a usage tool (e.g. `get_usage`), read the 5-hour %, "Weekly · all models" %, "Weekly · Fable" %, and both reset times. Feed them to the pacer so its budgets stay exact:
   `python3 "${CLAUDE_PLUGIN_ROOT}/limit_pacer.py" calibrate --five-hour <5h %> --weekly <weekly %> --weekly-reset "<local YYYY-MM-DD HH:MM>" --five-hour-reset "<local YYYY-MM-DD HH:MM>"`
2. **Build the data** into a scratch directory (your scratchpad if you have one), passing the readings you have:
   `python3 "${CLAUDE_PLUGIN_ROOT}/audit.py" --dash <DIR> --five-hour-pct <5h %> --weekly-pct <weekly %> --fable-pct <Fable %> $ARGUMENTS`
   It writes `headline.json`, `policies.json`, `weekly.json`, `five_hour.json`, `budgets.json` and `index.html`, and prints `dashboard: <url>` or `dashboard: none yet`.
3. **Publish.** You need an Artifact tool and the Dashboard artifact type; without them, run `python3 "${CLAUDE_PLUGIN_ROOT}/audit.py" --html ~/.claude/state/cc-limit-pacer/stats.html` and `open` it instead.
   - **No dashboard yet:** create one from the Dashboard type (title "CC Limit Pacer"), then save its URL: `python3 "${CLAUDE_PLUGIN_ROOT}/audit.py" --dashboard-url <url>`. Follow the type's instructions to write `dash/meta`, one file dataset per JSON (ids = file names without `.json`), and `files/index.html` from `index.html`.
   - **Dashboard exists:** read it (`ArtifactData` list on `datasets`, and get `files/index.html`) for versions. Upload each JSON as an asset of that dashboard, then in one batch `update` each `datasets/<id>` with the new `source.url` and `updated: {at: <now ISO>, by: "cc-limit-pacer audit.py"}` (pinned to the versions you read), and `set` `files/index.html` to the new `index.html` only if its text changed. Leave the type's own files alone.
   Dataset titles and descriptions for a new dashboard: headline "Pacer headline figures"; policies "Policy replay"; weekly "Weekly limit by week"; five_hour "5-hour windows by share"; budgets "Limit readings over time".
4. Give the link and, in at most three lines, the headline numbers: compactions saved per week and the change in hours locked, the biggest waste, weekly usage now.
