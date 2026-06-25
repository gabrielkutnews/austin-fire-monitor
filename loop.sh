#!/usr/bin/env bash
# Reliable poll loop for GitHub Actions.
#
# Why: GitHub's scheduled cron is heavily throttled (measured gaps of 4-19h on
# this repo), so a `*/5` cron can't deliver timely alerts. Instead, ONE job
# runs this loop — polling every POLL_INTERVAL for up to MAX_RUNTIME (safely
# under the 6h per-job cap) — then chains the next run via workflow_dispatch.
# An hourly cron + the workflow's concurrency group are the backstop if a job
# ever dies without chaining. Cadence is thus decoupled from cron firing.
#
# State (state.json) lives in the runner's local filesystem across polls, so
# dedup stays exact with NO per-poll commit; it's committed back only as a
# ~30-min checkpoint and once at exit (the handoff to the next chained job).
#
# monitor.py is unchanged — this just calls it repeatedly. Args pass through,
# so it's locally testable without git/gh/Slack, e.g.:
#   POLL_INTERVAL=15 MAX_RUNTIME=45 bash loop.sh --dry-run
set -uo pipefail
cd "$(dirname "$0")"

POLL_INTERVAL=${POLL_INTERVAL:-300}     # seconds between polls (5 min)
MAX_RUNTIME=${MAX_RUNTIME:-20400}       # total loop seconds (340 min, < 6h cap)
COMMIT_EVERY=${COMMIT_EVERY:-1800}      # min seconds between state checkpoints

in_ci() { [ "${GITHUB_ACTIONS:-}" = "true" ]; }   # skip git/gh when run locally

commit_state() {
  in_ci || return 0
  if [ -n "$(git status --porcelain state.json 2>/dev/null)" ]; then
    git add state.json \
      && git commit -q -m "Update state [skip ci]" \
      && git push -q || true
  fi
}

if in_ci; then
  git config user.name  "github-actions[bot]"
  git config user.email "github-actions[bot]@users.noreply.github.com"
fi

start=$(date +%s)
last_commit=0
while [ $(( $(date +%s) - start )) -lt "$MAX_RUNTIME" ]; do
  python3 monitor.py "$@" || true
  now=$(date +%s)
  if [ $(( now - last_commit )) -ge "$COMMIT_EVERY" ]; then
    commit_state
    last_commit=$now
  fi
  # Restart promptly if a real code/config change was pushed upstream, so edits
  # go live within ~one poll instead of waiting out the full MAX_RUNTIME. The
  # diff excludes state.json, so the loop's own state commits never trigger it.
  if in_ci; then
    git fetch -q origin main 2>/dev/null || true
    if ! git diff --quiet HEAD origin/main -- config.json monitor.py loop.sh \
         .github/workflows/monitor.yml 2>/dev/null; then
      echo "loop: upstream code/config changed — restarting to pick it up"
      break
    fi
  fi
  sleep "$POLL_INTERVAL"
done

commit_state                                          # final handoff commit
in_ci && (gh workflow run monitor.yml >/dev/null 2>&1 || true)   # chain next loop
exit 0
