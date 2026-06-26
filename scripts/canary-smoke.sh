#!/usr/bin/env bash
# Functional canary gate (the release smoke). Assert <app> answers CANARY-OK
# end-to-end through the prod event server on the freshly-deployed wheel.
#
# Robust to a cold image-swap boot. `fly deploy` only waits for the machine
# HEALTHCHECK — the manager health server, which comes up BEFORE the Claude
# session is ready — and the canary auto-suspends when idle. So the functional
# ask races first boot (volume ownership + team install + session spin-up),
# which can take a couple of minutes. We start the machine up-front so the boot
# isn't on the first ask's clock, then poll the ask with a generous, bounded
# budget. A genuinely broken wheel never answers and still fails the gate.
#
# Knobs (env, defaulted for CI; overridden by tests for speed):
#   CANARY_SMOKE_MAX_WAIT     total seconds to wait for readiness (default 300)
#   CANARY_SMOKE_INTERVAL     seconds between attempts            (default 15)
#   CANARY_SMOKE_ASK_TIMEOUT  per-ask reply wait, seconds         (default 180)
#   FLY                       fly/flyctl binary (auto-detected if unset)
set -euo pipefail

app="${1:?usage: canary-smoke.sh <app>}"

# `fly` is the current binary name; some flyctl installs only expose `flyctl`.
# Detect either (same pattern as deploy.py / fleet.sh).
FLY="${FLY:-$(command -v fly || command -v flyctl)}" \
  || { echo "::error::neither 'fly' nor 'flyctl' found on PATH" >&2; exit 1; }

max_wait="${CANARY_SMOKE_MAX_WAIT:-300}"
interval="${CANARY_SMOKE_INTERVAL:-15}"
ask_timeout="${CANARY_SMOKE_ASK_TIMEOUT:-180}"

ask="gosu bobi env HOME=/data/home bash -c \"cd /data/project && /opt/venv/bin/bobi ask \\\"Release canary smoke: reply with exactly CANARY-OK and nothing else.\\\" --timeout ${ask_timeout}\""

# Boot off the clock — the machine may be auto-stopped/suspended, and waking it
# on the first ask attempt is what used to burn the whole (too-small) budget.
"$FLY" machine start -a "$app" >/dev/null 2>&1 || true

deadline=$(( SECONDS + max_wait ))
attempt=0
while :; do
  attempt=$(( attempt + 1 ))
  resp="$("$FLY" ssh console -a "$app" -C "$ask" 2>&1 || true)"
  if printf '%s' "$resp" | grep -q "CANARY-OK"; then
    echo "Canary smoke passed — $app answered CANARY-OK on the wheel (attempt ${attempt})."
    exit 0
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "::error::canary smoke FAILED — $app did not answer CANARY-OK within ${max_wait}s; aborting release." >&2
    exit 1
  fi
  echo "canary not ready (attempt ${attempt}) — retrying in ${interval}s"
  sleep "$interval"
done
