#!/usr/bin/env bash
#
# fleet.sh — fleet-state helpers over the Fly API (C22 GitOps, #342).
#
# A "fleet" is the set of modastack instances sharing one operator namespace,
# stamped MODASTACK_FLEET=<prefix> into each app's [env] by provision-instance.sh.
# The Fly API is the ONLY state store — no database, no committed manifest. These
# helpers are the enumeration/classification primitive that gitops-teams.yml and
# gitops-release.yml build on, and that a future provisioner service (design §9,
# "Provisioner service replaces the C10 script + C22 Action behind the same
# contract") can replace wholesale without changing how instances are stamped.
#
# Identity model (design §9.1, SaaS-extensible):
#   * App name = "<fleet>-<slug>" — a deterministic DISCOVERY HINT.
#   * MODASTACK_FLEET app env stamp = the AUTHORITATIVE membership key. Two fleets
#     can share one Fly org; a future MODASTACK_TENANT filter slots into the same
#     query. Enumeration trusts the stamp, not the name string.
#
# Dual use — source it as a library, or run a subcommand:
#   source scripts/fleet.sh           # then call fleet_app / fleet_list / ...
#   scripts/fleet.sh app   <prefix> <slug>      # -> "<prefix>-<slug>"
#   scripts/fleet.sh list  <prefix>             # -> member app names, one per line
#   scripts/fleet.sh classify <prefix> <slug>...# -> added=[...] / changed=[...]
#   scripts/fleet.sh fleet-of <app>             # -> the app's MODASTACK_FLEET stamp
#
# classify partitions by Fly STATE, not git: a team slug whose app does not exist
# is "added" (provision); one whose app exists is "changed" (in-place update).
# This makes failed-provision retries and re-add-after-destroy self-heal.

# `fly` is the current binary; `flyctl` is the legacy name. Accept either.
FLEET_FLY="${FLEET_FLY:-fly}"
command -v "$FLEET_FLY" >/dev/null 2>&1 || FLEET_FLY="flyctl"

# fleet_app PREFIX SLUG -> the deterministic app name for one team in a fleet.
fleet_app() { printf '%s-%s\n' "$1" "$2"; }

# fleet_exists APP -> 0 if the Fly app exists (and is yours), else 1.
# Separate function so unit tests can stub it without touching Fly.
fleet_exists() { "$FLEET_FLY" status -a "$1" >/dev/null 2>&1; }

# fleet_of APP -> echo the app's MODASTACK_FLEET stamp ("" if none/unreadable).
# `fly config show` returns the live app config as JSON, including the [env] block.
fleet_of() {
  "$FLEET_FLY" config show -a "$1" 2>/dev/null \
    | jq -r '.env.MODASTACK_FLEET // empty' 2>/dev/null
}

# fleet_list PREFIX -> every member app name, one per line. Candidates come from a
# single `fly apps list` name-prefix filter (cheap); each is then confirmed by its
# MODASTACK_FLEET stamp so an unrelated "<prefix>-website" can't sneak in.
fleet_list() {
  local prefix="$1" app
  "$FLEET_FLY" apps list --json 2>/dev/null \
    | jq -r --arg p "${prefix}-" '.[].Name | select(startswith($p))' \
    | while IFS= read -r app; do
        [ -n "$app" ] || continue
        [ "$(fleet_of "$app")" = "$prefix" ] && printf '%s\n' "$app"
      done
}

# _fleet_json_array ITEM... -> a compact JSON array of the args (empty-safe).
_fleet_json_array() {
  if [ "$#" -eq 0 ]; then printf '[]'; else printf '%s\n' "$@" | jq -R . | jq -cs .; fi
}

# fleet_classify PREFIX SLUG... -> two GITHUB_OUTPUT-ready lines:
#   added=["new-slug",...]      (no app yet -> provision)
#   changed=["existing-slug",...] (app exists -> in-place update)
fleet_classify() {
  local prefix="$1"; shift
  local added=() changed=() slug
  for slug in "$@"; do
    if fleet_exists "$(fleet_app "$prefix" "$slug")"; then
      changed+=("$slug")
    else
      added+=("$slug")
    fi
  done
  printf 'added=%s\n'   "$(_fleet_json_array "${added[@]+"${added[@]}"}")"
  printf 'changed=%s\n' "$(_fleet_json_array "${changed[@]+"${changed[@]}"}")"
}

# --- subcommand dispatch (only when executed, never when sourced) -------------
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -euo pipefail
  command -v "$FLEET_FLY" >/dev/null 2>&1 \
    || { echo "fleet.sh: flyctl not found" >&2; exit 1; }
  command -v jq >/dev/null 2>&1 \
    || { echo "fleet.sh: jq not found" >&2; exit 1; }
  cmd="${1:-}"; shift || true
  case "$cmd" in
    app)      fleet_app "$@";;
    list)     fleet_list "$@";;
    classify) fleet_classify "$@";;
    fleet-of) fleet_of "$@";;
    -h|--help|"") sed -n '2,/^# fleet_app/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d';;
    *) echo "fleet.sh: unknown subcommand '$cmd' (app|list|classify|fleet-of)" >&2; exit 2;;
  esac
fi
