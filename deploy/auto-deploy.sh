#!/bin/bash
# Auto-deploy: pulls latest main, reinstalls, restarts modastack.
# Called by cron via check-deploy.sh when new commits are detected.

set -euo pipefail

REPO_DIR="$HOME/dev/modastack"
LOG="$HOME/.modastack/deploy.log"
LOCK="$HOME/.modastack/deploy.lock"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"; }

# Prevent concurrent deploys
if [ -f "$LOCK" ]; then
    log "Deploy already in progress — skipping"
    exit 0
fi
trap "rm -f $LOCK" EXIT
touch "$LOCK"

cd "$REPO_DIR"

log "Deploy starting"

# Pull modastack
if ! git pull origin main --ff-only >> "$LOG" 2>&1; then
    log "ERROR: git pull failed — local changes or conflicts"
    exit 1
fi

# Ensure gstack is installed (methodology skills for engineer sessions)
GSTACK_DIR="$HOME/dev/gstack"
if [ ! -d "$GSTACK_DIR" ]; then
    log "Installing gstack (first time)..."
    mkdir -p "$HOME/dev"
    git clone https://github.com/garrytan/gstack.git "$GSTACK_DIR" >> "$LOG" 2>&1

    # Install bun if needed (required by gstack setup)
    if ! command -v bun &>/dev/null; then
        curl -fsSL https://bun.sh/install | bash >> "$LOG" 2>&1
        export BUN_INSTALL="$HOME/.bun"
        export PATH="$BUN_INSTALL/bin:$PATH"
    fi

    # Run gstack setup (quiet, flat skill names)
    cd "$GSTACK_DIR"
    GSTACK_SKIP_COREUTILS=1 ./setup -q --no-prefix >> "$LOG" 2>&1 || log "WARNING: gstack setup had errors (non-fatal)"
    cd "$REPO_DIR"

    # Configure for headless/automated use — suppress all interactive prompts
    mkdir -p "$HOME/.gstack"
    "$GSTACK_DIR/bin/gstack-config" set update_check false 2>/dev/null || true
    "$GSTACK_DIR/bin/gstack-config" set routing_declined true 2>/dev/null || true
    "$GSTACK_DIR/bin/gstack-config" set proactive true 2>/dev/null || true
    "$GSTACK_DIR/bin/gstack-config" set telemetry off 2>/dev/null || true
    touch "$HOME/.gstack/.proactive-prompted"
    touch "$HOME/.gstack/.telemetry-prompted"
    touch "$HOME/.gstack/.completeness-intro-seen"
    touch "$HOME/.gstack/.welcome-seen"
    log "gstack installed and configured for headless use"
else
    git -C "$GSTACK_DIR" pull origin main --ff-only >> "$LOG" 2>&1 || log "WARNING: gstack pull failed (non-fatal)"
fi

# Reinstall
source .venv/bin/activate
if ! pip install -e . -q >> "$LOG" 2>&1; then
    log "ERROR: pip install failed"
    exit 1
fi

# Restart consumer (kills old, starts new)
tmux kill-session -t modastack-consumer 2>/dev/null || true
sleep 1
tmux new-session -d -s modastack-consumer \
    "cd $REPO_DIR && source .venv/bin/activate && modastack start --webhooks"

# Restart dashboard
tmux kill-session -t modastack-dashboard 2>/dev/null || true
sleep 1
tmux new-session -d -s modastack-dashboard \
    "cd $REPO_DIR && source .venv/bin/activate && modastack dashboard"

# Restart manager (kill old, start new with auto-accept)
tmux kill-session -t moda-manager 2>/dev/null || true
rm -f "$HOME/.modastack/manager/session_id"
sleep 1
tmux new-session -d -s moda-manager -x 200 -y 50 \
    "cd $REPO_DIR && claude --dangerously-skip-permissions --name modastack-manager"

# Wait for trust/permissions prompts and auto-accept
sleep 3
tmux send-keys -t moda-manager Down 2>/dev/null
sleep 0.3
tmux send-keys -t moda-manager Enter 2>/dev/null

NEW_VERSION=$(git rev-parse --short HEAD)
log "Deploy complete — now at $NEW_VERSION"
