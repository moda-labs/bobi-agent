#!/bin/bash
# Auto-deploy: pulls latest main, reinstalls, restarts modastack.
# Called by cron when new commits are detected on origin/main.

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

# Pull
if ! git pull origin main --ff-only >> "$LOG" 2>&1; then
    log "ERROR: git pull failed — local changes or conflicts"
    exit 1
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
