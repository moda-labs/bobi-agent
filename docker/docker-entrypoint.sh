#!/usr/bin/env bash
#
# bobi container entrypoint (containerized-8 / #338).
#
# Runs as root (PID 1, under Fly's injected init — no tini; see Dockerfile) only
# long enough to prepare the mounted volume, then drops to the non-root
# `bobi` user and exec's the manager. Because the final step is
# `exec gosu ... bobi agent <name> supervise -- --foreground`, SIGTERM is forwarded to the
# selected agent manager, which shuts sessions down gracefully (C2).
#
# First-boot install (empty volume -> `bobi agents install`) lives here for now so
# the image is independently testable; #339 (C9) hardens the idempotency and
# edge cases of this path.
set -euo pipefail

# shellcheck disable=SC2034  # Read remotely by the deploy migration probe.
BOBI_GATEWAY_AUTH_CONTRACT_VERSION="1"
APP_USER="bobi"
DATA_DIR="${DATA_DIR:-/data}"
log() { echo "[entrypoint] $*"; }
fatal() { echo "[entrypoint] FATAL: $*" >&2; exit 1; }
# $HOME stays on the IMAGE (/home/bobi) — that's where baked team tools
# (~/.claude/skills, ~/dev/gstack) live, read in place, never copied. Only
# Claude's DURABLE state (credentials + transcripts + session history) is
# redirected onto the volume via CLAUDE_CONFIG_DIR, the supported override.
# Splitting the two this way (vs. seeding tools onto a volume HOME) keeps build
# HOME == runtime HOME, so the image's `verify: requires` proves the live paths.
export HOME="${HOME:-/home/bobi}"
[ -n "${BOBI_HOME:-}" ] || fatal "BOBI_HOME is required. The image sets BOBI_HOME=/data/.bobi."
export BOBI_HOME
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-${DATA_DIR}/claude}"

if [ -n "${BOBI_AGENT:-}" ]; then
  AGENT_NAME="${BOBI_AGENT}"
elif [ -n "${BOBI_INSTANCE:-}" ]; then
  AGENT_NAME="${BOBI_INSTANCE}"
elif [ -n "${BOBI_ROOT:-}" ] && [ "$(basename "${BOBI_ROOT}")" = "run" ]; then
  AGENT_NAME="$(basename "$(dirname "${BOBI_ROOT}")")"
else
  fatal "No Bobi Agent selected. Set BOBI_INSTANCE, BOBI_AGENT, or BOBI_ROOT."
fi
RUN_ROOT="${BOBI_ROOT:-${BOBI_HOME}/agents/${AGENT_NAME}/run}"
export BOBI_ROOT="${RUN_ROOT}"
PACKAGE_DIR="${RUN_ROOT}/package"

# The agent brain (#485). Decides the provider API key (api_key/subscription),
# the durable OAuth credential dir on the volume, and the credential file the
# first-boot subscription bootstrap waits for. Default claude for entrypoint
# branching only; do not export that default because agent.yaml must still be
# able to select a non-Claude brain when BOBI_BRAIN was not explicit.
ENTRYPOINT_BRAIN="${BOBI_BRAIN:-claude}"
case "${BOBI_GATEWAY:-}" in
  1) ENTRYPOINT_IS_GATEWAY="1" ;;
  0)
    case "${BOBI_BRAIN:-}" in
      gateway|gateway-openai)
        fatal "BOBI_GATEWAY=0 conflicts with legacy gateway brain '${BOBI_BRAIN}'."
        ;;
      *) ENTRYPOINT_IS_GATEWAY="0" ;;
    esac
    ;;
  "")
    case "${BOBI_BRAIN:-}" in
      gateway|gateway-openai) ENTRYPOINT_IS_GATEWAY="1" ;;
      *) ENTRYPOINT_IS_GATEWAY="0" ;;
    esac
    ;;
  *) fatal "BOBI_GATEWAY must be 0 or 1 (got '${BOBI_GATEWAY}')." ;;
esac

entrypoint_engine() {
  case "$1" in
    ""|claude|gateway) printf "claude" ;;
    codex|gateway-openai) printf "codex" ;;
    *) printf "%s" "$1" ;;
  esac
}

