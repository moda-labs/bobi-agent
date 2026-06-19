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
done

echo "Built ${#DIRS[@]} team package(s) into $OUT/"
