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

release_write_guard() {
    # Bobi makes its own installed package read-only before running an agent
    # (bobi/runtime_guard.py). POSIX governs unlink by the *directory* write
    # bit, so uv cannot delete the tree it needs to replace and the upgrade
    # aborts partway - after the entrypoint and every dependency are already
    # gone. That state is exactly when `bobi guard release` is unavailable,
    # because `bobi` no longer imports, so unlock from the shell instead.
    local tool_dir
    tool_dir="$(uv tool dir 2>/dev/null || true)"
    if [ -n "$tool_dir" ] && [ -d "$tool_dir/bobi" ]; then
        echo "Releasing Bobi's runtime write guard..."
        chmod -R u+w "$tool_dir/bobi" || true
    fi
    # Hold the guard off while uv works. A running team re-applies it on every
    # agent launch, and monitor ticks land every 30s - far sooner than uv can
    # resolve, build, and swap the tree. Bobi reads this marker's mtime and
    # skips its own install while the window is open.
    local home_dir="${BOBI_HOME:-$HOME/.bobi}"
    mkdir -p "$home_dir" && touch "$home_dir/runtime-guard-released" || true
}

require_supported_node

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

release_write_guard

echo "Installing bobi..."
# --force is what makes this script an upgrade and a repair, not just a first
# install. A guard-aborted upgrade leaves a gutted environment whose uv receipt
# still matches, so a plain `uv tool install bobi` re-links the executable,
# exits 0, and leaves `bobi` unable to import.
uv tool install --force bobi

echo ""
echo "Done. Run 'bobi setup <name>' to create a Bobi Agent, or"
echo "'bobi agents install <source> --name <name>' to install one."