configure_brain_paths() {
  ENTRYPOINT_ENGINE="$(entrypoint_engine "${ENTRYPOINT_BRAIN}")"
  case "$ENTRYPOINT_ENGINE" in
    codex)
      BRAIN_SHADOW_KEY="OPENAI_API_KEY"
      if [ "${ENTRYPOINT_IS_GATEWAY}" = "1" ]; then
        BRAIN_AUTH_TOKEN_KEY="BOBI_GATEWAY_API_KEY"
      else
        BRAIN_AUTH_TOKEN_KEY=""
      fi
      BRAIN_CRED_DIR="${DATA_DIR}/codex"      # ~/.codex symlinks here
      BRAIN_HOME_LINK="${HOME}/.codex"
      BRAIN_CRED_FILE="auth.json"
      ;;
    *)  # claude (default): durable state already lives under CLAUDE_CONFIG_DIR
      BRAIN_SHADOW_KEY="ANTHROPIC_API_KEY"
      if [ "${ENTRYPOINT_IS_GATEWAY}" = "1" ]; then
        BRAIN_AUTH_TOKEN_KEY="ANTHROPIC_AUTH_TOKEN"
      else
        BRAIN_AUTH_TOKEN_KEY=""
      fi
      BRAIN_CRED_DIR="${CLAUDE_CONFIG_DIR}"
      BRAIN_HOME_LINK="${HOME}/.claude"
      BRAIN_CRED_FILE=".credentials.json"
      ;;
  esac
}

has_api_key_credential() {
  [ -n "${!BRAIN_SHADOW_KEY:-}" ] && return 0
  [ -n "${BRAIN_AUTH_TOKEN_KEY}" ] || return 1
  [ -n "${!BRAIN_AUTH_TOKEN_KEY:-}" ]
}

validate_auth_mode() {
  # The provider API key is brain-specific (ANTHROPIC_API_KEY / OPENAI_API_KEY);
  # ${!BRAIN_SHADOW_KEY} is its live value via bash indirect expansion. Claude
  # gateways authenticate with ANTHROPIC_AUTH_TOKEN instead and deliberately
  # blank ANTHROPIC_API_KEY before launching each session. Their credentials are
  # optional because some endpoints, such as local Ollama, serve without one.
  case "${BOBI_AUTH:-api_key}" in
    api_key)
      if [ "${ENTRYPOINT_ENGINE}" = "claude" ] \
         && [ "${ENTRYPOINT_IS_GATEWAY}" != "1" ] \
         && [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
        fatal "ANTHROPIC_AUTH_TOKEN is only valid for a configured Claude gateway; unset it for native Claude."
      elif [ "${ENTRYPOINT_IS_GATEWAY}" = "1" ] \
         && [ "${ENTRYPOINT_ENGINE}" = "claude" ]; then
        if [ -z "${!BRAIN_AUTH_TOKEN_KEY:-}" ]; then
          [ -z "${!BRAIN_SHADOW_KEY:-}" ] \
            || fatal "Claude gateways ignore ${BRAIN_SHADOW_KEY}; set ${BRAIN_AUTH_TOKEN_KEY}, or unset ${BRAIN_SHADOW_KEY} for an unauthenticated gateway."
          [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
            || fatal "Claude gateway has no ${BRAIN_AUTH_TOKEN_KEY}, but CLAUDE_CODE_OAUTH_TOKEN is set; set ${BRAIN_AUTH_TOKEN_KEY} or unset it."
          [ ! -f "${BRAIN_CRED_DIR}/${BRAIN_CRED_FILE}" ] \
            || fatal "Claude gateway has no ${BRAIN_AUTH_TOKEN_KEY}, but stored subscription credentials exist at ${BRAIN_CRED_DIR}/${BRAIN_CRED_FILE}; set ${BRAIN_AUTH_TOKEN_KEY} or remove that file."
        fi
      elif [ "${ENTRYPOINT_IS_GATEWAY}" = "1" ] \
           && [ "${ENTRYPOINT_ENGINE}" = "codex" ]; then
        [ -n "${!BRAIN_AUTH_TOKEN_KEY:-}" ] \
          || fatal "BOBI_AUTH=api_key but ${BRAIN_AUTH_TOKEN_KEY} is unset for the Codex gateway."
      elif ! has_api_key_credential; then
        fatal "BOBI_AUTH=api_key but ${BRAIN_SHADOW_KEY} is unset."
      fi
      ;;
    subscription)
      # Provider credentials silently outrank subscription OAuth creds, so they
      # must be entirely absent in this mode (§6.1).
      [ -z "${!BRAIN_SHADOW_KEY:-}" ] \
        || fatal "BOBI_AUTH=subscription but ${BRAIN_SHADOW_KEY} is set; it overrides subscription auth. Unset it."
      if [ "${ENTRYPOINT_ENGINE}" = "claude" ] \
         && [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
        fatal "BOBI_AUTH=subscription but ANTHROPIC_AUTH_TOKEN is set; it overrides subscription auth. Unset it."
      elif [ -n "${BRAIN_AUTH_TOKEN_KEY}" ] \
           && [ -n "${!BRAIN_AUTH_TOKEN_KEY:-}" ]; then
        fatal "BOBI_AUTH=subscription but ${BRAIN_AUTH_TOKEN_KEY} is set; it overrides subscription auth. Unset it."
      fi
      if [ "${ENTRYPOINT_IS_GATEWAY}" = "1" ] \
         && [ "${ENTRYPOINT_ENGINE}" != "claude" ]; then
        fatal "BOBI_AUTH=subscription applies only to Claude gateways; use BOBI_AUTH=api_key for Codex gateways."
      fi
      ;;
    *)
      fatal "unknown BOBI_AUTH='${BOBI_AUTH}' (expected api_key|subscription)."
      ;;
  esac
}

