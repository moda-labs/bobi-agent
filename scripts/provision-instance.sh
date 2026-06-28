#!/usr/bin/env bash
#
# provision-instance.sh — stand up one bobi instance on Fly Machines (C10 / #340).
#
# A bobi instance is: one container image + one persistent volume (mounted at
# /data, with BOBI_HOME=/data/.bobi) + a set of env vars + an outbound WSS
# connection to one event-server deployment. Nothing reaches in; it reaches out only.
# See docs/CONTAINERIZED_DEPLOYMENT.md §2 (The image) for the instance contract.
#
# This script creates the Fly app, the volume, the secrets, and the machine, then
# deploys the C8 image (Dockerfile at repo root). It is idempotent: re-running
# against an existing app skips create steps and re-deploys, so it also serves as
# a manual "redeploy this instance" until C22 (GitOps) automates that.
#
# What it deliberately does NOT do:
#   * It never pre-registers a deployment or handles deployment_id/api_key. The
#     instance self-mints its event-bus bubble and self-registers every session
#     at boot (#240 bubble model — subagent.py:ensure_bubble/register). The
#     provisioner's only event-server job is to hand the instance the Worker URL.
#   * It never writes the volume's agent.yaml. After first boot installs the team
#     (entrypoint, BOBI_TEAM), the volume config is the source of truth — a
#     reprovision must not clobber workspace edits (that's why this only sets env
#     + secrets, never Bobi Agent runtime files).
#
# ─────────────────────────────────────────────────────────────────────────────
# OPERATOR-AGNOSTIC (design §9.1): no moda-labs assumptions are baked in.
#   * --app is the FULL, globally-unique Fly app name. Fly names are unique across
#     all of Fly, so operator-namespace it (e.g. acme-bobi-eng); there is no
#     fixed "bobi-<name>" that would squat on someone else's account.
#   * --event-server is a parameter. It defaults to the shared moda-labs Worker
#     only as a convenience; point it at YOUR OWN Worker to run standalone.
#
# BRING YOUR OWN EVENT SERVER (run a fully independent instance):
#   An instance needs two accounts: Fly (compute) and a Cloudflare Worker (the
#   event server). To stand up your own Worker instead of the shared one:
#
#     cd event-server
#     npm install
#     npx wrangler login                 # your Cloudflare account
#     npx wrangler deploy                # prints https://<name>.<you>.workers.dev
#
#   Then pass that URL:
#     scripts/provision-instance.sh --app you-bobi-eng --team eng-team \
#       --event-server https://<name>.<you>.workers.dev --env-file ./instance.env
#
#   The URL MUST be https:// (or a loopback) — the bubble key is transmitted once
#   at mint time and is refused over cleartext remote URLs (server.py guard).
# ─────────────────────────────────────────────────────────────────────────────
#
# Usage:
#   scripts/provision-instance.sh --app APP --team TEAM --env-file FILE [options]
#
# Required:
#   --app APP            Globally-unique Fly app name (operator-namespaced).
#   --team TEAM          Team to install on first boot (BOBI_TEAM). Resolves
#                        only a name/path the INSTANCE can already see (e.g. a
#                        volume path) — there is NO team registry in the image, so
#                        a bare name won't resolve at boot; the entrypoint fails
#                        loud and tells you to use --team-url. For a published
#                        team use --team-url; for a local package, `bobi
#                        deploy` ssh-pushes it (no team source on the instance).
#       …or…
#   --team-url URL       Public .tar.gz URL of one team package, fetched at first
#                        boot (BOBI_TEAM_URL). The dark instance pulls it over
#                        HTTPS — works with a GitHub release/raw asset or your own
#                        server, and is restageable later by swapping the asset.
#       …or…
#   --blank              Provision with NO team source. The instance boots into the
#                        "wait for team" state (entrypoint §3) and holds until a team
#                        is pushed in over `fly ssh` — the ssh-push delivery path that
#                        `bobi deploy` uses for a LOCAL team package. Use this
#                        instead of --team/--team-url; the caller pushes the team next.
#                        Provide exactly one of --team / --team-url / --blank.
#   --env-file FILE      KEY=VALUE file of service tokens (SLACK_BOT_TOKEN, GITHUB_TOKEN,
#                        LINEAR_API_KEY, VENN_API_KEY, ...). In api_key mode it must also
#                        contain ANTHROPIC_API_KEY; in subscription mode it must NOT.
#                        Tokens are set as Fly secrets. Any BOBI_* keys in the file
#                        are treated as plaintext [env], overridden by the flags below.
#
# Options:
#   --fleet PREFIX       Operator/fleet namespace stamped into the instance as
#                        BOBI_FLEET. The fleet-state primitive: enumerate a
#                        fleet by `fly apps list` filtered on this stamp (the C22
#                        GitOps Action and any future provisioner service share it).
#                        Default: the leading dash-segment of --app (e.g. --app
#                        acme-bobi-eng ⇒ fleet "acme"). Pass explicitly when
#                        the app name's first segment isn't your fleet namespace.
#   --instance NAME      Per-instance identity stamped as BOBI_INSTANCE — the
#                        SaaS tenant key (enumerable in [env] next to BOBI_FLEET).
#                        Default: the app name with the "<fleet>-" prefix stripped.
#   --auth MODE          api_key (default) | subscription. See §6.1.
#   --event-server URL   Worker URL (https://). Default: the shared moda-labs Worker.
#   --region REGION      Fly region for the app + volume. Default: iad.
#   --org ORG            Fly org slug to create the app in. Default: your personal org.
#   --volume-size GB     Persistent volume size. Default: 15 (design §8: 10–20 GB).
#   --memory SIZE        Machine memory, e.g. 4gb (default), 8gb for heavy teams.
#   --cpus N             Shared vCPUs. Default: 2 (shared-cpu-2x at 4gb).
#   --login-channel ID   subscription mode only: private Slack channel ID for the
#                        first-boot login bootstrap (C23, BOBI_LOGIN_CHANNEL).
#   --claude-version V   Pin the claude CLI version baked into the image (build-arg).
#   --image REF          Deploy a prebuilt image by ref (e.g. a team-flavored
#                        image from CI) instead of building. Skips the build
#                        context/Dockerfile/build-args entirely (C24).
#   --local-build        Build with local buildkit (--local-only, gzip layers)
#                        instead of Fly's remote builder. The path used on a
#                        macOS/Docker-Desktop laptop where the remote builder is
#                        unreliable; DOCKER_HOST must point at the daemon (#387).
#   --volume-name NAME   Volume name. Default: data (mounted at /data; Bobi
#                        state defaults to /data/.bobi).
#   --yes                Skip the confirmation prompt.
#   -h, --help           Show this help.
#
# Examples:
#   # api_key instance against the shared Worker
#   scripts/provision-instance.sh --app acme-bobi-eng --team eng-team \
#     --env-file ./eng.env --region sjc
#
#   # subscription (internal dogfood) instance, own Worker, first-boot Slack login
#   scripts/provision-instance.sh --app acme-bobi-dog --team eng-team \
#     --auth subscription --event-server https://ev.acme.workers.dev \
#     --login-channel C0PRIVATE --env-file ./dog.env
#
# To tear an instance down: scripts/destroy-instance.sh --app APP

