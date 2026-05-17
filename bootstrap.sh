#!/usr/bin/env bash
set -euo pipefail

# agentd bootstrap
# Paste into any coding agent or run directly. Handles:
# 1. Clone (if not already installed)
# 2. Create venv + install
# 3. Run `dispatch init` (if no config exists)
# 4. Run `dispatch setup` in the current repo (if called from a repo)

REPO_URL="https://github.com/underminedsk/agentd.git"

echo "==> agentd bootstrap"

# 1. Detect install location — if we're inside the repo already, use it
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$SCRIPT_DIR/dispatch/__init__.py" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
elif [ -f "./dispatch/__init__.py" ]; then
    INSTALL_DIR="$(pwd)"
else
    INSTALL_DIR="${AGENTD_DIR:-$HOME/dev/agentd}"
fi

# Clone only if truly not present
if [ ! -f "$INSTALL_DIR/dispatch/__init__.py" ]; then
    echo "    Cloning to $INSTALL_DIR..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
else
    echo "    Found at $INSTALL_DIR (skipping clone)"
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

# 3. Ensure gstack is installed (required dependency)
if [ ! -d "$HOME/.claude/skills/gstack" ]; then
    echo "    Installing gstack (required for review/ship/plan skills)..."
    git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git "$HOME/.claude/skills/gstack"
    (cd "$HOME/.claude/skills/gstack" && ./setup 2>/dev/null)
else
    echo "    gstack found at ~/.claude/skills/gstack"
fi

# 4. Init global config if it doesn't exist
if [ ! -f "$HOME/.dispatch/config.yaml" ]; then
    echo "    Initializing config (non-interactive)..."
    "$DISPATCH" init --non-interactive
    echo "    Config created at ~/.dispatch/config.yaml"
    echo "    Add your Linear API key: dispatch init --linear-key YOUR_KEY"
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