is_pending_team_auth_resolution() {
  [ "${BOBI_AUTH:-api_key}" = "api_key" ] || return 1
  [ -z "${BOBI_GATEWAY:-}" ] || return 1
  [ "${ENTRYPOINT_IS_GATEWAY}" != "1" ] || return 1
  case "${ENTRYPOINT_ENGINE}" in
    claude)
      [ -z "${ANTHROPIC_API_KEY:-}" ] \
        || [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]
      ;;
    codex)
      [ -z "${OPENAI_API_KEY:-}" ] \
        && [ -n "${BOBI_GATEWAY_API_KEY:-}" ]
      ;;
    *)
      return 1
      ;;
  esac
}

validate_auth_before_install() {
  if [ -f "${PACKAGE_DIR}/agent.yaml" ]; then
    validate_auth_mode
  elif [ -n "${BOBI_BRAIN:-}" ] \
       || { [ -z "${BOBI_TEAM_URL:-}" ] && [ -z "${BOBI_TEAM:-}" ]; }; then
    # A boot whose credentials fit a gateway may be waiting for a team whose
    # base URL is not knowable until ssh-push installs agent.yaml. Defer that
    # credential decision; cases that cannot describe an incoming gateway
    # validate now.
    is_pending_team_auth_resolution || validate_auth_mode
  fi
}