set -euo pipefail

# Default event server: the shared moda-labs Worker. Override with --event-server
# (and BRING YOUR OWN EVENT SERVER above) to run independently.
DEFAULT_EVENT_SERVER="https://bobi-events.modalabs.workers.dev"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- args -------------------------------------------------------------------
APP="" TEAM="" TEAM_URL="" BLANK="0" ENV_FILE="" AUTH="api_key" FLEET="" INSTANCE=""
EVENT_SERVER="$DEFAULT_EVENT_SERVER"
REGION="iad" ORG="" VOLUME_SIZE="15" MEMORY="4gb" CPUS="2"
LOGIN_CHANNEL="" CLAUDE_VERSION="" VOLUME_NAME="data" ASSUME_YES="0" BRAIN=""
# Build context + Dockerfile default to the repo (source build); `bobi
# deploy` overrides them to a packaged context (Dockerfile.pypi, PyPI install)
# in binary mode, so deploy needs no checkout. --build-arg K=V is repeatable.
BUILD_CONTEXT="" DOCKERFILE=""
# --image <ref> deploys a prebuilt image (C24 team-flavored images) instead of
# building one — the build context/Dockerfile/build-args are then unused.
IMAGE=""
# --local-build builds the image with local buildkit (--local-only, gzip layers)
# instead of Fly's remote builder — the path `bobi deploy` takes on a
# macOS/Docker-Desktop laptop, where the remote builder is unreliable (#387).
# DOCKER_HOST is supplied by the caller's env (deploy.py resolves it).
LOCAL_BUILD=""
declare -a BUILD_ARGS=()

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2;;
    --fleet) FLEET="$2"; shift 2;;
    --instance) INSTANCE="$2"; shift 2;;
    --team) TEAM="$2"; shift 2;;
    --team-url) TEAM_URL="$2"; shift 2;;
    --blank) BLANK="1"; shift;;
    --env-file) ENV_FILE="$2"; shift 2;;
    --auth) AUTH="$2"; shift 2;;
    --event-server) EVENT_SERVER="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    --org) ORG="$2"; shift 2;;
    --volume-size) VOLUME_SIZE="$2"; shift 2;;
    --memory) MEMORY="$2"; shift 2;;
    --cpus) CPUS="$2"; shift 2;;
    --login-channel) LOGIN_CHANNEL="$2"; shift 2;;
    --brain) BRAIN="$2"; shift 2;;
    --claude-version) CLAUDE_VERSION="$2"; shift 2;;
    --volume-name) VOLUME_NAME="$2"; shift 2;;
    --build-context) BUILD_CONTEXT="$2"; shift 2;;
    --dockerfile) DOCKERFILE="$2"; shift 2;;
    --build-arg) BUILD_ARGS+=("$2"); shift 2;;
    --image) IMAGE="$2"; shift 2;;
    --local-build) LOCAL_BUILD="1"; shift;;
    --yes) ASSUME_YES="1"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown argument: $1" >&2; echo "Run with --help." >&2; exit 2;;
  esac
