#!/usr/bin/env bash
#
# build-team-images.sh — build (and optionally push) per-team container images
# (C24 team-flavored images). The companion to build-team-tarballs.sh: that
# packages a team's DEFINITION (prompts/workflows → volume); this bakes a team's
# TOOL DEPS into an image.
#
# For each team dir that declares a `build:` spec (or ships a raw Dockerfile
# escape hatch), render the team-deps hook (bobi.build_render) and build the
# ONE repo Dockerfile with --build-arg TEAM_DEPS=<rendered>. The hook runs as a
# stable layer BELOW the framework wheel, and its final step re-runs the team's
# `requires[].check` — so a missing tool fails THIS build (CI), not production.
# Teams with no build spec are skipped (they deploy on the generic image).
#
# Usage:
#   scripts/build-team-images.sh [TEAM_DIR ...]
#
#   TEAM_DIR...  Team source dirs (each holds agent.yaml). Default: agents/*.
#   -h, --help   Show this help.
#
# Environment:
#   REGISTRY       Image namespace. Default: registry.fly.io (Fly's own registry —
#                  native auth via `fly auth docker`, no extra service, and Fly
#                  pulls it without public-package/token-scope hassle). For Fly,
#                  each image repo maps to a Fly app, so this script ensures a
#                  holder app `bobi-<team>` exists before pushing.
#   PUSH           "1" to docker push after build. Default: "0" (build only).
#   ORG            Fly org for the holder app (Fly registry only). Default: personal.
#   BOBI_BUILD  source (default; build the wheel from this checkout) or pypi.
#   BOBI_VERSION  required when BOBI_BUILD=pypi (the published version).
#   TAG            Extra tag besides :latest (default: the short git SHA).
#
# Examples:
#   scripts/build-team-images.sh                       # build all teams w/ a spec
#   PUSH=1 scripts/build-team-images.sh agents/eng-team
set -euo pipefail

REGISTRY="${REGISTRY:-registry.fly.io}"
PUSH="${PUSH:-0}"
BUILD_MODE="${BOBI_BUILD:-source}"
ORG="${ORG:-}"

# `fly` is the current binary; `flyctl` is the legacy name. Accept either.
FLY="fly"; command -v fly >/dev/null 2>&1 || FLY="flyctl"

# Fly's registry is app-scoped: pushing registry.fly.io/<repo> needs a Fly app
# named <repo>. Ensure a holder app exists (idempotent) + refresh docker creds.
ensure_fly_repo() {
  local app="$1"
  command -v "$FLY" >/dev/null 2>&1 || { echo "flyctl not found for a registry.fly.io push" >&2; exit 1; }
  "$FLY" apps create "$app" ${ORG:+--org "$ORG"} 2>/dev/null \
    || echo "  (holder app ${app} already exists)"
  "$FLY" auth docker >/dev/null
}