resolve_configured_brain() {
  [ -f "${PACKAGE_DIR}/agent.yaml" ] || return 0

  local configured configured_brain configured_engine configured_gateway encoded
  local configured_endpoint configured_legacy effective_engine
  local -a configured_fields
  if ! configured="$(BOBI_ROOT="${RUN_ROOT}" python - <<'PY' 2>/dev/null
import base64
import hashlib
import os
from pathlib import Path

from bobi.config import Config

config = Config.load(Path(os.environ["BOBI_ROOT"]))
configured_brain = config.brain_kind or ""
# Explicit pypi image pins may still select bobi 0.43-0.45, before
# brain_is_gateway existed. Those releases understand the alias kinds but not
# canonical engine + base_url, so preserve exactly that older classification.
has_gateway_identity = hasattr(config, "brain_is_gateway")
configured_gateway = getattr(
    config,
    "brain_is_gateway",
    configured_brain in {"gateway", "gateway-openai"},
)
brain_config = getattr(config, "brain", {}) or {}
configured_base_url = getattr(
    config,
    "brain_base_url",
    str(brain_config.get("base_url", "") or ""),
)
configured_endpoint = (
    hashlib.sha256(configured_base_url.encode()).hexdigest()
    if configured_gateway and configured_base_url
    else ""
)
for field in (
    configured_brain,
    "1" if configured_gateway else "0",
    configured_endpoint,
    "0" if has_gateway_identity else "1",
):
    print(base64.b64encode(field.encode()).decode())
PY
  )"; then
    fatal "Could not resolve the installed team's brain and gateway identity."
  fi
  mapfile -t configured_fields <<< "${configured}"
  [ "${#configured_fields[@]}" -eq 4 ] \
    || fatal "Could not parse the installed team's brain and gateway identity."
  for encoded in "${configured_fields[@]}"; do
    [[ "${encoded}" =~ ^[A-Za-z0-9+/]*={0,2}$ ]] \
      || fatal "Could not parse the installed team's brain and gateway identity."
  done
  configured_brain="$(printf '%s' "${configured_fields[0]}" | base64 --decode)" \
    || fatal "Could not parse the installed team's brain and gateway identity."
  configured_gateway="$(printf '%s' "${configured_fields[1]}" | base64 --decode)" \
    || fatal "Could not parse the installed team's brain and gateway identity."
  configured_endpoint="$(printf '%s' "${configured_fields[2]}" | base64 --decode)" \
    || fatal "Could not parse the installed team's brain and gateway identity."
  configured_legacy="$(printf '%s' "${configured_fields[3]}" | base64 --decode)" \
    || fatal "Could not parse the installed team's brain and gateway identity."
  if [ -n "${configured_brain}" ] \
     && [[ ! "${configured_brain}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
    fatal "Invalid installed team brain identity."
  fi
  case "${configured_gateway}" in
    0|1) ;;
    *) fatal "Could not parse the installed team's gateway identity." ;;
  esac
  if [ -n "${configured_endpoint}" ] \
     && [[ ! "${configured_endpoint}" =~ ^[0-9a-f]{64}$ ]]; then
    fatal "Could not parse the installed team's gateway endpoint."
  fi
  case "${configured_legacy}" in
    0|1) ;;
    *) fatal "Could not parse the installed team's legacy identity." ;;
  esac
  if [ -z "${BOBI_BRAIN:-}" ] && [ -n "${configured_brain}" ]; then
    ENTRYPOINT_BRAIN="${configured_brain}"
  fi
  configured_engine="$(entrypoint_engine "${configured_brain}")"
  effective_engine="$(entrypoint_engine "${ENTRYPOINT_BRAIN}")"
  if [ -n "${BOBI_GATEWAY:-}" ] \
     && [ "${configured_gateway}" != "${BOBI_GATEWAY}" ]; then
    fatal "BOBI_GATEWAY=${BOBI_GATEWAY} conflicts with the installed team's gateway identity (${configured_gateway})."
  fi
  if [ -n "${BOBI_GATEWAY_ENDPOINT_SHA256:-}" ] \
     && [ "${configured_endpoint}" != "${BOBI_GATEWAY_ENDPOINT_SHA256}" ]; then
    fatal "BOBI_GATEWAY_ENDPOINT_SHA256 conflicts with the installed team's gateway endpoint."
  fi
  # Old runtimes select gateways by their alias kind. The provisioner now
  # stamps canonical engine identity, so restore the installed alias when the
  # old Config shape proves that canonical endpoint identity is unavailable.
  if [ "${configured_legacy}" = "1" ] \
     && [ "${configured_gateway}" = "1" ] \
     && [ "${configured_engine}" = "${effective_engine}" ]; then
    case "${configured_brain}" in
      gateway|gateway-openai) ENTRYPOINT_BRAIN="${configured_brain}" ;;
    esac
  fi
  case "${BOBI_BRAIN:-}" in
    gateway|gateway-openai)
      ENTRYPOINT_IS_GATEWAY="1"
      ;;
    *)
      if { [ "${configured_engine}" = "claude" ] \
           || [ "${configured_engine}" = "codex" ]; } \
         && [ "${configured_gateway}" = "1" ] \
         && [ "${configured_engine}" = "${effective_engine}" ]; then
        ENTRYPOINT_IS_GATEWAY="1"
      else
        ENTRYPOINT_IS_GATEWAY="0"
      fi
      ;;
  esac
}

