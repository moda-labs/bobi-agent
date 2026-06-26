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

# The agent brain (#485). Decides the provider API key (api_key/subscription),
# the durable OAuth credential dir on the volume, and the credential file the
# first-boot subscription bootstrap waits for. Default claude for entrypoint
# branching only; do not export that default because agent.yaml must still be
# able to select a non-Claude brain when MODASTACK_BRAIN was not explicit.
ENTRYPOINT_BRAIN="${MODASTACK_BRAIN:-claude}"

configure_brain_paths() {
  case "$ENTRYPOINT_BRAIN" in
    codex)
      BRAIN_SHADOW_KEY="OPENAI_API_KEY"
      BRAIN_CRED_DIR="${DATA_DIR}/codex"      # ~/.codex symlinks here
      BRAIN_HOME_LINK="${HOME}/.codex"
      BRAIN_CRED_FILE="auth.json"
      ;;
    *)  # claude (default): durable state already lives under CLAUDE_CONFIG_DIR
      BRAIN_SHADOW_KEY="ANTHROPIC_API_KEY"
      BRAIN_CRED_DIR="${CLAUDE_CONFIG_DIR}"
      BRAIN_HOME_LINK="${HOME}/.claude"
      BRAIN_CRED_FILE=".credentials.json"
      ;;
  esac
}

validate_auth_mode() {
  # The provider API key is brain-specific (ANTHROPIC_API_KEY / OPENAI_API_KEY);
  # ${!BRAIN_SHADOW_KEY} is its live value via bash indirect expansion.
  case "${MODASTACK_AUTH:-api_key}" in
    api_key)
      [ -n "${!BRAIN_SHADOW_KEY:-}" ] \
        || fatal "MODASTACK_AUTH=api_key but ${BRAIN_SHADOW_KEY} is unset."
      ;;
    subscription)
      # The provider API key silently outranks subscription OAuth creds and bills
      # the API instead — it must be entirely absent in this mode (§6.1).
      [ -z "${!BRAIN_SHADOW_KEY:-}" ] \
        || fatal "MODASTACK_AUTH=subscription but ${BRAIN_SHADOW_KEY} is set; it overrides subscription auth. Unset it."
      ;;
    *)
      fatal "unknown MODASTACK_AUTH='${MODASTACK_AUTH}' (expected api_key|subscription)."
      ;;
  esac
}

resolve_configured_brain() {
  [ -z "${MODASTACK_BRAIN:-}" ] || return 0
  [ -f "${PROJECT_DIR}/.modastack/agent.yaml" ] || return 0

  local configured
  if configured="$(PROJECT_DIR="${PROJECT_DIR}" python - <<'PY' 2>/dev/null
import os
from pathlib import Path

from modastack.config import Config

print(Config.load(Path(os.environ["PROJECT_DIR"])).brain_kind or "", end="")
PY
  )" && [ -n "${configured}" ]; then
    ENTRYPOINT_BRAIN="${configured}"
  fi
}

resolve_configured_brain
configure_brain_paths

log() { echo "[entrypoint] $*"; }
fatal() { echo "[entrypoint] FATAL: $*" >&2; exit 1; }

materialize_codex_api_key_auth() {
  local cred_dir="$1"
  [ -n "${OPENAI_API_KEY:-}" ] || return 0
  log "Writing Codex API-key auth file from OPENAI_API_KEY"
  mkdir -p "${cred_dir}"
  CODEX_CRED_DIR="${cred_dir}" OPENAI_API_KEY="${OPENAI_API_KEY}" python - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["CODEX_CRED_DIR"]) / "auth.json"
path.write_text(json.dumps({"OPENAI_API_KEY": os.environ["OPENAI_API_KEY"]}) + "\n")
path.chmod(0o600)
PY
  chown -R "${APP_USER}:${APP_USER}" "${cred_dir}"
}

AUTH_VALIDATED=0
if [ -n "${MODASTACK_BRAIN:-}" ] \
   || [ -f "${PROJECT_DIR}/.modastack/agent.yaml" ] \
   || { [ -z "${MODASTACK_TEAM_URL:-}" ] && [ -z "${MODASTACK_TEAM:-}" ]; }; then
  validate_auth_mode
  AUTH_VALIDATED=1