done

log()   { echo "[provision] $*"; }
fatal() { echo "[provision] FATAL: $*" >&2; exit 1; }

# `fly` is the current binary; `flyctl` is the legacy name. Accept either.
FLY="fly"
command -v fly >/dev/null 2>&1 || FLY="flyctl"
command -v "$FLY" >/dev/null 2>&1 \
  || fatal "flyctl not found. Install it: https://fly.io/docs/flyctl/install/"

# --- validate ---------------------------------------------------------------
[ -n "$APP" ]      || fatal "--app is required (globally-unique Fly app name)."
# Fleet namespace defaults to the app name's leading dash-segment so a fleet of
# apps named "<fleet>-<...>" enumerates by this stamp without extra config.
[ -n "$FLEET" ] || FLEET="${APP%%-*}"
# Instance key defaults to the app name minus the "<fleet>-" prefix (the slug),
# matching the app = "<fleet>-<name>" convention. Falls back to the full app name
# when the prefix doesn't match (e.g. an explicit --fleet that isn't the prefix).
if [ -z "$INSTANCE" ]; then
  case "$APP" in
    "$FLEET"-*) INSTANCE="${APP#"$FLEET"-}";;
    *)          INSTANCE="$APP";;
  esac
fi
# Exactly one team source: --team (registry name), --team-url (public tarball),
# or --blank (no source; the entrypoint waits for an ssh-pushed team).
team_modes=0
[ -n "$TEAM" ]     && team_modes=$((team_modes + 1))
[ -n "$TEAM_URL" ] && team_modes=$((team_modes + 1))
[ "$BLANK" = "1" ] && team_modes=$((team_modes + 1))
if [ "$team_modes" -gt 1 ]; then
  fatal "pass exactly one of --team / --team-url / --blank."