resolve_configured_brain
configure_brain_paths

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

codex_auth_uses_api_key() {
  local cred_dir="$1"
  CODEX_CRED_DIR="${cred_dir}" python - <<'PY'
import json
import os
import sys
from pathlib import Path

try:
    data = json.loads((Path(os.environ["CODEX_CRED_DIR"]) / "auth.json").read_text())
except Exception:
    sys.exit(1)
# API-key auth ONLY: a real OPENAI_API_KEY value AND no OAuth tokens. A codex
# `login --device-auth` file carries an OPENAI_API_KEY field (null) ALONGSIDE
# `tokens`, so a bare `"OPENAI_API_KEY" in data` misreads valid OAuth as an
# API-key file and wipes it every boot (re-posting a device-login each time).
sys.exit(0 if isinstance(data, dict) and data.get("OPENAI_API_KEY")
         and not data.get("tokens") else 1)
PY
}

validate_auth_before_install

# --- 1. Prepare the volume (root) -------------------------------------------
# Only durable state lives on the volume: BOBI_HOME, the selected run root, and
# Claude's config dir (CLAUDE_CONFIG_DIR). HOME is on the image and needs no
# volume prep.
mkdir -p "${BOBI_HOME}" "${RUN_ROOT}" "${RUN_ROOT}/workspace" "${CLAUDE_CONFIG_DIR}" \
  "${HF_HOME:-${BOBI_HOME}/cache/huggingface}" \
  "${FASTEMBED_CACHE_PATH:-${BOBI_HOME}/cache/fastembed}"

# Fly/EC2/k8s mount fresh volumes owned by root. Take ownership once so the
# non-root user can write; a stamp keeps subsequent boots from re-walking a
# large, already-correct tree.
if [ ! -e "${DATA_DIR}/.bobi-owned" ]; then
  log "Taking ownership of ${DATA_DIR} for ${APP_USER} (first boot)"
  chown -R "${APP_USER}:${APP_USER}" "${DATA_DIR}"
  : > "${DATA_DIR}/.bobi-owned"
else
  chown "${APP_USER}:${APP_USER}" "${DATA_DIR}" "${BOBI_HOME}" "${RUN_ROOT}" \
    "${CLAUDE_CONFIG_DIR}" \
    "${HF_HOME:-${BOBI_HOME}/cache/huggingface}" \
    "${FASTEMBED_CACHE_PATH:-${BOBI_HOME}/cache/fastembed}"
fi

# --- 1b. Make ~/.claude coincide with the durable volume config dir (C24) -----
# $HOME stays on the image (baked tools read in place), but Claude's DURABLE
# state (creds, transcripts, settings) lives under CLAUDE_CONFIG_DIR on the
# volume. Point the whole ~/.claude AT that dir, so any tool/skill that hardcodes
# ~/.claude/{projects,settings.json,skills,…} sees Claude's real state — one
# coherent home tree, split underneath only by storage lifecycle (image vs
# volume), invisible to anything using `~`.
#
# Personal skills are baked OUTSIDE ~/.claude at /opt/bobi/skills (immutable
# image content; build-render.py put them there) and surfaced via the config
# dir's skills/ entry — a symlinked DIRECTORY is safe (files read inside it) and
# resolves to the exact path the build's `verify: requires` checked. No baked
# skills (generic image) → that link is skipped; Claude finds project skills.
if [ -d /opt/bobi/skills ]; then
  log "Linking ${CLAUDE_CONFIG_DIR}/skills -> /opt/bobi/skills (baked team skills)"
  ln -sfn /opt/bobi/skills "${CLAUDE_CONFIG_DIR}/skills"
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

cd "${RUN_ROOT}"

