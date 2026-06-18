#!/usr/bin/env bash
#
# modastack container entrypoint (containerized-8 / #338).
#
# Runs as root under tini (PID 1) only long enough to prepare the mounted
# volume, then drops to the non-root `modastack` user and exec's the manager.
# Because the final step is `exec gosu ... modastack start --foreground`, tini's
# SIGTERM is forwarded straight to the manager, which shuts sessions down
# gracefully (C2).
#
# First-boot install (empty volume -> `modastack install`) lives here for now so
# the image is independently testable; #339 (C9) hardens the idempotency and
# edge cases of this path.
set -euo pipefail

APP_USER="modastack"
DATA_DIR="${DATA_DIR:-/data}"
PROJECT_DIR="${MODASTACK_PROJECT:-${DATA_DIR}/project}"
# The agent's $HOME lives on the volume so ~/.claude (credentials + transcripts,
# required for session resume) and any caches persist across image updates (§3).
export HOME="${MODASTACK_HOME:-${DATA_DIR}/home}"

log() { echo "[entrypoint] $*"; }
fatal() { echo "[entrypoint] FATAL: $*" >&2; exit 1; }

# --- 1. Validate auth mode BEFORE touching the volume (§6.1) -----------------
case "${MODASTACK_AUTH:-api_key}" in
  api_key)
    [ -n "${ANTHROPIC_API_KEY:-}" ] \
      || fatal "MODASTACK_AUTH=api_key but ANTHROPIC_API_KEY is unset."
    ;;
  subscription)
    # ANTHROPIC_API_KEY silently outranks subscription OAuth creds and bills the
    # API instead — it must be entirely absent in this mode (§6.1).
    [ -z "${ANTHROPIC_API_KEY:-}" ] \
      || fatal "MODASTACK_AUTH=subscription but ANTHROPIC_API_KEY is set; it overrides subscription auth. Unset it."
    ;;
  *)
    fatal "unknown MODASTACK_AUTH='${MODASTACK_AUTH}' (expected api_key|subscription)."
    ;;
esac

# --- 2. Prepare the volume (root) -------------------------------------------
mkdir -p "${PROJECT_DIR}" "${HOME}"

# Fly/EC2/k8s mount fresh volumes owned by root. Take ownership once so the
# non-root user can write; a stamp keeps subsequent boots from re-walking a
# large, already-correct tree.
if [ ! -e "${DATA_DIR}/.modastack-owned" ]; then
  log "Taking ownership of ${DATA_DIR} for ${APP_USER} (first boot)"
  chown -R "${APP_USER}:${APP_USER}" "${DATA_DIR}"
  : > "${DATA_DIR}/.modastack-owned"
else
  chown "${APP_USER}:${APP_USER}" "${DATA_DIR}" "${PROJECT_DIR}" "${HOME}"
fi

cd "${PROJECT_DIR}"

# gosu resets HOME to the target user's passwd home (/home/modastack), which
# would send the agent's ~/.claude (subscription creds + transcripts) off the
# volume. Re-assert the volume HOME inside every privilege drop with `env`.
as_app() { gosu "${APP_USER}" env "HOME=${HOME}" "$@"; }

# --- 3. First boot: install a team if the volume has no agent (C9 hardens) ---
if [ ! -f "${PROJECT_DIR}/.modastack/agent.yaml" ]; then
  [ -n "${MODASTACK_TEAM:-}" ] \
    || fatal "empty volume and MODASTACK_TEAM is unset — nothing to install."
  log "First boot: installing team '${MODASTACK_TEAM}' (non-interactive)"
  as_app modastack install "${MODASTACK_TEAM}" --non-interactive
fi

# --- 4. Subscription auth: bootstrap login over Slack if no creds yet (C23) --
# Idempotent: a no-op once ~/.claude/.credentials.json exists on the volume.
if [ "${MODASTACK_AUTH:-api_key}" = "subscription" ] \
   && [ ! -f "${HOME}/.claude/.credentials.json" ]; then
  log "Subscription mode, no credentials on volume — running login bootstrap"
  as_app modastack login-bootstrap
fi

# --- 5. Hand off to the manager as the non-root user ------------------------
log "Starting manager (user=${APP_USER}, project=${PROJECT_DIR}, home=${HOME})"
exec gosu "${APP_USER}" env "HOME=${HOME}" modastack start --foreground "$@"
