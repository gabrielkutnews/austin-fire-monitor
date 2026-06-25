# Austin Fire + TCEQ Spill + r/Austin Monitor

DMs the Slack users in the `SLACK_USER_IDS` secret (via the **breakingbot** app
in the KUT and KUTX workspace) whenever:
- the [Austin Real-Time Fire Incidents dataset](https://data.austintexas.gov/d/wpu4-x69d)
  gains a newsworthy incident or one changes status (e.g. ACTIVE → ARCHIVED),
- the [TCEQ Emergency Response Spills dataset](https://data.texas.gov/d/xagr-a3x2)
  gains an Austin-area or very large spill, or
- [r/Austin](https://www.reddit.com/r/Austin/) gets a breaking-news-keyword post
  or one that crosses an upvote threshold (trending).

To add a recipient: get their Slack member ID (profile → ⋮ → Copy member ID)
and re-set the full comma-separated list — no code change:

```bash
gh secret set SLACK_USER_IDS --body "U0AAAAAAAAA,U0BBBBBBBBB,Unew…"
```

Polls every **~5 minutes** via **GitHub Actions** (`.github/workflows/monitor.yml`).
Cadence does **not** rely on cron — GitHub throttles scheduled cron badly (we
measured 4–19h gaps). Instead, `loop.sh` runs one job that polls every ~5 min
for ~5h40m, then chains the next run via `gh workflow run`; a `concurrency`
group keeps exactly one loop alive and the **hourly** cron is only a backstop
if a job ever dies without chaining. State (`state.json`) stays in the runner
between polls and is committed back every ~30 min + at job exit. Free: public
repos get unlimited Actions minutes.

**No LLM / zero tokens by design** — each poll is a couple of ~50-byte Socrata
probes (`max(:updated_at)`, one per dataset) plus one `/r/Austin/new` fetch
(credential-free RSS by default, or authenticated OAuth if creds are set);
delta fetches happen only when data changed. Do not put Claude or any LLM in
this polling loop.

## What gets DM'd (news filter)

The raw feed averages **~110 incidents/day**, mostly routine (53/day fire
alarms, 25/day odor/trash/CO/smoke). The filter cuts that to **~12–15 DMs/day**:

- 🚨 **New incident in an alert category** — `issue_reported` starts with one
  of `alert_prefixes` in `config.json`. Defaults: `BOX` (all structure fires
  incl. midrise/hirise/marina), `BRUSH`, `ATTACK` (active attack), `ALERT`
  (aircraft emergencies), `WRESQT`/`RESQT` (water/technical rescue),
  `HMTF`/`HMI`/`HMCLAN` (hazmat), `FLOOD`, `Traffic Injury Pri 1` (incl.
  w/Cardiac). (`Traffic Injury Pri 2` was removed 2026-06-15 as too noisy.)
- ⏱️ **Escalation** — an incident still ACTIVE after `escalation_minutes` (45),
  *except* categories in `escalation_exclude_prefixes` (see below). Routine calls
  archive at p50≈20 min / p90≈38 min (measured 2026-06-11), so 45+ min on a
  non-excluded category means a real sustained response. Fires at most once per
  incident, and works even on cycles where the data didn't change.
  - **`escalation_exclude_prefixes`** (added 2026-06-24) — categories never worth
    escalating: `ODOR`, `RESQV` (vehicle rescue), Traffic Injury Pri 2 (+ Rollover),
    `ALARM`/`ALARMM`/`ALARMH` (fire alarms), `BWP` (broken water pipe), `SMOKE`,
    `AUTO` (auto fire), `TRASH` (trash fire). Escalation still fires for the rest
    (e.g. `GRASS`/`ELEC` fires, `HC` hazardous condition).

(Closure notices — the old `ACTIVE → ARCHIVED after Xm` messages — were removed
2026-06-24; a status change is now logged, not DM'd.)

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

- 🔴 **r/Austin keyword** — a new post whose title/body matches `reddit_keywords`
  (word-boundary match, so "fire" ≠ "fired"). Works **credential-free** by
  default (public RSS feed); each post alerts once.
- 📈 **r/Austin trending** — any recent post (within `reddit_trending_hours`, 24)
  that crosses `reddit_score_threshold` (50 upvotes), regardless of keywords —
  catches whatever the community is reacting to. **Requires OAuth creds** (RSS
  carries no upvote scores); see Secrets. Without creds, only 🔴 keyword alerts
  fire. Reddit is fail-isolated — any Reddit error leaves fire/TCEQ unaffected.

Everything else gets a `suppressed: …` / `tceq suppressed: …` line in
`monitor.log` and is tracked in state (fire incidents can still escalate). To
tune: edit `alert_prefixes` (prefix match against `issue_reported`; e.g. add
`"RESQV"` for vehicle pin-ins ~0.8/day, or `"ELEC"` for electrical fires
~3/day), `escalation_minutes`, `escalation_exclude_prefixes` (which categories
never escalate), `tceq_counties`/`tceq_cities`/`tceq_thresholds`,
or `reddit_keywords`/`reddit_score_threshold`/`reddit_trending_hours` — no code
changes.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | The whole monitor (python3 stdlib only) |
| `config.json` | Non-secret knobs: `alert_prefixes`, `escalation_minutes`, `tceq_*`, `reddit_*` (committed) |
| `state.json` | Fire watermark + statuses, `tceq_watermark`/`tceq_seen`, `reddit_kw_watermark`/`reddit_trending` dedupe (script-managed, committed back each run; delete to re-seed all sources) |
| `loop.sh` | Poll loop wrapper: runs `monitor.py` every ~5 min for ~5h40m, checkpoints state, chains the next run (decouples cadence from cron) |
| `.github/workflows/monitor.yml` | Runs `loop.sh`; hourly cron + concurrency are the restart backstop |
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

Optional — the r/Austin source runs credential-free via RSS (🔴 keyword alerts).
These secrets upgrade it to authenticated OAuth and add 📈 **trending** (RSS has
no upvote scores). Without them, only keyword alerts fire:

| Secret | Value |
|---|---|
| `REDDIT_CLIENT_ID` | The string under the app name on reddit.com/prefs/apps |
| `REDDIT_CLIENT_SECRET` | The app's `secret` |
| `REDDIT_USERNAME` | Your Reddit username (used only in the API User-Agent) |

To create the app: **reddit.com/prefs/apps → "create another app…" → type
`script`** → name it (e.g. `austin-pulse-monitor`), redirect URI
`http://localhost:8080` (unused). Then:

```bash
gh secret set REDDIT_CLIENT_ID     --body '…'
gh secret set REDDIT_CLIENT_SECRET --body '…'
gh secret set REDDIT_USERNAME      --body 'your_reddit_username'
```

Reddit uses app-only OAuth (`grant_type=client_credentials`) for public read
access — no Reddit password is stored. One authenticated `/r/Austin/new` call
per cycle sits well inside the free 100-calls/min quota.

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
gh workflow run "Austin Fire Monitor"   # start a loop now (also how it self-chains)
gh run watch                            # watch the active loop
gh run list --workflow=monitor.yml      # recent runs (should be ~back-to-back)
```

Stop it (the loop self-restarts, so cancelling one run isn't enough):
```bash
gh workflow disable "Austin Fire Monitor"   # stop the chain (no new runs)
gh run cancel <run-id>                       # end the loop currently running
# re-enable later with: gh workflow enable "Austin Fire Monitor"
```

Local (optional, for testing):
```bash
ALL_SECRETS='{"SLACK_BOT_TOKEN":"xoxb-…","SLACK_USER_IDS":"U…"}' \
  python3 ~/austin-fire-monitor/monitor.py --dry-run    # one poll, print instead of DM

# Exercise the loop wrapper itself (skips git/gh when not in CI):
POLL_INTERVAL=15 MAX_RUNTIME=45 bash ~/austin-fire-monitor/loop.sh --dry-run

# Reddit creds can also be passed as plain env vars (handy for a local live test
# without putting them in the PUBLIC config.json):
REDDIT_CLIENT_ID=… REDDIT_CLIENT_SECRET=… REDDIT_USERNAME=… \
  python3 ~/austin-fire-monitor/monitor.py --dry-run
```

To test end-to-end: edit `state.json`, set `watermark` back an hour or two,
run the script — you'll get a real DM with the incidents from that window.

Exit codes: `0` ok/no-change · `1` network/Slack error (watermark untouched,
so the next run retries — nothing is ever lost) · `2` credentials not configured.
