#!/usr/bin/env bash
# Install bobi
#
# One-liner:
#   curl -sL https://raw.githubusercontent.com/moda-labs/bobi-agent/main/scripts/install.sh | bash

set -euo pipefail

require_supported_node() {
    local version
    local major

    if ! command -v node &>/dev/null; then
        echo "Node.js 20+ is required for Bobi's local event server, but node was not found on PATH." >&2
        echo "Install Node.js 20 or newer, ensure node is on PATH, and rerun this installer." >&2
        exit 1
    fi
    if ! version="$(node --version 2>/dev/null)"; then
        echo "Node.js 20+ is required, but 'node --version' failed." >&2
        echo "Repair or upgrade Node.js and rerun this installer." >&2
        exit 1
    fi
    major="${version#v}"
    major="${major%%.*}"
    case "$major" in
        ""|*[!0-9]*)
            echo "Node.js 20+ is required, but the installed version could not be parsed: $version" >&2
            exit 1
            ;;
    esac
    if (( major < 20 )); then
        echo "Node.js 20+ is required for Bobi's local event server; found $version." >&2
        echo "Upgrade Node.js, ensure the newer node is on PATH, and rerun this installer." >&2
        exit 1
    fi
}

require_supported_node

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
