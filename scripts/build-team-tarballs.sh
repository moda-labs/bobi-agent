#!/usr/bin/env bash
#
# build-team-tarballs.sh — package agent-team source dirs into distributable
# .tar.gz artifacts (C10 team-injection seam).
#
# Each team directory (one holding an `agent.yaml`) becomes `<out>/<team>.tar.gz`,
# which extracts to a single `<team>/` dir containing `agent.yaml` — exactly the
# shape `modastack install <url>` / `registry.fetch_from_url` expect. Upload the
# results somewhere publicly fetchable and pass the URL as `--team-url` (or
# MODASTACK_TEAM_URL) so a dark instance can pull its team at first boot.
#
# Usage:
#   scripts/build-team-tarballs.sh [--out DIR] [TEAM_DIR ...]
#
#   --out DIR    Output directory for the tarballs. Default: dist/teams.
#   TEAM_DIR...  Specific team source dirs to package. Default: every
#                immediate subdirectory of agents/ that contains an agent.yaml.
#   -h, --help   Show this help.
#
# Examples:
#   scripts/build-team-tarballs.sh                          # all of agents/*
#   scripts/build-team-tarballs.sh agents/eng-team          # just one
#   scripts/build-team-tarballs.sh --out /tmp/pkg tests/fixtures/smoke-team

set -euo pipefail

OUT="dist/teams"
declare -a DIRS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'; exit 0;;
    -*) echo "Unknown argument: $1" >&2; exit 2;;
    *) DIRS+=("$1"); shift;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Default: every team under agents/.
if [ "${#DIRS[@]}" -eq 0 ]; then
  for d in "$REPO_ROOT"/agents/*/; do
    [ -f "${d}agent.yaml" ] && DIRS+=("${d%/}")
  done
fi
[ "${#DIRS[@]}" -gt 0 ] || { echo "No team directories found to package." >&2; exit 1; }

mkdir -p "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"

for dir in "${DIRS[@]}"; do
  [ -f "$dir/agent.yaml" ] || { echo "Skipping '$dir' (no agent.yaml)" >&2; continue; }
  name="$(basename "$dir")"
  parent="$(cd "$(dirname "$dir")" && pwd)"
  # Reproducible-ish: sorted entries, normalized ownership. (GNU tar flags; the
  # CI runner is Linux. Plain tar still works elsewhere — the flags are additive.)
  tar -czf "$OUT_ABS/$name.tar.gz" \
      --sort=name --owner=0 --group=0 --numeric-owner \
      -C "$parent" "$name" 2>/dev/null \
    || tar -czf "$OUT_ABS/$name.tar.gz" -C "$parent" "$name"
  echo "  $name -> $OUT/$name.tar.gz"

  # Also emit the immutable, pinnable per-team package `<team>-<version>.tar.gz`
  # (#440 Phase 1). It is a byte-for-byte copy of the rolling tarball at publish
  # time — only the filename differs — so a pinned install gets exactly what the
  # rolling one served. The version is read in Python (bash YAML parsing is
  # brittle); a team with no `version:` gets only the rolling tarball (D-5).
  # `|| version=""`: a single broken team must never abort the whole build
  # (it would strand healthy teams' tarballs). Degrade it to rolling-only.
  version="$(python3 "$REPO_ROOT/scripts/team-version.py" "$dir")" || version=""
  if [ -n "$version" ]; then
    cp "$OUT_ABS/$name.tar.gz" "$OUT_ABS/$name-$version.tar.gz"
    echo "  $name -> $OUT/$name-$version.tar.gz (versioned, immutable)"
  else
    echo "  warning: $name has no version in agent.yaml — rolling tarball only (not pinnable)" >&2
  fi
done

echo "Built ${#DIRS[@]} team package(s) into $OUT/"
