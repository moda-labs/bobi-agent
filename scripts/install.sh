#!/usr/bin/env bash
# Install modastack
#
# One-liner:
#   curl -sL https://raw.githubusercontent.com/moda-labs/modastack/main/scripts/install.sh | bash

set -euo pipefail

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing modastack..."
uv tool install modastack

echo ""
echo "Done. Run 'modastack start <agent-pack>' to get started."
