#!/usr/bin/env bash
#
# modastack container entrypoint (containerized-8 / #338).
#
# Runs as root (PID 1, under Fly's injected init — no tini; see Dockerfile) only
# long enough to prepare the mounted volume, then drops to the non-root
# `modastack` user and exec's the manager. Because the final step is
# `exec gosu ... modastack start --foreground`, SIGTERM is forwarded straight to
# the manager, which shuts sessions down gracefully (C2).
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

# --- 2b. Seed baked team tools onto the volume HOME (C24) --------------------
# A team-flavored image (modastack/build_render.py) bakes host tools into a seed
# dir. The agent's runtime $HOME is the VOLUME (/data/home), so ~-relative tools
# (e.g. gstack's ~/.claude/skills) would be shadowed by the mount — copy the
# seed onto the volume HOME. Gated on a content stamp so a re-seed happens ONLY
# when the image's tools change (a deps bump); an ordinary boot is a no-op. The
# seed holds tool files only, so creds/transcripts already on the volume survive.
SEED_DIR="/opt/modastack/home-seed"
SEED_STAMP="${SEED_DIR}/.modastack-tool-stamp"
HOME_STAMP="${HOME}/.modastack-tool-stamp"
if [ -f "${SEED_STAMP}" ]; then
  if [ "$(cat "${SEED_STAMP}")" != "$(cat "${HOME_STAMP}" 2>/dev/null || true)" ]; then
    log "Seeding baked team tools onto ${HOME} (tool stamp changed)"
    cp -a "${SEED_DIR}/." "${HOME}/"
    chown -R "${APP_USER}:${APP_USER}" "${HOME}"
  else
    log "Baked team tools already current on volume — skipping seed"
  fi
fi

cd "${PROJECT_DIR}"

# gosu resets HOME to the target user's passwd home (/home/modastack), which
# would send the agent's ~/.claude (subscription creds + transcripts) off the
# volume. Re-assert the volume HOME inside every privilege drop with `env`.
as_app() { gosu "${APP_USER}" env "HOME=${HOME}" "$@"; }

# --- 3. First boot: install a team if the volume has no agent (C9 hardens) ---
# Team source precedence: a public MODASTACK_TEAM_URL (fetched at boot — the
# dark instance reaches out, nothing reaches in) wins over MODASTACK_TEAM (a
# bundled/registry name).
#
# With NEITHER set on an empty volume the instance enters the "wait for team"
# state instead of crashing: it was provisioned blank for ssh-push delivery
# (`modastack deploy` with a local `team:` package — DEPLOY_INTERFACE.md). The
# operator pushes the team over `fly ssh` (sftp the tarball + `modastack install`),
# which lands .modastack/agent.yaml on the volume; we poll for it, then start.
# This is the single-developer "I built it, ship it — no hosting" path, and it
# keeps PID 1 alive so the Fly machine stays "started" while we wait.
if [ ! -f "${PROJECT_DIR}/.modastack/agent.yaml" ]; then
  if [ -n "${MODASTACK_TEAM_URL:-}" ]; then
    log "First boot: installing team from URL ${MODASTACK_TEAM_URL} (non-interactive)"
    as_app modastack install "${MODASTACK_TEAM_URL}" --non-interactive
  elif [ -n "${MODASTACK_TEAM:-}" ]; then
    log "First boot: installing team '${MODASTACK_TEAM}' (non-interactive)"
    as_app modastack install "${MODASTACK_TEAM}" --non-interactive
  else
    log "No team source and empty volume — blank instance, waiting for an"
    log "ssh-push team delivery (modastack deploy). Poll for .modastack/agent.yaml..."
    waited=0
    while [ ! -f "${PROJECT_DIR}/.modastack/agent.yaml" ]; do
      sleep 2
      waited=$((waited + 2))
      # Heartbeat every ~2 min so `fly logs` shows the instance is alive, not hung.
      if [ $((waited % 120)) -eq 0 ]; then
        log "Still waiting for a pushed team (${waited}s)..."
      fi
    done
    log "Team appeared on the volume after ${waited}s — proceeding to start."
  fi
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