fi

# --- 1. Prepare the volume (root) -------------------------------------------
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

# --- 1b. Make ~/.claude coincide with the durable volume config dir (C24) -----
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
as_app() {
  if [ -n "${MODASTACK_BRAIN:-}" ] || [ "${ENTRYPOINT_BRAIN}" != "claude" ]; then
    gosu "${APP_USER}" env "HOME=${HOME}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "MODASTACK_BRAIN=${ENTRYPOINT_BRAIN}" "$@"
  else
    gosu "${APP_USER}" env "HOME=${HOME}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "$@"
  fi
}

# --- 2. First boot: install a team if the volume has no agent (C9 hardens) ---
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
    # MODASTACK_TEAM must resolve to something the INSTANCE can see: a path on the
    # volume (e.g. an ssh-pushed /mnt/team) or a local package — there is NO team
    # registry baked into the image, so a bare name won't resolve. Fail LOUD with
    # the actionable alternative instead of letting `set -e` crash-loop the Fly
    # machine on a bare pipefail trace (C9/#339).
    if ! as_app modastack install "${MODASTACK_TEAM}" --non-interactive; then
      log "ERROR: couldn't install team '${MODASTACK_TEAM}'. The container has no"
      log "       team registry, so MODASTACK_TEAM only resolves a path/package the"
      log "       instance can already see. To deliver a PUBLISHED team, set"
      log "       MODASTACK_TEAM_URL=<https .tar.gz> instead; to deliver a LOCAL"
      log "       package, use 'modastack deploy <name>' (ssh-push, no team source"
      log "       on the instance). See DEPLOYMENT.md / DEPLOY_INTERFACE.md."
      exit 1
    fi
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

resolve_configured_brain
configure_brain_paths

# --- 3. Validate auth mode --------------------------------------------------
# First-boot team installs can define ``brain.kind`` in agent.yaml, so their
# shadow-key check may need to wait until after install. Blank/no-team boots and
# existing installs validate before the wait-for-team loop so auth mistakes fail
# fast instead of hanging as a blank instance.
if [ "${AUTH_VALIDATED}" != "1" ]; then
  validate_auth_mode
fi