elif [ "$team_modes" -eq 0 ]; then
  fatal "one of --team (registry name), --team-url (public .tar.gz URL), or --blank (ssh-push) is required."
fi
if [ -n "$TEAM_URL" ]; then
  case "$TEAM_URL" in
    https://*|http://*) ;;
    *) fatal "--team-url must be an http(s) URL. Got: $TEAM_URL";;
  esac
fi
[ -n "$ENV_FILE" ] || fatal "--env-file is required (KEY=VALUE service tokens)."
[ -f "$ENV_FILE" ] || fatal "--env-file '$ENV_FILE' not found."
# Build context + Dockerfile: default to the repo (source build) unless the
# caller passed a packaged context (binary-mode `bobi deploy`). Skipped
# entirely in --image mode (a prebuilt image is deployed, nothing is built).
if [ -z "$IMAGE" ]; then
  [ -n "$BUILD_CONTEXT" ] || BUILD_CONTEXT="$REPO_ROOT"
  [ -n "$DOCKERFILE" ]    || DOCKERFILE="$REPO_ROOT/Dockerfile"
  [ -d "$BUILD_CONTEXT" ] || fatal "--build-context '$BUILD_CONTEXT' is not a directory."
  [ -f "$DOCKERFILE" ]    || fatal "Dockerfile not found: $DOCKERFILE"
fi

case "$AUTH" in
  api_key|subscription) ;;
  *) fatal "--auth must be api_key or subscription (got '$AUTH').";;
esac

# The brain (#485) decides which provider API key authenticates (api_key mode) or
# would shadow subscription OAuth, and which login the first-boot bootstrap runs.
case "${BRAIN:-claude}" in
  ""|claude) AUTH_KEY="ANTHROPIC_API_KEY"
             LOGIN_FALLBACK_CMD="env CLAUDE_CONFIG_DIR=/data/claude claude auth login --claudeai";;
  codex)     AUTH_KEY="OPENAI_API_KEY"
             LOGIN_FALLBACK_CMD="env CODEX_HOME=/data/codex codex login --device-auth";;
  *) fatal "--brain must be claude or codex (got '$BRAIN').";;
esac

# Mirror the server-side bubble-mint guard so we fail fast with a clear message:
# minting transmits the bubble key once, so a cleartext remote URL is refused.
case "$EVENT_SERVER" in
  https://*) ;;
  # Quote the IPv6 literal so `[::1]` is matched as text, not a glob char-class.
  http://localhost*|http://127.0.0.1*|'http://[::1]'*) ;;
  *) fatal "--event-server must be https:// (or a loopback). Got: $EVENT_SERVER";;
esac

if ! "$FLY" auth whoami >/dev/null 2>&1; then
  fatal "Not logged in to Fly. Run: $FLY auth login"
fi

# --- parse the env file: secrets vs BOBI_* identity --------------------
# Anything BOBI_* is plaintext instance identity → fly.toml [env].
# Everything else is sensitive → fly secrets. Flag-provided identity wins.
declare -a SECRET_ARGS=()
declare -A ENV_FROM_FILE=()
HAS_AUTH_KEY="0"
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"                          # tolerate CRLF
  case "$line" in ''|'#'*) continue;; esac
  [[ "$line" == *=* ]] || continue
  key="${line%%=*}"; val="${line#*=}"
  key="$(echo -n "$key" | tr -d '[:space:]')"
  [ -n "$key" ] || continue
  if [[ "$key" == BOBI_* ]]; then
    ENV_FROM_FILE["$key"]="$val"
    continue
  fi
  [ "$key" = "$AUTH_KEY" ] && HAS_AUTH_KEY="1"
  SECRET_ARGS+=("$key=$val")
