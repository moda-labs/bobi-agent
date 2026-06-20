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
# $HOME stays on the IMAGE (/home/modastack) — that's where baked team tools
# (~/.claude/skills, ~/dev/gstack) live, read in place, never copied. Only
# Claude's DURABLE state (credentials + transcripts + session history) is
# redirected onto the volume via CLAUDE_CONFIG_DIR, the supported override.
# Splitting the two this way (vs. seeding tools onto a volume HOME) keeps build
# HOME == runtime HOME, so the image's `verify: requires` proves the live paths.
export HOME="${MODASTACK_HOME:-/home/modastack}"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-${DATA_DIR}/claude}"

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
# Only the durable dirs live on the volume: the project root and Claude's config
# dir (CLAUDE_CONFIG_DIR). HOME is on the image and needs no volume prep.
mkdir -p "${PROJECT_DIR}" "${CLAUDE_CONFIG_DIR}"

# Fly/EC2/k8s mount fresh volumes owned by root. Take ownership once so the
# non-root user can write; a stamp keeps subsequent boots from re-walking a
# large, already-correct tree.
if [ ! -e "${DATA_DIR}/.modastack-owned" ]; then
  log "Taking ownership of ${DATA_DIR} for ${APP_USER} (first boot)"
  chown -R "${APP_USER}:${APP_USER}" "${DATA_DIR}"
  : > "${DATA_DIR}/.modastack-owned"
else
  chown "${APP_USER}:${APP_USER}" "${DATA_DIR}" "${PROJECT_DIR}" "${CLAUDE_CONFIG_DIR}"
fi

# --- 2b. Make ~/.claude coincide with the durable volume config dir (C24) -----
# $HOME stays on the image (baked tools read in place), but Claude's DURABLE
# state (creds, transcripts, settings) lives under CLAUDE_CONFIG_DIR on the
# volume. Point the whole ~/.claude AT that dir, so any tool/skill that hardcodes
# ~/.claude/{projects,settings.json,skills,…} sees Claude's real state — one
# coherent home tree, split underneath only by storage lifecycle (image vs
# volume), invisible to anything using `~`.
#
# Personal skills are baked OUTSIDE ~/.claude at /opt/modastack/skills (immutable
# image content; build-render.py put them there) and surfaced via the config
# dir's skills/ entry — a symlinked DIRECTORY is safe (files read inside it) and
# resolves to the exact path the build's `verify: requires` checked. No baked
# skills (generic image) → that link is skipped; Claude finds project skills.
if [ -d /opt/modastack/skills ]; then
  log "Linking ${CLAUDE_CONFIG_DIR}/skills -> /opt/modastack/skills (baked team skills)"
  ln -sfn /opt/modastack/skills "${CLAUDE_CONFIG_DIR}/skills"
fi
# Replace the image's real ~/.claude (created at build) with a symlink to the
# volume config dir. Idempotent: rewrite only when it isn't already that link
# (a fresh image rootfs each deploy ships the real dir; a same-machine restart
# already has the link). rm -rf only ever discards ephemeral image content —
# the durable state and the baked skills both live elsewhere.
if [ "$(readlink "${HOME}/.claude" 2>/dev/null)" != "${CLAUDE_CONFIG_DIR}" ]; then
  log "Pointing ${HOME}/.claude -> ${CLAUDE_CONFIG_DIR} (durable config on volume)"
  rm -rf "${HOME}/.claude"
  ln -s "${CLAUDE_CONFIG_DIR}" "${HOME}/.claude"
fi

cd "${PROJECT_DIR}"

# Carry HOME (the image home — gosu would otherwise reset it from passwd, which
# is the same path here, but be explicit) and CLAUDE_CONFIG_DIR (the volume dir
# holding durable creds/transcripts) into every privilege drop.
as_app() { gosu "${APP_USER}" env "HOME=${HOME}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "$@"; }

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
# Idempotent: a no-op once the credentials exist. They live under
# CLAUDE_CONFIG_DIR (the volume), not HOME — that's the durable state we keep.
if [ "${MODASTACK_AUTH:-api_key}" = "subscription" ] \
   && [ ! -f "${CLAUDE_CONFIG_DIR}/.credentials.json" ]; then
  log "Subscription mode, no credentials on volume — running login bootstrap"
  as_app modastack login-bootstrap
fi

# --- 5. Hand off to the manager as the non-root user ------------------------
log "Starting manager (user=${APP_USER}, project=${PROJECT_DIR}, home=${HOME}, claude_config=${CLAUDE_CONFIG_DIR})"
exec gosu "${APP_USER}" env "HOME=${HOME}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" modastack start --foreground "$@"
