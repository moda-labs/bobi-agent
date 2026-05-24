#!/bin/bash
# Cron job: check if origin/main has new commits, deploy if so.
# Install: crontab -e → * * * * * ~/dev/modastack/deploy/check-deploy.sh

REPO_DIR="$HOME/dev/modastack"

cd "$REPO_DIR" || exit 1

git fetch origin main -q 2>/dev/null || exit 0

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    exec "$REPO_DIR/deploy/auto-deploy.sh"
fi