# A freshly-created holder app isn't immediately visible to the registry, so the
# first push can 404 with NAME_UNKNOWN. Retry a few times (the attempts
# themselves pace the propagation — no foreground sleep needed).
push_with_retry() {
  local ref="$1" n=0
  until docker push "$ref"; do
    n=$((n + 1))
    [ "$n" -ge 6 ] && { echo "push failed after ${n} attempts: ${ref}" >&2; return 1; }
    echo "  push of ${ref} failed (attempt ${n}) — retrying (registry may still be propagating)…"
  done
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# A team with a guide-only dependency (#428) needs its recipe materialized by the
# bootstrap agent on a FRESH BASE IMAGE (OQ6), so the recipe is faithful to the
# image, not to the CI host. We build that base image once and reuse it. Brains to
# verify under (and the API keys they need) come from BOBI_BOOTSTRAP_BRAINS
# (default: claude); pass the matching *_API_KEY in the environment.
BOOTSTRAP_BASE_IMG=""
_relpath() { python -c 'import os,sys;print(os.path.relpath(os.path.abspath(sys.argv[1]),sys.argv[2]))' "$1" "$2"; }

ensure_bootstrap_base() {
  [ -n "$BOOTSTRAP_BASE_IMG" ] && return 0
  BOOTSTRAP_BASE_IMG="bobi-bootstrap-base:${TAG:-dev}"
  echo "== building base image for in-container dependency bootstrap: ${BOOTSTRAP_BASE_IMG} =="
  docker build --build-arg "BOBI_BUILD=${BUILD_MODE}" "${EXTRA_BUILD_ARGS[@]}" \
    -t "${BOOTSTRAP_BASE_IMG}" -f "$REPO_ROOT/Dockerfile" "$REPO_ROOT"
}

# render_team_deps <team_dir> <out_abs> — render the team-deps hook to <out_abs>.
# Guide-only deps render inside the base image (live agent); everything else
# renders host-side with no agent (byte-identical to the pre-#428 render).
render_team_deps() {
  local dir="$1" out_abs="$2"
  if python -m bobi.dep_bootstrap "$dir" --needs-agent 2>/dev/null; then
    ensure_bootstrap_base
    local rel_dir out_rel brains
    rel_dir="$(_relpath "$dir" "$REPO_ROOT")"
    out_rel="$(_relpath "$out_abs" "$REPO_ROOT")"
    brains="${BOBI_BOOTSTRAP_BRAINS:-claude}"
    echo "== bootstrapping guide deps for $(basename "$dir") in ${BOOTSTRAP_BASE_IMG} (brains=${brains}) =="
    # cwd=/tmp (not /repo) so the IMAGE's installed bobi runs, not the mounted
    # source; the repo is mounted only for the team source + rendered output.
    # IS_SANDBOX=1 lets the brain CLI run bypassPermissions as root in this
    # throwaway build container. The recipe is baked below via a normal build.
    docker run --rm \
      -e IS_SANDBOX=1 \
      ${ANTHROPIC_API_KEY:+-e ANTHROPIC_API_KEY} \
      ${OPENAI_API_KEY:+-e OPENAI_API_KEY} \
      -v "$REPO_ROOT:/repo" -w /tmp \
      --entrypoint python \
      "$BOOTSTRAP_BASE_IMG" \
      -m bobi.dep_bootstrap "/repo/${rel_dir}" --render "/repo/${out_rel}" --brains "$brains"
  else
    python -m bobi.dep_bootstrap "$dir" --render "$out_abs"
  fi
}

declare -a DIRS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'; exit 0;;
    -*) echo "Unknown argument: $1" >&2; exit 2;;
    *) DIRS+=("$1"); shift;;
  esac
done

if [ "${#DIRS[@]}" -eq 0 ]; then
  for d in "$REPO_ROOT"/agents/*/; do
    [ -f "${d}agent.yaml" ] && DIRS+=("${d%/}")
  done
fi
[ "${#DIRS[@]}" -gt 0 ] || { echo "No team directories found." >&2; exit 1; }

TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
DEPS_DIR="$REPO_ROOT/dist/team-deps"
mkdir -p "$DEPS_DIR"

declare -a EXTRA_BUILD_ARGS=()
[ "$BUILD_MODE" = "pypi" ] && EXTRA_BUILD_ARGS+=(--build-arg "BOBI_VERSION=${BOBI_VERSION:?BOBI_VERSION required in pypi mode}")

built=0
for dir in "${DIRS[@]}"; do
  team="$(basename "$dir")"
  # --check bakes-anything gate (#428): a declarative build OR a guide-only
  # dependency the bootstrap agent must resolve. Wider than build_render --check
  # (declarative only), so a guide-only team isn't silently skipped.
  if ! python -m bobi.dep_bootstrap "$dir" --check 2>/dev/null; then
    echo "skip ${team}: nothing to bake — deploys on the generic image"
    continue
  fi
  # Render the team-deps hook INTO the build context (repo root) so the
  # Dockerfile's `COPY ${TEAM_DEPS}` can reach it. A guide-only dep is
  # materialized by the bootstrap agent ON A FRESH BASE IMAGE (OQ6) — so the
  # resolved recipe is faithful to the image, not the CI host — then frozen
  # through the ONE renderer. A declarative-only team renders host-side, no agent.
  deps_rel="dist/team-deps/${team}.sh"
  render_team_deps "$dir" "$REPO_ROOT/${deps_rel}"

  img="${REGISTRY}/bobi-${team}"
  echo "== building ${img}:${TAG} (mode=${BUILD_MODE}) =="
  docker build \
    --build-arg "BOBI_BUILD=${BUILD_MODE}" \
    "${EXTRA_BUILD_ARGS[@]}" \
    --build-arg "TEAM_DEPS=${deps_rel}" \
    -t "${img}:${TAG}" -t "${img}:latest" \
    -f "$REPO_ROOT/Dockerfile" "$REPO_ROOT"

  if [ "$PUSH" = "1" ]; then
    # Fly registry repos are app-scoped — ensure the holder app exists first.
    case "$REGISTRY" in registry.fly.io*) ensure_fly_repo "bobi-${team}";; esac
    echo "== pushing ${img}:${TAG} + :latest =="
    push_with_retry "${img}:${TAG}"
    push_with_retry "${img}:latest"
  fi
  built=$((built + 1))
done

echo "Done. Built ${built} team image(s)."