done < "$ENV_FILE"

# --- auth-mode invariants (§6.1) --------------------------------------------
if [ "$AUTH" = "api_key" ]; then
  [ "$HAS_AUTH_KEY" = "1" ] \
    || fatal "--auth api_key but $AUTH_KEY is missing from $ENV_FILE."
else
  # subscription: the provider API key silently outranks subscription OAuth creds
  # and bills the API instead — it must be entirely absent (§6.1).
  [ "$HAS_AUTH_KEY" = "0" ] \
    || fatal "--auth subscription but $AUTH_KEY is in $ENV_FILE. Remove it (it overrides subscription auth)."
  [ -n "$LOGIN_CHANNEL" ] || [ -n "${ENV_FROM_FILE[BOBI_LOGIN_CHANNEL]:-}" ] \
    || log "WARNING: subscription mode without --login-channel. First-boot login (C23) will have no Slack channel; fall back to: $FLY ssh console -a $APP --command '$LOGIN_FALLBACK_CMD'"
fi

# --- assemble the instance's [env] identity ---------------------------------
# eng-team's agent.yaml references ${BOBI_EVENT_SERVER}; that is the var the
# image resolves the Worker URL from (config.py interpolates ${VAR} at load).
declare -A ENV_VARS=()
for k in "${!ENV_FROM_FILE[@]}"; do ENV_VARS["$k"]="${ENV_FROM_FILE[$k]}"; done
[ -n "$TEAM" ]     && ENV_VARS["BOBI_TEAM"]="$TEAM"
[ -n "$TEAM_URL" ] && ENV_VARS["BOBI_TEAM_URL"]="$TEAM_URL"
ENV_VARS["BOBI_AUTH"]="$AUTH"
ENV_VARS["BOBI_EVENT_SERVER"]="$EVENT_SERVER"
# Fleet-membership stamp: this is the authoritative fleet-state key (the app name
# is only a discovery hint). Enumerate a fleet by reading this back per app
# (`fly config env`/`scripts/fleet.sh`), so two fleets can share one Fly org and
# a future BOBI_TENANT filter slots into the same [env] block (design §9.1).
ENV_VARS["BOBI_FLEET"]="$FLEET"
# Per-instance identity (the SaaS tenant key). Enumerable alongside BOBI_FLEET;
# `bobi deploy <name>` stamps and reads this back to find the app for <name>.
ENV_VARS["BOBI_INSTANCE"]="$INSTANCE"
[ -n "$LOGIN_CHANNEL" ] && ENV_VARS["BOBI_LOGIN_CHANNEL"]="$LOGIN_CHANNEL"
# The agent brain (#485): the entrypoint reads it to branch credential dir +
# first-boot login; get_brain() reads it to select the runtime adapter.
[ -n "$BRAIN" ] && ENV_VARS["BOBI_BRAIN"]="$BRAIN"
# NB: the agent UI is on by default in the container (docker-entrypoint.sh sets
# BOBI_UI=1), so it is NOT a provisioned [env] flag. Set BOBI_UI=0 here
# (or as a Fly env) only to disable it for an instance.

# --- confirm ----------------------------------------------------------------
echo
echo "  Fly app        : $APP${ORG:+  (org: $ORG)}"
echo "  Fleet          : $FLEET  (BOBI_FLEET stamp)"
echo "  Instance       : $INSTANCE  (BOBI_INSTANCE stamp)"
echo "  Region         : $REGION"
if [ "$BLANK" = "1" ]; then
  echo "  Team           : (blank — waits for an ssh-pushed team)"
else
  echo "  Team           : ${TEAM:-(from URL) $TEAM_URL}"
