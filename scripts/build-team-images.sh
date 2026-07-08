#!/usr/bin/env bash
#
# build-team-images.sh - thin CI wrapper over `bobi build` (#610).
#
# For each team dir that bakes anything (declarative `build:` or a guide-only
# dependency), run `bobi build` to produce the team-flavored image. Teams with
# nothing to bake are skipped (they deploy on the generic image). All render
# and bootstrap mechanics live in `bobi build` (the bobi-deploy plugin,
# bobi_deploy/build.py, #707); this wrapper
# only keeps the CI conveniences: agents/* iteration, the skip gate, git-sha
# tagging, and the Fly-registry holder-app dance (Fly's registry is app-scoped,
# which plain `docker push` can't handle).
#
# Usage:
#   scripts/build-team-images.sh [TEAM_DIR ...]
#
#   TEAM_DIR...  Team source dirs (each holds agent.yaml). Default: agents/*.
#   -h, --help   Show this help.
#
# Environment:
#   REGISTRY       Image namespace. Default: registry.fly.io (Fly's own registry -
#                  native auth via `fly auth docker`, no extra service, and Fly
#                  pulls it without public-package/token-scope hassle). For Fly,
#                  each image repo maps to a Fly app, so this script ensures a
#                  holder app `bobi-<team>` exists before pushing.
#   PUSH           "1" to push after build. Default: "0" (build only).
#   ORG            Fly org for the holder app (Fly registry only). Default: personal.
#   BOBI_BUILD  source (default; build from this checkout) or pypi.
#   BOBI_VERSION  pypi mode: the published version to pin (default: the
#                  installed bobi's own version).
#   TAG            Extra tag besides :latest (default: the short git SHA).
#   BOBI_BOOTSTRAP_BRAINS  Brains for guide-dep bootstraps (default: claude);
#                  pass the matching *_API_KEY in the environment.
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
# themselves pace the propagation - no foreground sleep needed).
push_with_retry() {
  local ref="$1" n=0
  until docker push "$ref"; do
    n=$((n + 1))
    [ "$n" -ge 6 ] && { echo "push failed after ${n} attempts: ${ref}" >&2; return 1; }
    echo "  push of ${ref} failed (attempt ${n}) - retrying (registry may still be propagating)..."
  done
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

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

declare -a BUILD_FLAGS=(--build "$BUILD_MODE")
[ -n "${BOBI_VERSION:-}" ] && BUILD_FLAGS+=(--bobi-version "$BOBI_VERSION")
[ -n "${BOBI_BOOTSTRAP_BRAINS:-}" ] && BUILD_FLAGS+=(--brains "$BOBI_BOOTSTRAP_BRAINS")

built=0
for dir in "${DIRS[@]}"; do
  team="$(basename "$dir")"
  # --check bakes-anything gate (#428): a declarative build OR a guide-only
  # dependency the bootstrap agent must resolve. Generic teams are skipped so
  # CI doesn't churn identical generic images per team.
  if ! python -m bobi.dep_bootstrap "$dir" --check 2>/dev/null; then
    echo "skip ${team}: nothing to bake - deploys on the generic image"
    continue
  fi

  img="${REGISTRY}/bobi-${team}"
  echo "== building ${img}:${TAG} (mode=${BUILD_MODE}) =="
  python -m bobi.cli build "$dir" "${BUILD_FLAGS[@]}" \
    --tag "${img}:${TAG}" --tag "${img}:latest"

  if [ "$PUSH" = "1" ]; then
    # Fly registry repos are app-scoped - ensure the holder app exists first.
    case "$REGISTRY" in registry.fly.io*) ensure_fly_repo "bobi-${team}";; esac
    echo "== pushing ${img}:${TAG} + :latest =="
    push_with_retry "${img}:${TAG}"
    push_with_retry "${img}:latest"
  fi
  built=$((built + 1))
done

echo "Done. Built ${built} team image(s)."
