# Austin Fire Incident + TCEQ Spill Monitor

DMs the Slack users in the `SLACK_USER_IDS` secret (via the **breakingbot** app
in the KUT and KUTX workspace) whenever:
- the [Austin Real-Time Fire Incidents dataset](https://data.austintexas.gov/d/wpu4-x69d)
  gains a newsworthy incident or one changes status (e.g. ACTIVE → ARCHIVED), or
- the [TCEQ Emergency Response Spills dataset](https://data.texas.gov/d/xagr-a3x2)
  gains an Austin-area or very large spill.

To add a recipient: get their Slack member ID (profile → ⋮ → Copy member ID)
and re-set the full comma-separated list — no code change:

```bash
gh secret set SLACK_USER_IDS --body "U0AAAAAAAAA,U0BBBBBBBBB,Unew…"
```

Runs every 5 minutes via **GitHub Actions** (`.github/workflows/monitor.yml`).
**No LLM / zero tokens by design** — each
cycle is two ~50-byte Socrata API probes (`max(:updated_at)`, one per
dataset); only when data changed does it fetch the delta rows and post. Do
not put Claude or any LLM in this polling loop.

## What gets DM'd (news filter)

The raw feed averages **~110 incidents/day**, mostly routine (53/day fire
alarms, 25/day odor/trash/CO/smoke). The filter cuts that to **~12–15 DMs/day**:

- 🚨 **New incident in an alert category** — `issue_reported` starts with one
  of `alert_prefixes` in `config.json`. Defaults: `BOX` (all structure fires
  incl. midrise/hirise/marina), `BRUSH`, `ATTACK` (active attack), `ALERT`
  (aircraft emergencies), `WRESQT`/`RESQT` (water/technical rescue),
  `HMTF`/`HMI`/`HMCLAN` (hazmat), `FLOOD`, `Traffic Injury Pri 1` (incl.
  w/Cardiac), `Traffic Injury Pri 2` (incl. Rollover; added 2026-06-11 —
  ~6.5/day plus their closures, the single largest volume contributor).
- ⏱️ **Escalation** — *any* incident, even a routine alarm, still ACTIVE after
  `escalation_minutes` (45). Routine calls archive at p50≈20 min / p90≈38 min
  (measured 2026-06-11), so 45+ min means a real sustained response. Fires at
  most once per incident, and works even on cycles where the data didn't change.
- 🔚 **Closure** — when an incident that was alerted (🚨 or ⏱️) archives, with
  total duration (`ACTIVE → ARCHIVED after 1h 12m`).

- 🛢️ **TCEQ spill** — a spill whose `loc_cnty_name` is in `tceq_counties`
  (Travis/Williamson/Hays/Bastrop/Caldwell) or `near_city_name` in
  `tceq_cities` (Austin), **or** whose amount exceeds `tceq_thresholds`
  (1,000 gallons / 50 barrels / 5,000 pounds; `amt_num` + `uom_cd` with
  synonym + comma handling). Each spill alerts **once** (`tceq_seen` dedupe in
  state) no matter how often TCEQ edits the row. Why the extra machinery: TCEQ
  bulk-touches old rows (measured 18k rows/day churn vs ~37 genuinely new
  spills/month), so the delta fetch is restricted server-side to
  `rcvd_dt` within `tceq_recent_days` (14) — old re-touched rows are never
  downloaded. Expected volume: Austin-area ≈ 1/week + occasional big ones.

Everything else gets a `suppressed: …` / `tceq suppressed: …` line in
`monitor.log` and is tracked in state (fire incidents can still escalate). To
tune: edit `alert_prefixes` (prefix match against `issue_reported`; e.g. add
`"RESQV"` for vehicle pin-ins ~0.8/day, or `"ELEC"` for electrical fires
~3/day), `escalation_minutes`, `tceq_counties`/`tceq_cities`/`tceq_thresholds`
— no code changes.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | The whole monitor (python3 stdlib only) |
| `config.json` | Non-secret knobs: `alert_prefixes`, `escalation_minutes`, `tceq_*` (committed) |
| `state.json` | Fire watermark + incident statuses, `tceq_watermark` + `tceq_seen` dedupe (script-managed, committed back each run; delete to re-trigger the "Monitoring started" DM) |
| `.github/workflows/monitor.yml` | Schedules the run every 5 min and persists `state.json` |
| `monitor.log` | Local launchd output (gitignored; cloud logs live in the Actions run console) |

## Secrets

The monitor runs on GitHub Actions. The workflow injects the entire repo
secret set as one JSON env var (`ALL_SECRETS: ${{ toJSON(secrets) }}`), and
`monitor.py` parses it. Two secrets are required:

| Secret | Value |
|---|---|
| `SLACK_BOT_TOKEN` | The `xoxb-…` bot token (breakingbot, scope `chat:write`) |
| `SLACK_USER_IDS` | Comma-separated Slack member IDs to DM |

```bash
gh secret set SLACK_BOT_TOKEN   --body 'xoxb-…'
gh secret set SLACK_USER_IDS    --body 'U0AAAAAAAAA,U0BBBBBBBBB,U0CCCCCCCCC'
```

**Adding more secrets later needs no workflow edit** — thanks to the
`toJSON(secrets)` auto-map, `gh secret set NEW_NAME` is enough; read it in code
via `secrets.get("NEW_NAME")` inside `load_config()`.

To rotate the bot token: generate a new one in the Slack app settings
(api.slack.com/apps → breakingbot → OAuth & Permissions) and re-run
`gh secret set SLACK_BOT_TOKEN`. The script needs only the `chat:write` scope
(it DMs the member ID directly; it deliberately avoids `conversations.open`,
which would need `im:write`).

**Local runs** still work without GitHub: when `ALL_SECRETS` is unset,
`monitor.py` falls back to the macOS Keychain (service `austin-fire-monitor`,
account `slack-bot-token`) and then to a `slack_bot_token`/`slack_user_ids` in
`config.json`.

## Operating it

Cloud (primary):
```bash
gh workflow run "Austin Fire Monitor"   # trigger a run now
gh run watch                            # watch the latest run
gh run list --workflow=monitor.yml      # recent runs
```

Local (optional, for testing):
```bash
ALL_SECRETS='{"SLACK_BOT_TOKEN":"xoxb-…","SLACK_USER_IDS":"U…"}' \
  python3 ~/austin-fire-monitor/monitor.py --dry-run    # print instead of DM
```

To test end-to-end: edit `state.json`, set `watermark` back an hour or two,
run the script — you'll get a real DM with the incidents from that window.

Exit codes: `0` ok/no-change · `1` network/Slack error (watermark untouched,
so the next run retries — nothing is ever lost) · `2` credentials not configured.
