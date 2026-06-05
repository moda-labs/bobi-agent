#!/usr/bin/env bash
# Bootstrap a self-hosted GitHub Actions runner for modastack CI.
#
# Prerequisites: Ubuntu 22.04+ EC2 instance with SSH access.
# Claude CLI must be logged in after setup: `claude login`
#
# Usage:
#   ssh ubuntu@<ip> 'bash -s' < deploy/setup-ci-runner.sh
#
# After running, SSH in and:
#   1. claude login          # authenticate with your Anthropic account
#   2. Grab a runner token from:
#      https://github.com/moda-labs/modastack/settings/actions/runners/new
#   3. cd ~/actions-runner && ./config.sh --url https://github.com/moda-labs/modastack --token <TOKEN>
#   4. sudo ./svc.sh install && sudo ./svc.sh start

set -euo pipefail

echo "=== System packages ==="
sudo apt-get update -qq
sudo apt-get install -y -qq git curl python3 python3-pip python3-venv jq

echo "=== Python 3.13 ==="
if ! python3.13 --version 2>/dev/null; then
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.13 python3.13-venv python3.13-dev
fi

echo "=== Claude CLI ==="
if ! command -v claude &>/dev/null; then
    curl -fsSL https://claude.ai/install.sh | sh
    echo 'export PATH="$HOME/.claude/bin:$PATH"' >> ~/.bashrc
    export PATH="$HOME/.claude/bin:$PATH"
fi
claude --version

echo "=== GitHub Actions runner ==="
RUNNER_DIR="$HOME/actions-runner"
if [ ! -d "$RUNNER_DIR" ]; then
    mkdir -p "$RUNNER_DIR" && cd "$RUNNER_DIR"
    RUNNER_VERSION=$(curl -s https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
    curl -fsSL -o runner.tar.gz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
    tar xzf runner.tar.gz
    rm runner.tar.gz
    echo "Runner downloaded to $RUNNER_DIR"
else
    echo "Runner already installed at $RUNNER_DIR"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. claude login"
echo "  2. Get a runner token from:"
echo "     https://github.com/moda-labs/modastack/settings/actions/runners/new"
echo "  3. cd ~/actions-runner"
echo "     ./config.sh --url https://github.com/moda-labs/modastack --token <TOKEN> --labels self-hosted,modastack-ci"
echo "  4. sudo ./svc.sh install && sudo ./svc.sh start"
