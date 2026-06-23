#!/usr/bin/env bash
#
# publish-team-tarballs.sh — upload built team packages to a GitHub release with
# split semantics (the publish half of #440 Phase 1):
#
#   - rolling   <team>.tar.gz           -> uploaded WITH --clobber. The floating
#                                          "latest main" pointer (team_url: /
#                                          MODASTACK_TEAM_URL consumers). Mutable
#                                          by design.
#   - versioned <team>-<ver>.tar.gz     -> uploaded WITHOUT --clobber. An
#                                          immutable, pinnable per-team package.
#                                          A re-run for an already-published
#                                          version is a NO-OP success.
#
# Immutability is fail-closed by construction, NOT a check-then-act window: we
# attempt the upload without --clobber and treat the resulting "asset already
# exists" (HTTP 422) as the no-op skip path. Two concurrent main pushes can't
# both observe "absent" and race a clobber. Any OTHER upload error is fatal.
#
# Usage:  publish-team-tarballs.sh <release-tag> <dist-dir>
#
set -euo pipefail

TAG="${1:?usage: publish-team-tarballs.sh <release-tag> <dist-dir>}"
DIR="${2:?usage: publish-team-tarballs.sh <release-tag> <dist-dir>}"

shopt -s nullglob
files=("$DIR"/*.tar.gz)
[ "${#files[@]}" -gt 0 ] || { echo "No tarballs in $DIR" >&2; exit 1; }

# A versioned asset ends with -<major>.<minor>.<patch>.tar.gz; a rolling asset
# (e.g. eng-team.tar.gz) does not. Team names contain hyphens, so match the
# semver suffix rather than splitting on '-'.
version_re='-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz$'

for tgz in "${files[@]}"; do
  base="$(basename "$tgz")"
  if [[ "$base" =~ $version_re ]]; then
    err="$(mktemp)"
    if gh release upload "$TAG" "$tgz" 2>"$err"; then
      echo "published: $base (immutable)"
    elif grep -qiE 'already.?exists|HTTP 422|status.?422' "$err"; then
      # Already published — immutable, so this is the success no-op path.
      echo "skip: $base already published (immutable)"
    else
      echo "publish: fatal error uploading $base" >&2
      cat "$err" >&2
      rm -f "$err"
      exit 1
    fi
    rm -f "$err"
  else
    gh release upload "$TAG" "$tgz" --clobber
    echo "published: $base (rolling)"
  fi
done
