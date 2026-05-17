#!/usr/bin/env bash
set -euo pipefail

# agent-dispatch bootstrap
# Paste into any coding agent or run directly. Handles:
# 1. Clone (if not already installed)
# 2. Create venv + install
# 3. Run `dispatch init` (if no config exists)
# 4. Run `dispatch setup` in the current repo (if called from a repo)

INSTALL_DIR="${AGENT_DISPATCH_DIR:-$HOME/dev/agent-dispatch}"
REPO_URL="https://github.com/zkozick/agent-dispatch.git"

echo "==> agent-dispatch bootstrap"

# 1. Clone if needed
if [ ! -d "$INSTALL_DIR" ]; then
    echo "    Cloning to $INSTALL_DIR..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
else
    echo "    Already installed at $INSTALL_DIR"
fi

# 2. Create venv + install if needed
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    echo "    Creating venv..."
    python3 -m venv "$INSTALL_DIR/.venv"
fi

echo "    Installing dependencies..."
"$INSTALL_DIR/.venv/bin/pip" install -q -e "$INSTALL_DIR"

# Make dispatch available without activating venv
DISPATCH="$INSTALL_DIR/.venv/bin/dispatch"

# 3. Init global config if it doesn't exist
if [ ! -f "$HOME/.dispatch/config.yaml" ]; then
    echo "    Running dispatch init..."
    "$DISPATCH" init
fi

# 4. Setup current repo if we're in one
CURRENT_DIR="$(pwd)"
if git rev-parse --is-inside-work-tree &>/dev/null; then
    REPO_ROOT="$(git rev-parse --show-toplevel)"
    if [ ! -f "$REPO_ROOT/.dispatch.yaml" ]; then
        echo "    Setting up dispatch for: $REPO_ROOT"
        "$DISPATCH" setup "$REPO_ROOT"
    else
        echo "    .dispatch.yaml already exists in $REPO_ROOT"
        # Still register if not already
        "$DISPATCH" register "$REPO_ROOT" 2>/dev/null || true
    fi
else
    echo "    Not inside a git repo — skipping repo setup."
    echo "    Run 'dispatch setup' from inside a repo to wire it up."
fi

echo ""
echo "==> Done! Commands available at: $DISPATCH"
echo "    Or activate the venv: source $INSTALL_DIR/.venv/bin/activate"
echo ""
echo "    dispatch setup     # wire up any repo (auto-detects everything)"
echo "    dispatch cycle     # run one scan/dispatch cycle"
echo "    dispatch status    # check in-flight work"
