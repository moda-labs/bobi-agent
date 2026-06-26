#!/usr/bin/env bash
#
# destroy-instance.sh — tear down one bobi instance on Fly (C10 / #340).
#
# Destroying the app removes its machines AND its volume. The volume is the only
# copy of the instance's state: project config, sessions, ~/.claude transcripts,
# subscription OAuth credentials, and KBs (design §2, §3). There is no undo. Back
# up first if you need any of it (C11).
#
# Usage:
#   scripts/destroy-instance.sh --app APP [--yes]
#
#   --app APP   The Fly app to destroy.
#   --yes       Skip the typed-confirmation prompt (for automation).
#   -h, --help  Show this help.

set -euo pipefail

APP="" ASSUME_YES="0"
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2;;
    --yes) ASSUME_YES="1"; shift;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'; exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done

log()   { echo "[destroy] $*"; }
fatal() { echo "[destroy] FATAL: $*" >&2; exit 1; }

[ -n "$APP" ] || fatal "--app is required."

FLY="fly"
command -v fly >/dev/null 2>&1 || FLY="flyctl"
command -v "$FLY" >/dev/null 2>&1 || fatal "flyctl not found."
"$FLY" auth whoami >/dev/null 2>&1 || fatal "Not logged in to Fly. Run: $FLY auth login"
"$FLY" status -a "$APP" >/dev/null 2>&1 || fatal "App '$APP' not found (or not yours)."

echo
echo "  This DESTROYS Fly app '$APP', its machines, AND its volume."
echo "  The volume holds the only copy of this instance's state — it is GONE after this."
echo

if [ "$ASSUME_YES" != "1" ]; then
  read -r -p "Type the app name ($APP) to confirm: " reply
  [ "$reply" = "$APP" ] || fatal "Confirmation did not match. Aborted."
fi

log "Destroying app '$APP'..."
"$FLY" apps destroy "$APP" --yes
log "Done. '$APP' and its volume are gone."
