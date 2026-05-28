#!/usr/bin/env bash
# DEPRECATED — use deploy/install.sh instead.
#
# One-liner:
#   curl -sL https://raw.githubusercontent.com/moda-labs/modastack/main/deploy/install.sh | bash

echo "bootstrap.sh is deprecated. Running deploy/install.sh instead..."
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [[ -f "$SCRIPT_DIR/deploy/install.sh" ]]; then
    exec bash "$SCRIPT_DIR/deploy/install.sh" "$@"
else
    exec bash -c "$(curl -fsSL https://raw.githubusercontent.com/moda-labs/modastack/main/deploy/install.sh)" -- "$@"
fi
