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

    # Install system deps (unzip for bun, libs for Playwright Chromium)
    sudo apt-get install -y -qq unzip >> "$LOG" 2>&1 || true

    # Install bun if needed (required by gstack setup)
    if ! command -v bun &>/dev/null; then
        curl -fsSL https://bun.sh/install | bash >> "$LOG" 2>&1
        export BUN_INSTALL="$HOME/.bun"
        export PATH="$BUN_INSTALL/bin:$PATH"
    fi

    # Ensure bun is on PATH (may have been installed in a previous run)
    export PATH="$HOME/.bun/bin:$PATH"

    # Run gstack setup (quiet, flat skill names)
    cd "$GSTACK_DIR"
    bun install --frozen-lockfile >> "$LOG" 2>&1 || bun install >> "$LOG" 2>&1
    bunx playwright install-deps chromium >> "$LOG" 2>&1 || true
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

# Reinstall skill symlinks (roles/ replaces old engineer/, product_manager/, tools/)
SKILLS_DIR="$REPO_DIR/.claude/skills"
mkdir -p "$SKILLS_DIR"
for skill_dir in \
    "$REPO_DIR/roles/engineer/process"/* \
    "$REPO_DIR/roles/engineer/practices"/* \
    "$REPO_DIR/roles/product_manager"/* \
    "$REPO_DIR/roles/tools"/*; do
    [ -d "$skill_dir" ] && [ -f "$skill_dir/SKILL.md" ] || continue
    name=$(basename "$skill_dir")
    link="$SKILLS_DIR/$name"
    rm -f "$link"
    ln -s "$(python3 -c "import os; print(os.path.relpath('$skill_dir', '$SKILLS_DIR'))")" "$link"
done
log "Skill symlinks refreshed ($(ls "$SKILLS_DIR" | wc -l | tr -d ' ') skills)"

# Stop consumer first so it doesn't inject into a dying manager
tmux kill-session -t modastack-consumer 2>/dev/null || true
tmux kill-session -t modastack-dashboard 2>/dev/null || true
# Kill any orphaned modastack processes still holding the webhook port
pkill -f "modastack start" 2>/dev/null || true
sleep 1

# Restart manager (must be ready before consumer starts)
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

# Wait for manager to be idle before starting consumer
# (consumer's readiness gate also checks, but this avoids the initial batch failure)
sleep 5

# Start consumer (its readiness gate will wait for manager to be fully ready)
tmux new-session -d -s modastack-consumer \
    "bash -c 'cd $REPO_DIR && source .venv/bin/activate && modastack start --webhooks'"

# Restart dashboard
sleep 1
tmux new-session -d -s modastack-dashboard \
    "bash -c 'cd $REPO_DIR && source .venv/bin/activate && modastack dashboard'"

NEW_VERSION=$(git rev-parse --short HEAD)
log "Deploy complete — now at $NEW_VERSION"