fi
echo "  Auth mode      : $AUTH"
echo "  Event server   : $EVENT_SERVER"
echo "  Volume         : $VOLUME_NAME (${VOLUME_SIZE} GB) at /data"
echo "  Machine        : ${CPUS} shared vCPU / ${MEMORY}, always-on (no auto-stop)"
echo "  Secrets set    : ${#SECRET_ARGS[@]} key(s) from $ENV_FILE"
echo "  Env (identity) : ${!ENV_VARS[*]}"
echo
if [ "$ASSUME_YES" != "1" ]; then
  read -r -p "Provision this instance? [y/N] " reply
  case "$reply" in y|Y|yes|YES) ;; *) fatal "Aborted.";; esac
fi

# --- 1. app -----------------------------------------------------------------
if "$FLY" status -a "$APP" >/dev/null 2>&1; then
  log "App '$APP' already exists — skipping create (will redeploy)."
else
  log "Creating app '$APP'..."
  "$FLY" apps create "$APP" ${ORG:+--org "$ORG"}
fi

# --- 2. volume --------------------------------------------------------------
# `fly volumes list` prints existing volumes; match by name so reruns don't
# stack a second volume (which would let the deploy pick the wrong one).
if "$FLY" volumes list -a "$APP" 2>/dev/null | grep -qw "$VOLUME_NAME"; then
  log "Volume '$VOLUME_NAME' already exists — skipping create."
else
  log "Creating ${VOLUME_SIZE} GB volume '$VOLUME_NAME' in $REGION..."
  "$FLY" volumes create "$VOLUME_NAME" -a "$APP" \
    --region "$REGION" --size "$VOLUME_SIZE" --yes
fi

# --- 3. secrets -------------------------------------------------------------
# Stage secrets so they apply on the next deploy (no machines exist yet on first
# run). Re-running re-stages; unchanged secrets are a no-op.
if [ "${#SECRET_ARGS[@]}" -gt 0 ]; then
  log "Setting ${#SECRET_ARGS[@]} secret(s)..."
  "$FLY" secrets set --stage -a "$APP" "${SECRET_ARGS[@]}" >/dev/null
fi

# --- 4. generate fly.toml ---------------------------------------------------
# Per-app config written to a temp file (app name + region vary per instance).
# NO [http_service]/ports: the instance is dark — outbound WSS only (§2, §5).
# No [http_service] also means Fly never auto-stops it ⇒ always-on for MVP
# (design §10 C10: auto_stop:false). Liveness is the image's own HEALTHCHECK.
CFG="$(mktemp -t fly-"$APP"-XXXX.toml)"
trap 'rm -f "$CFG"' EXIT

{
  echo "app = \"$APP\""
  echo "primary_region = \"$REGION\""
  # NB: the Dockerfile is passed via `fly deploy --dockerfile`, not a
  # `[build] dockerfile = …` key — Fly resolves that key relative to THIS
  # config file's directory (a temp dir), where the Dockerfile isn't.
  # Build args: CLAUDE_VERSION (pin) + any --build-arg (e.g. BOBI_VERSION,
  # which the PyPI image installs). Emitted as a [build.args] TOML table. In
  # --image mode nothing is built, so the build table is omitted.
  if [ -z "$IMAGE" ] && { [ -n "$CLAUDE_VERSION" ] || [ "${#BUILD_ARGS[@]}" -gt 0 ]; }; then
    echo
    echo "[build.args]"
    [ -n "$CLAUDE_VERSION" ] && echo "  CLAUDE_VERSION = \"$CLAUDE_VERSION\""
    if [ "${#BUILD_ARGS[@]}" -gt 0 ]; then
      for kv in "${BUILD_ARGS[@]}"; do
        echo "  ${kv%%=*} = \"${kv#*=}\""
      done
    fi
  fi
  echo
  echo "[env]"
  for k in $(printf '%s\n' "${!ENV_VARS[@]}" | sort); do
    echo "  $k = \"${ENV_VARS[$k]}\""
  done
  echo
  echo "[[mounts]]"
  echo "  source = \"$VOLUME_NAME\""
  echo "  destination = \"/data\""
  echo
  echo "[[vm]]"
  echo "  cpu_kind = \"shared\""
  echo "  cpus = $CPUS"
  echo "  memory = \"$MEMORY\""
} > "$CFG"