# Carry HOME (the image home), BOBI_HOME/BOBI_ROOT (the durable selected Bobi
# runtime), and CLAUDE_CONFIG_DIR (the volume dir holding durable
# creds/transcripts) into every privilege drop.
as_app() {
  if [ -n "${BOBI_BRAIN:-}" ] || [ "${ENTRYPOINT_BRAIN}" != "claude" ]; then
    gosu "${APP_USER}" env "HOME=${HOME}" "BOBI_HOME=${BOBI_HOME}" "BOBI_ROOT=${BOBI_ROOT}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "BOBI_BRAIN=${ENTRYPOINT_BRAIN}" "$@"
  else
    gosu "${APP_USER}" env "HOME=${HOME}" "BOBI_HOME=${BOBI_HOME}" "BOBI_ROOT=${BOBI_ROOT}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "$@"
  fi
}

# --- 2. First boot: install a team if the volume has no agent (C9 hardens) ---
# Team source precedence: a public BOBI_TEAM_URL (fetched at boot — the
# dark instance reaches out, nothing reaches in) wins over BOBI_TEAM (a
# bundled/registry name).
#
# With NEITHER set on an empty volume the instance enters the "wait for team"
# state instead of crashing: it was provisioned blank for ssh-push delivery
# (`bobi deploy` with a local `team:` package - docs/CONTAINERIZED_DEPLOYMENT.md). The
# operator pushes the team over `fly ssh` (sftp the tarball + `bobi agents install`),
# which lands run/package/agent.yaml on the volume; we poll for it, then start.
# This is the single-developer "I built it, ship it — no hosting" path, and it
# keeps PID 1 alive so the orchestrator still sees the container running
# while we wait.
if [ ! -f "${PACKAGE_DIR}/agent.yaml" ]; then
  if [ -n "${BOBI_TEAM_URL:-}" ]; then
    log "First boot: installing team from URL ${BOBI_TEAM_URL} (non-interactive)"
    as_app bobi agents install "${BOBI_TEAM_URL}" --name "${AGENT_NAME}" --non-interactive
  elif [ -n "${BOBI_TEAM:-}" ]; then
    log "First boot: installing team '${BOBI_TEAM}' (non-interactive)"
    # BOBI_TEAM must resolve to something the INSTANCE can see: a path on the
    # volume (e.g. an ssh-pushed /mnt/team) or a local package — there is NO team
    # registry baked into the image, so a bare name won't resolve. Fail LOUD with
    # the actionable alternative instead of letting `set -e` crash-loop the Fly
    # machine on a bare pipefail trace (C9/#339).
    if ! as_app bobi agents install "${BOBI_TEAM}" --name "${AGENT_NAME}" --non-interactive; then
      log "ERROR: couldn't install team '${BOBI_TEAM}'. The container has no"
      log "       team registry, so BOBI_TEAM only resolves a path/package the"
      log "       instance can already see. To deliver a PUBLISHED team, set"
      log "       BOBI_TEAM_URL=<https .tar.gz> instead; to deliver a LOCAL"
      log "       package, use 'bobi deploy <name>' (ssh-push, no team source"
      log "       on the instance). See docs/CONTAINERIZED_DEPLOYMENT.md."
      exit 1
    fi
  else
    log "No team source and empty volume — blank instance, waiting for an"
    log "ssh-push team delivery (bobi deploy). Poll for ${PACKAGE_DIR}/agent.yaml..."
    waited=0
    while [ ! -f "${PACKAGE_DIR}/agent.yaml" ]; do
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
# credential check may need to wait until after install. This final check is
# authoritative even if the installed config changed after the early fail-fast
# check.
validate_auth_mode

