#!/bin/bash
# Modastack EC2 Setup
#
# Usage:
#   1. Launch Ubuntu 24.04 EC2 instance (t3.medium+, 30GB+ disk)
#   2. Security group: open port 22 (SSH) and 8080 (webhooks)
#   3. SSH in and run: curl -sL https://raw.githubusercontent.com/underminedsk/modastack/main/deploy/setup-ec2.sh | bash
#
# After setup, you'll need to manually:
#   - Authenticate Claude Code (interactive, one-time)
#   - Authenticate GitHub CLI (interactive, one-time)
#   - Copy credentials from your local machine

set -euo pipefail

echo "=== Modastack EC2 Setup ==="
echo ""

# --- System packages ---
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq tmux git curl unzip python3 python3-pip python3-venv jq

# --- Node.js (for Claude Code) ---
echo "[2/7] Installing Node.js..."
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y -qq nodejs
fi

# --- Claude Code CLI ---
echo "[3/7] Installing Claude Code..."
if ! command -v claude &>/dev/null; then
    npm install -g @anthropic-ai/claude-code
fi
echo "  Claude Code: $(claude --version 2>/dev/null || echo 'installed, needs auth')"

# --- GitHub CLI ---
echo "[4/7] Installing GitHub CLI..."
if ! command -v gh &>/dev/null; then
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq gh
fi
echo "  gh: $(gh --version | head -1)"

# --- Bun (for gstack) ---
echo "[5/9] Installing Bun..."
if ! command -v bun &>/dev/null; then
    curl -fsSL https://bun.sh/install | bash
    export BUN_INSTALL="$HOME/.bun"
    export PATH="$BUN_INSTALL/bin:$PATH"
fi
echo "  bun: $(bun --version 2>/dev/null || echo 'failed')"

# --- Clone modastack ---
echo "[6/9] Cloning modastack..."
INSTALL_DIR="${HOME}/dev/modastack"
mkdir -p "${HOME}/dev"
if [ -d "$INSTALL_DIR" ]; then
    echo "  Already exists at $INSTALL_DIR — pulling latest"
    git -C "$INSTALL_DIR" pull origin main
else
    git clone https://github.com/underminedsk/modastack.git "$INSTALL_DIR"
fi

# --- Python venv + install ---
echo "[7/9] Setting up Python environment..."
cd "$INSTALL_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]" -q

echo "  modastack: $(modastack --version 2>/dev/null)"

# --- GStack (methodology skills for engineer sessions) ---
echo "[8/9] Installing GStack skills..."
GSTACK_DIR="${HOME}/dev/gstack"
if [ -d "$GSTACK_DIR" ]; then
    echo "  Already exists at $GSTACK_DIR — pulling latest"
    git -C "$GSTACK_DIR" pull origin main
else
    git clone https://github.com/garrytan/gstack.git "$GSTACK_DIR"
fi

# Run gstack setup (quiet, no prefix for flat skill names)
cd "$GSTACK_DIR"
GSTACK_SKIP_COREUTILS=1 ./setup -q --no-prefix || echo "  warning: gstack setup had errors (non-fatal)"
cd "$INSTALL_DIR"

# Configure gstack for headless/automated use — suppress all interactive prompts
mkdir -p ~/.gstack
"$GSTACK_DIR/bin/gstack-config" set update_check false 2>/dev/null || true
"$GSTACK_DIR/bin/gstack-config" set routing_declined true 2>/dev/null || true
"$GSTACK_DIR/bin/gstack-config" set proactive true 2>/dev/null || true
"$GSTACK_DIR/bin/gstack-config" set telemetry off 2>/dev/null || true
# Create marker files so preamble never fires AskUserQuestion
touch ~/.gstack/.proactive-prompted
touch ~/.gstack/.telemetry-prompted
touch ~/.gstack/.completeness-intro-seen
touch ~/.gstack/.welcome-seen

echo "  gstack: installed at $GSTACK_DIR"
echo "  skills: $(ls ~/.claude/skills/ 2>/dev/null | wc -l | tr -d ' ') skills linked"

# --- Config directory ---
echo "[9/9] Setting up config..."
mkdir -p ~/.modastack/manager

if [ ! -f ~/.modastack/config.yaml ]; then
    cat > ~/.modastack/config.yaml << 'EOF'
# Modastack instance configuration
# Fill in your tokens after setup

slack:
  bot_token: ""      # xoxb-... from Slack app
  app_token: ""      # xapp-... from Slack Socket Mode

webhooks:
  port: 8080

github:
  default_account: ""  # your GitHub username

repos: []
EOF
    echo "  Created ~/.modastack/config.yaml — fill in your tokens"
else
    echo "  Config already exists"
fi

if [ ! -f ~/.modastack/credentials.yaml ]; then
    cat > ~/.modastack/credentials.yaml << 'EOF'
# Linear API keys — one per workspace
# example:
#   myproject:
#     linear_api_key: "lin_api_..."
EOF
    echo "  Created ~/.modastack/credentials.yaml — add your Linear keys"
else
    echo "  Credentials already exist"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps (manual, one-time):"
echo ""
echo "  1. Authenticate Claude Code:"
echo "     source ~/modastack/.venv/bin/activate"
echo "     claude"
echo "     # Follow the browser auth flow"
echo ""
echo "  2. Authenticate GitHub:"
echo "     gh auth login"
echo ""
echo "  3. Copy credentials from your local machine:"
echo "     # On your local machine, run:"
echo "     scp ~/.modastack/config.yaml     ec2-user@<EC2_IP>:~/.modastack/config.yaml"
echo "     scp ~/.modastack/credentials.yaml ec2-user@<EC2_IP>:~/.modastack/credentials.yaml"
echo ""
echo "  4. Start modastack:"
echo "     tmux new -s modastack"
echo "     source ~/modastack/.venv/bin/activate"
echo "     modastack start --webhooks"
echo "     # Ctrl-B D to detach"
echo ""
echo "  5. Set up repos via Slack (no more SSH needed):"
echo "     DM Modabot: \"set up moda-labs/bettertab\""
echo "     DM Modabot: \"set up myorg/my-repo --linear-project PROJ\""
echo ""
echo "  To reconnect later:"
echo "     ssh ec2-user@<EC2_IP>"
echo "     tmux attach -t modastack"
