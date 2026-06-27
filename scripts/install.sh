#!/usr/bin/env bash
# Install bobi
#
# One-liner:
#   curl -sL https://raw.githubusercontent.com/moda-labs/bobi/main/scripts/install.sh | bash

set -euo pipefail

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing bobi..."
uv tool install bobi

echo ""
echo "Done. Run 'bobi setup <name>' to create a Bobi Agent, or"
echo "'bobi agents install <source> --name <name>' to install one."
