---
description: What this plugin would have saved you (compactions, lockouts), what you wasted, and how much of each limit you use
argument-hint: "[--days N | --since YYYY-MM-DD] [--fable-pct P]"
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/audit.py" $ARGUMENTS`

If it asks to calibrate first, say so and offer `/cc-limit-pacer:calibrate`. Otherwise give a short verdict in plain words, in this order:
1. **Saved**: the change in compactions/week, lockouts and hours locked from `today` to `830k + pacer`, and the price (work held back). If the credibility line says the budgets look off, lead with that and offer `/cc-limit-pacer:calibrate`.
2. **Wasted**: the biggest item of compaction spend, hours locked out, weekly allowance left unused, Fable allowance unused, advisor spend.
3. **Limits**: whether they run close to the weekly limit or leave it on the table, and how full their 5h windows usually get.
If the output has no Fable line and you have a usage tool (e.g. `get_usage`), read the "Weekly · Fable" % and rerun with `--fable-pct P`. Offer `/cc-limit-pacer:stats` for the visual version.