log "Generated fly config:"
sed 's/^/    /' "$CFG"

# --- 5. deploy --------------------------------------------------------------
# --remote-only builds the image on Fly's builders (no local Docker needed).
# Build context + --dockerfile default to the repo (source build) but are
# overridable (--build-context/--dockerfile): binary-mode `bobi deploy`
# points them at a packaged context whose Dockerfile.pypi installs bobi from
# PyPI, so no checkout is needed. The generated config lives in a temp dir, so
# the Dockerfile is pointed at explicitly rather than via a relative key.
#
# --depot=false forces Fly's classic buildkit builder (gzip layers). The default
# Depot builder emits zstd-compressed OCI layers (tar+zstd), which Fly's MACHINE
# INIT cannot extract — the rootfs comes up incomplete and execve of the
# entrypoint (and Fly's own hallpass) fails with ENOENT. gzip layers boot fine.
# (Depot's `--compression=gzip` is a narrower alternative, but disabling Depot is
# the proven path.)
#
# Build mode: --remote-only (default) builds on Fly's builders — correct for
# CI/GitOps and any host with a working Docker daemon at /var/run/docker.sock.
# --local-build flips to --local-only (local buildkit), the path `bobi
# deploy` takes on a macOS/Docker-Desktop laptop where the remote builder is
# unreliable (#387). Local buildkit also emits gzip layers, so --depot=false is
# moot but harmless there; we keep it for a single, consistent flag set.
#
# --ha=false: one machine, matching our one volume (Fly defaults to HA = a spare
#   machine, which would need a second volume and fail the deploy).
# --wait-timeout 10m: first boot installs a team (and on a cold image warms a
#   model) past the default 5m machine-state wait.
#
# --image mode (C24): deploy a prebuilt team image by ref — no build context,
#   no Dockerfile, no remote builder. Fly pulls the image (its lower layers
#   dedup against other fleet apps in the same registry).
if [ -n "$IMAGE" ]; then
  log "Deploying prebuilt image '$IMAGE' (no build)..."
  "$FLY" deploy --image "$IMAGE" --config "$CFG" \
    --ha=false --wait-timeout 10m
else
  BUILD_MODE="--remote-only"
  if [ -n "$LOCAL_BUILD" ]; then
    BUILD_MODE="--local-only"
    log "Building locally with buildkit (--local-only, gzip layers) — #387."
    [ -n "${DOCKER_HOST:-}" ] && log "  DOCKER_HOST=$DOCKER_HOST"
  fi
  log "Building image on Fly and deploying... (first build bakes the model; be patient)"
  "$FLY" deploy "$BUILD_CONTEXT" --config "$CFG" --dockerfile "$DOCKERFILE" \
    --depot=false --ha=false --wait-timeout 10m "$BUILD_MODE"
fi

echo
log "Done. Instance '$APP' is provisioning."
echo "  Logs   : $FLY logs -a $APP"
echo "  Status : $FLY status -a $APP"
echo "  Admin  : $FLY ssh console -a $APP --command 'gosu bobi env HOME=/home/bobi BOBI_HOME=/data/.bobi CLAUDE_CONFIG_DIR=/data/claude bobi agent $INSTANCE status'"
if [ "$AUTH" = "subscription" ]; then
  echo
  echo "  Subscription first boot: the entrypoint posts a login URL to the private"
  echo "  Slack channel ${LOGIN_CHANNEL:-<BOBI_LOGIN_CHANNEL>}; open it, log in, and paste the code back in"
  echo "  that channel (C23). Manual fallback:"
  echo "    $FLY ssh console -a $APP --command 'env CLAUDE_CONFIG_DIR=/data/claude claude auth login --claudeai'"
fi