# --- 3b. Codex's durable OAuth dir on the volume (#485) ---------------------
# Same idea for codex: ~/.codex (where `codex login`/`codex exec` keep auth.json)
# points at a volume dir so the ChatGPT subscription survives a redeploy. claude
# already gets this via CLAUDE_CONFIG_DIR above; codex has no config-dir override,
# so we symlink the home dir directly.
if [ "${ENTRYPOINT_ENGINE}" = "codex" ]; then
  mkdir -p "${BRAIN_CRED_DIR}"
  chown "${APP_USER}:${APP_USER}" "${BRAIN_CRED_DIR}"
  if [ -d /opt/bobi/skills ]; then
    log "Linking baked team skills into ${BRAIN_CRED_DIR}/skills for codex"
    mkdir -p "${BRAIN_CRED_DIR}/skills"
    chown "${APP_USER}:${APP_USER}" "${BRAIN_CRED_DIR}/skills"
    for existing_skill in "${BRAIN_CRED_DIR}/skills"/*; do
      [ -L "${existing_skill}" ] || continue
      existing_target="$(readlink "${existing_skill}")"
      case "${existing_target}" in
        /opt/bobi/skills/*)
          if [ ! -e "${existing_target}" ]; then
            log "Removing stale baked codex skill link ${existing_skill}"
            rm -f "${existing_skill}"
          fi
          ;;
      esac
    done
    for skill_path in /opt/bobi/skills/*; do
      [ -e "${skill_path}" ] || continue
      skill_name="$(basename "${skill_path}")"
      skill_dest="${BRAIN_CRED_DIR}/skills/${skill_name}"
      if [ -e "${skill_dest}" ] || [ -L "${skill_dest}" ]; then
        if [ -L "${skill_dest}" ]; then
          skill_dest_target="$(readlink "${skill_dest}")"
          case "${skill_dest_target}" in
            /opt/bobi/skills/*) ;;
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
if [ "${BOBI_AUTH:-api_key}" != "subscription" ]; then
  if [ "${ENTRYPOINT_ENGINE}" = "codex" ]; then
    if [ "${ENTRYPOINT_IS_GATEWAY}" = "1" ]; then
      [ -z "${OPENAI_API_KEY:-}" ] \
        || log "Codex gateway: leaving OPENAI_API_KEY out of native auth materialization"
    else
      materialize_codex_api_key_auth "${BRAIN_CRED_DIR}"
    fi
  else
    materialize_codex_api_key_auth "${HOME}/.codex"
  fi
elif [ -n "${OPENAI_API_KEY:-}" ]; then
  log "Subscription mode: leaving OPENAI_API_KEY out of Codex auth materialization"
fi

if [ "${BOBI_AUTH:-api_key}" = "subscription" ]; then
  if [ "${ENTRYPOINT_ENGINE}" = "codex" ]; then
    codex_dir="${BRAIN_CRED_DIR}"
  else
    codex_dir="${HOME}/.codex"
  fi
  if codex_auth_uses_api_key "${codex_dir}"; then
    log "Subscription mode: removing Codex API-key auth file so OAuth can be used"
    rm -f "${codex_dir}/auth.json"
  fi
fi

# --- 4. Subscription auth: bootstrap when stored creds cannot refresh (C23) --
# This is deliberately structural and local. It catches missing, malformed,
# blanked, and explicitly-expired refresh credentials without a network probe.
if [ "${BOBI_AUTH:-api_key}" = "subscription" ]; then
  credential_path="${BRAIN_CRED_DIR}/${BRAIN_CRED_FILE}"
  if credential_reason="$(as_app python -m bobi.auth_bootstrap \
      credential-status "${ENTRYPOINT_ENGINE}" "${credential_path}" 2>&1)"; then
    log "Subscription mode, ${ENTRYPOINT_BRAIN} ${credential_reason} — skipping login bootstrap"
  else
    log "Subscription mode, ${ENTRYPOINT_BRAIN} ${credential_reason} — running login bootstrap"
    as_app bobi agent "${AGENT_NAME}" login-bootstrap
  fi
fi

# --- 4c. Dependency snapshot is frozen; warm boot runs no bootstrap (#428) ---
# The unified dependency model materializes + verifies a team's deps when the
# team IMAGE is built (in CI, OQ1) and freezes the result. Production never runs
# the bootstrap agent: a warm boot replays that snapshot. Surface the baked
# dependency-set identity so `fly logs` shows which snapshot is live — `bobi
# deploy` compares this same stamp (over `fly ssh`) to decide when a CHANGED
# dependency set needs a rebuild + re-bootstrap.
if [ -f /opt/bobi/dep-list.hash ]; then
  log "Dependency snapshot $(cat /opt/bobi/dep-list.hash) — warm boot, no bootstrap (deps baked in CI, #428)."
fi

# --- 5. Hand off to the manager as the non-root user ------------------------
# Agent UI on by default IN THE CONTAINER (the manager starts it on the private
# 6PN for control-plane administration). It's image behavior, not a per-instance
# flag, so existing instances pick it up on their next image swap. Disable with
# BOBI_UI=0 in the Fly env. The dark instance has no public route, so this
# exposes nothing — see DESIGN.md "Agent UI".
export BOBI_UI="${BOBI_UI:-1}"
# #5: the supervisor sidecar is the terminal process. It spawns the manager as a
# child, watches the director's progress from OUTSIDE (health endpoint + process
# table + status-file freshness), restarts a wedged director from below (the one
# recovery layer stall-recovery cannot provide), AND publishes tier-1 heartbeat
# + tier-2 lifecycle telemetry onto the bus. It runs no agent loop, so it cannot
# wedge from the same cause; on restart-budget exhaustion it exits non-zero and
# the orchestrator's machine restart policy escalates. `healthcheck.sh` is
# unaffected (the manager child still writes the port file). The forwarded
# `--foreground` keeps the manager a supervisable child rather than letting it
# daemonize.
#
# The sidecar is the ONLY supervision path. The legacy in-repo `bobi supervise`
# watchdog was removed in bobi-agent #720, so there is nothing to fall back to;
# its absence means a misbuilt image, and we fail loudly rather than boot a
# half-supervised (or unsupervisable) instance.
#
# The guard has to test something different now. It used to be
# `command -v bobi-supervisor` — a distinct binary, so presence on PATH was a
# real answer. Since bobi 0.51.0 the sidecar is a SUBCOMMAND of the same `bobi`
# the manager already runs, so `command -v` would only re-prove that `bobi`
# exists, which the lines above already depend on: it would pass on precisely
# the misbuilt image it exists to catch (a wheel too old to carry
# `bobi.supervisor`). Ask the CLI whether the subcommand is really there.
if ! bobi agent --help 2>/dev/null | grep -qw supervise; then
  log "FATAL: this image's bobi does not provide 'bobi agent <name> supervise' - the image is misbuilt (needs bobi >= 0.51.0, which carries the supervisor sidecar in the wheel)."
  bobi --version 2>&1 | sed 's/^/[entrypoint]   /' || true
  exit 1
fi
log "Starting manager under the supervisor sidecar (user=${APP_USER}, agent=${AGENT_NAME}, fleet=${BOBI_FLEET:-default}, instance=${BOBI_INSTANCE:-${AGENT_NAME}}, run=${RUN_ROOT}, home=${HOME}, bobi_home=${BOBI_HOME}, claude_config=${CLAUDE_CONFIG_DIR})"
# The agent name is a CLI POSITIONAL now, not an env var the shim read off
# BOBI_ROOT. Pass ${AGENT_NAME}, which is resolved above from BOBI_AGENT,
# BOBI_INSTANCE, or BOBI_ROOT's grandparent and is guaranteed non-empty (the
# resolution `fatal`s otherwise). Do NOT substitute BOBI_INSTANCE here: it is
# only one of those three sources and is empty on the other two paths.
#
# BOBI_ROOT stays in the env for the manager and the team's own scripts, but it
# no longer selects what gets supervised — `bobi agent <name> supervise` binds
# the root from the name and deliberately refuses to re-resolve from BOBI_ROOT,
# so the name on this line is the single answer.
if [ -n "${BOBI_BRAIN:-}" ] || [ "${ENTRYPOINT_BRAIN}" != "claude" ]; then
  exec gosu "${APP_USER}" env "HOME=${HOME}" "BOBI_HOME=${BOBI_HOME}" "BOBI_ROOT=${BOBI_ROOT}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "BOBI_BRAIN=${ENTRYPOINT_BRAIN}" "BOBI_UI=${BOBI_UI}" bobi agent "${AGENT_NAME}" supervise -- --foreground "$@"
else
  exec gosu "${APP_USER}" env "HOME=${HOME}" "BOBI_HOME=${BOBI_HOME}" "BOBI_ROOT=${BOBI_ROOT}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}" "BOBI_UI=${BOBI_UI}" bobi agent "${AGENT_NAME}" supervise -- --foreground "$@"
fi