# --- 3b. Codex's durable OAuth dir on the volume (#485) ---------------------
# Same idea for codex: ~/.codex (where `codex login`/`codex exec` keep auth.json)
# points at a volume dir so the ChatGPT subscription survives a redeploy. claude
# already gets this via CLAUDE_CONFIG_DIR above; codex has no config-dir override,
# so we symlink the home dir directly.
if [ "${ENTRYPOINT_BRAIN}" = "codex" ]; then
  mkdir -p "${BRAIN_CRED_DIR}"
  chown "${APP_USER}:${APP_USER}" "${BRAIN_CRED_DIR}"
  if [ -d /opt/modastack/skills ]; then
    log "Linking baked team skills into ${BRAIN_CRED_DIR}/skills for codex"
    mkdir -p "${BRAIN_CRED_DIR}/skills"
    chown "${APP_USER}:${APP_USER}" "${BRAIN_CRED_DIR}/skills"
    for existing_skill in "${BRAIN_CRED_DIR}/skills"/*; do
      [ -L "${existing_skill}" ] || continue
      existing_target="$(readlink "${existing_skill}")"
      case "${existing_target}" in
        /opt/modastack/skills/*)
          if [ ! -e "${existing_target}" ]; then
            log "Removing stale baked codex skill link ${existing_skill}"
            rm -f "${existing_skill}"
          fi
          ;;
      esac
    done
    for skill_path in /opt/modastack/skills/*; do
      [ -e "${skill_path}" ] || continue
      skill_name="$(basename "${skill_path}")"
      skill_dest="${BRAIN_CRED_DIR}/skills/${skill_name}"
      if [ -e "${skill_dest}" ] || [ -L "${skill_dest}" ]; then
        if [ -L "${skill_dest}" ]; then
          skill_dest_target="$(readlink "${skill_dest}")"
          case "${skill_dest_target}" in
            /opt/modastack/skills/*) ;;
            *)
              log "Leaving existing codex skill link at ${skill_dest}; baked skill ${skill_name} not linked"
              continue
              ;;
          esac
        else
          log "Leaving existing codex skill at ${skill_dest}; baked skill ${skill_name} not linked"
          continue
        fi
      fi
      ln -sfnT "${skill_path}" "${skill_dest}"
    done
  fi
  if [ "$(readlink "${BRAIN_HOME_LINK}" 2>/dev/null)" != "${BRAIN_CRED_DIR}" ]; then
    log "Pointing ${BRAIN_HOME_LINK} -> ${BRAIN_CRED_DIR} (durable codex creds on volume)"
    rm -rf "${BRAIN_HOME_LINK}"
    ln -s "${BRAIN_CRED_DIR}" "${BRAIN_HOME_LINK}"
  fi
fi

# The Codex CLI also exists as an auxiliary tool for Claude-brained teams
# (`tool_library: [codex]`). Unlike Claude, Codex does not read OPENAI_API_KEY
# directly; it expects ~/.codex/auth.json. In subscription mode, never turn an
# ambient API key into Codex auth: subscription OAuth must remain authoritative.
if [ "${MODASTACK_AUTH:-api_key}" != "subscription" ]; then
  if [ "${ENTRYPOINT_BRAIN}" = "codex" ]; then
    materialize_codex_api_key_auth "${BRAIN_CRED_DIR}"
  else
    materialize_codex_api_key_auth "${HOME}/.codex"
  fi
elif [ -n "${OPENAI_API_KEY:-}" ]; then
  log "Subscription mode: leaving OPENAI_API_KEY out of Codex auth materialization"
fi

# --- 4. Subscription auth: bootstrap login over Slack if no creds yet (C23) --
# Idempotent: a no-op once the credentials exist. They live under
# CLAUDE_CONFIG_DIR (the volume), not HOME — that's the durable state we keep.
if [ "${MODASTACK_AUTH:-api_key}" = "subscription" ] \
   && [ ! -f "${BRAIN_CRED_DIR}/${BRAIN_CRED_FILE}" ]; then
  log "Subscription mode, no ${ENTRYPOINT_BRAIN} credentials on volume — running login bootstrap"
  as_app modastack login-bootstrap
fi

# --- 5. Hand off to the manager as the non-root user ------------------------
# Agent UI on by default IN THE CONTAINER (the manager starts it on the private
# 6PN; reach it with `modastack ui <deployment>` / `fly proxy`). It's image
# behavior, not a per-instance flag, so existing instances pick it up on their
# next image swap. Disable with MODASTACK_UI=0 in the Fly env. The dark instance
# has no public route, so this exposes nothing — see DESIGN.md "Agent UI".
export MODASTACK_UI="${MODASTACK_UI:-1}"
log "Starting manager under self-heal watchdog (user=${APP_USER}, project=${PROJECT_DIR}, home=${HOME}, claude_config=${CLAUDE_CONFIG_DIR})"
# #464: launch the manager under `modastack supervise` instead of directly.
# The supervisor is the entrypoint process (parent); it spawns the manager as a
# child, watches the director's progress via the health endpoint, and restarts
# a wedged director from below — the one recovery layer stall-recovery cannot
# provide. It runs no agent loop, so it cannot wedge from the same cause; on
# restart-budget exhaustion it exits non-zero and Fly's machine restart policy
# escalates. `healthcheck.sh` is unaffected (the manager child still writes the
# port file). The forwarded `--foreground` keeps the manager a supervisable
# child rather than letting it daemonize.
if [ -n "${MODASTACK_BRAIN:-}" ] || [ "${ENTRYPOINT_BRAIN}" != "claude" ]; then
  exec gosu "${APP_USER}" env "HOME=${HOME}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "MODASTACK_BRAIN=${ENTRYPOINT_BRAIN}" "MODASTACK_UI=${MODASTACK_UI}" modastack supervise -- --foreground "$@"
else
  exec gosu "${APP_USER}" env "HOME=${HOME}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "MODASTACK_UI=${MODASTACK_UI}" modastack supervise -- --foreground "$@"
fi
