#!/usr/bin/env bash
# Universal modastack installer
#
# One-liner:
#   curl -sL https://raw.githubusercontent.com/moda-labs/modastack/main/deploy/install.sh | bash
#
# Or download and run:
#   curl -sL https://raw.githubusercontent.com/moda-labs/modastack/main/deploy/install.sh -o install.sh
#   bash install.sh
#
# Flags:
#   --non-interactive   Skip all prompts, use defaults (for CI/automation)

set -euo pipefail

# ── Globals ──────────────────────────────────────────────────────────
INSTALL_DIR="${MODASTACK_DIR:-$HOME/dev/modastack}"
GSTACK_DIR="${GSTACK_DIR:-$HOME/dev/gstack}"
REPO_URL="https://github.com/moda-labs/modastack.git"
GSTACK_URL="https://github.com/garrytan/gstack.git"
LOG_FILE="$HOME/.modastack/install.log"
NON_INTERACTIVE=false
OS=""
DISTRO=""

for arg in "$@"; do
    case "$arg" in
        --non-interactive) NON_INTERACTIVE=true ;;
    esac
done

# ── Colors ───────────────────────────────────────────────────────────
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    DIM='\033[2m'
    RESET='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' BOLD='' DIM='' RESET=''
fi

# ── Helpers ──────────────────────────────────────────────────────────
info()    { echo -e "${BLUE}▸${RESET} $1"; }
success() { echo -e "${GREEN}✓${RESET} $1"; }
warn()    { echo -e "${YELLOW}!${RESET} $1"; }
fail()    { echo -e "${RED}✗${RESET} $1" >&2; exit 1; }

log() {
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

step() {
    echo ""
    echo -e "${BOLD}[$1/$STEP_TOTAL] $2${RESET}"
}

# Read from /dev/tty so this works in curl|bash mode
ask() {
    local prompt="$1" default="${2:-}"
    if $NON_INTERACTIVE; then echo "$default"; return; fi
    if [[ -n "$default" ]]; then
        echo -en "  ${prompt} [${default}]: " >/dev/tty
    else
        echo -en "  ${prompt}: " >/dev/tty
    fi
    local answer
    read -r answer < /dev/tty
    echo "${answer:-$default}"
}

ask_yn() {
    local prompt="$1" default="${2:-n}"
    if $NON_INTERACTIVE; then
        [[ "$default" == "y" ]] && return 0 || return 1
    fi
    local hint
    [[ "$default" == "y" ]] && hint="Y/n" || hint="y/N"
    echo -en "  ${prompt} (${hint}): " >/dev/tty
    local answer
    read -r answer < /dev/tty
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy] ]]
}

ask_choice() {
    local prompt="$1"
    shift
    local options=("$@")
    if $NON_INTERACTIVE; then echo "1"; return; fi
    echo "" >/dev/tty
    echo -e "  ${prompt}" >/dev/tty
    echo "" >/dev/tty
    for i in "${!options[@]}"; do
        echo -e "    $((i+1))) ${options[$i]}" >/dev/tty
    done
    echo "" >/dev/tty
    echo -en "  Choice [1]: " >/dev/tty
    local answer
    read -r answer < /dev/tty
    echo "${answer:-1}"
}

ask_secret() {
    local prompt="$1"
    if $NON_INTERACTIVE; then echo ""; return; fi
    echo -en "  ${prompt}: " >/dev/tty
    local answer
    read -rs answer < /dev/tty
    echo "" >/dev/tty
    echo "$answer"
}

command_exists() { command -v "$1" &>/dev/null; }

wait_for_enter() {
    if $NON_INTERACTIVE; then return; fi
    echo -en "  ${DIM}Press Enter to continue...${RESET}" >/dev/tty
    read -r < /dev/tty
}

# ── Error handling ───────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        echo ""
        echo -e "${RED}Setup failed.${RESET}"
        echo -e "  Log: ${LOG_FILE}"
        echo -e "  Re-run the installer to pick up where it left off."
    fi
}
trap cleanup EXIT

# ── OS Detection ─────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Darwin) OS="macos" ;;
        Linux)  OS="linux" ;;
        *)      fail "Unsupported OS: $(uname -s). modastack supports macOS and Linux." ;;
    esac

    if [[ "$OS" == "linux" ]]; then
        if [[ -f /etc/os-release ]]; then
            # shellcheck disable=SC1091
            source /etc/os-release
            case "$ID" in
                ubuntu|debian|pop|linuxmint) DISTRO="debian" ;;
                fedora|rhel|centos|rocky|almalinux) DISTRO="fedora" ;;
                arch|manjaro|endeavouros) DISTRO="arch" ;;
                alpine) DISTRO="alpine" ;;
                *) DISTRO="unknown" ;;
            esac
        else
            DISTRO="unknown"
        fi
    fi
}

# ── Package installation per OS ──────────────────────────────────────
install_brew_if_needed() {
    if command_exists brew; then return; fi
    info "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/tty
    # Add to PATH for this session
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
}

install_package() {
    local name="$1"
    local brew_name="${2:-$name}"

    if command_exists "$name"; then
        success "${name} $(${name} --version 2>/dev/null | head -1 || echo '(installed)')"
        return
    fi

    info "Installing ${name}..."
    case "$OS" in
        macos)
            install_brew_if_needed
            brew install "$brew_name" >> "$LOG_FILE" 2>&1
            ;;
        linux)
            case "$DISTRO" in
                debian)  sudo apt-get install -y -qq "$name" >> "$LOG_FILE" 2>&1 ;;
                fedora)  sudo dnf install -y -q "$name" >> "$LOG_FILE" 2>&1 ;;
                arch)    sudo pacman -S --noconfirm "$name" >> "$LOG_FILE" 2>&1 ;;
                alpine)  sudo apk add --quiet "$name" >> "$LOG_FILE" 2>&1 ;;
                *)       warn "Can't auto-install ${name} on this distro. Install it manually." ; return 1 ;;
            esac
            ;;
    esac
    success "${name} installed"
}

install_node() {
    if command_exists node; then
        local ver
        ver=$(node --version 2>/dev/null || echo "v0")
        local major="${ver#v}"
        major="${major%%.*}"
        if [[ "$major" -ge 18 ]]; then
            success "node ${ver}"
            return
        fi
        warn "Node.js ${ver} is too old (need 18+). Installing newer version..."
    fi

    info "Installing Node.js..."
    case "$OS" in
        macos)
            install_brew_if_needed
            brew install node >> "$LOG_FILE" 2>&1
            ;;
        linux)
            case "$DISTRO" in
                debian)
                    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >> "$LOG_FILE" 2>&1
                    sudo apt-get install -y -qq nodejs >> "$LOG_FILE" 2>&1
                    ;;
                fedora)
                    curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash - >> "$LOG_FILE" 2>&1
                    sudo dnf install -y -q nodejs >> "$LOG_FILE" 2>&1
                    ;;
                arch) sudo pacman -S --noconfirm nodejs npm >> "$LOG_FILE" 2>&1 ;;
                alpine) sudo apk add --quiet nodejs npm >> "$LOG_FILE" 2>&1 ;;
                *) warn "Install Node.js 18+ manually." ; return 1 ;;
            esac
            ;;
    esac
    success "node $(node --version)"
}

install_bun() {
    if command_exists bun; then
        success "bun $(bun --version 2>/dev/null)"
        return
    fi

    info "Installing Bun..."
    curl -fsSL https://bun.sh/install | bash >> "$LOG_FILE" 2>&1
    export BUN_INSTALL="$HOME/.bun"
    export PATH="$BUN_INSTALL/bin:$PATH"
    success "bun $(bun --version)"
}

install_gh() {
    if command_exists gh; then
        success "gh $(gh --version 2>/dev/null | head -1)"
        return
    fi

    info "Installing GitHub CLI..."
    case "$OS" in
        macos)
            install_brew_if_needed
            brew install gh >> "$LOG_FILE" 2>&1
            ;;
        linux)
            case "$DISTRO" in
                debian)
                    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
                        | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
                    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
                        | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
                    sudo apt-get update -qq >> "$LOG_FILE" 2>&1
                    sudo apt-get install -y -qq gh >> "$LOG_FILE" 2>&1
                    ;;
                fedora) sudo dnf install -y -q gh >> "$LOG_FILE" 2>&1 ;;
                arch) sudo pacman -S --noconfirm github-cli >> "$LOG_FILE" 2>&1 ;;
                alpine) sudo apk add --quiet github-cli >> "$LOG_FILE" 2>&1 ;;
                *) warn "Install gh CLI manually: https://cli.github.com/" ; return 1 ;;
            esac
            ;;
    esac
    success "gh installed"
}

install_claude() {
    if command_exists claude; then
        success "claude $(claude --version 2>/dev/null | head -1 || echo '(installed)')"
        return
    fi

    info "Installing Claude Code..."
    npm install -g @anthropic-ai/claude-code >> "$LOG_FILE" 2>&1
    success "claude installed"
}

# ── Check Python version ─────────────────────────────────────────────
ensure_python() {
    if ! command_exists python3; then
        install_package python3
    fi

    local ver
    ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local major="${ver%%.*}"
    local minor="${ver#*.}"

    if [[ "$major" -lt 3 ]] || [[ "$minor" -lt 11 ]]; then
        warn "Python ${ver} found — modastack needs 3.11+."
        info "Installing Python 3.12..."
        case "$OS" in
            macos)
                install_brew_if_needed
                brew install python@3.12 >> "$LOG_FILE" 2>&1
                ;;
            linux)
                case "$DISTRO" in
                    debian)
                        sudo apt-get install -y -qq python3.12 python3.12-venv >> "$LOG_FILE" 2>&1 || \
                            fail "Could not install Python 3.12. Add the deadsnakes PPA and retry."
                        ;;
                    *) fail "Install Python 3.11+ manually and re-run the installer." ;;
                esac
                ;;
        esac
    fi

    # Ensure venv module works (separate package on some distros)
    if [[ "$OS" == "linux" ]] && [[ "$DISTRO" == "debian" ]]; then
        python3 -m venv --help &>/dev/null 2>&1 || \
            sudo apt-get install -y -qq python3-venv >> "$LOG_FILE" 2>&1
    fi

    success "python3 ${ver}"
}

# ═════════════════════════════════════════════════════════════════════
#  Main flow
# ═════════════════════════════════════════════════════════════════════
STEP_TOTAL=11

echo ""
echo -e "${BOLD}  ╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}  ║         modastack installer              ║${RESET}"
echo -e "${BOLD}  ║   Event-driven AI engineering team       ║${RESET}"
echo -e "${BOLD}  ╚══════════════════════════════════════════╝${RESET}"
echo ""

log "Installer started (args: $*)"

# ── 1. Detect platform ──────────────────────────────────────────────
step 1 "Detecting platform"

detect_os
if [[ "$OS" == "macos" ]]; then
    success "macOS $(sw_vers -productVersion) ($(uname -m))"
else
    success "Linux — ${DISTRO} ($(uname -m))"
fi

# ── 2. System dependencies ──────────────────────────────────────────
step 2 "Installing system dependencies"

if [[ "$OS" == "linux" ]] && [[ "$DISTRO" == "debian" ]]; then
    sudo apt-get update -qq >> "$LOG_FILE" 2>&1
fi

install_package git
install_package curl
install_package jq
install_package unzip

# ── 3. Python ────────────────────────────────────────────────────────
step 3 "Checking Python"
ensure_python

# ── 4. Node.js + Bun + GH CLI + Claude Code ─────────────────────────
step 4 "Installing toolchain"
install_node
install_bun
install_gh
install_claude

# ── 5. Clone modastack ──────────────────────────────────────────────
step 5 "Installing modastack"

mkdir -p "$(dirname "$INSTALL_DIR")"
if [[ -d "$INSTALL_DIR" ]] && [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Already installed at ${INSTALL_DIR} — pulling latest"
    git -C "$INSTALL_DIR" pull origin main --ff-only >> "$LOG_FILE" 2>&1 || \
        warn "git pull failed — continuing with existing version"
elif [[ -d "$INSTALL_DIR" ]]; then
    fail "${INSTALL_DIR} exists but is not a git repo. Remove it or set MODASTACK_DIR."
else
    info "Cloning modastack..."
    git clone "$REPO_URL" "$INSTALL_DIR" >> "$LOG_FILE" 2>&1
fi
success "modastack at ${INSTALL_DIR}"

# ── 6. Python venv + pip ────────────────────────────────────────────
step 6 "Setting up Python environment"

if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    info "Creating virtual environment..."
    python3 -m venv "$INSTALL_DIR/.venv"
fi

# shellcheck disable=SC1091
source "$INSTALL_DIR/.venv/bin/activate"
pip install -e "$INSTALL_DIR" -q >> "$LOG_FILE" 2>&1
MODASTACK_VERSION=$(modastack --version 2>/dev/null || echo "installed")
success "modastack ${MODASTACK_VERSION}"

# ── 7. GStack skills ────────────────────────────────────────────────
step 7 "Installing GStack skills"

if [[ -d "$GSTACK_DIR" ]]; then
    info "Already installed — pulling latest"
    git -C "$GSTACK_DIR" pull origin main --ff-only >> "$LOG_FILE" 2>&1 || \
        warn "gstack pull failed — continuing with existing version"
else
    info "Cloning gstack..."
    git clone "$GSTACK_URL" "$GSTACK_DIR" >> "$LOG_FILE" 2>&1
fi

# Ensure bun deps are installed
(cd "$GSTACK_DIR" && bun install --frozen-lockfile >> "$LOG_FILE" 2>&1) || \
    (cd "$GSTACK_DIR" && bun install >> "$LOG_FILE" 2>&1) || true

# Run setup (quiet, flat skill names)
(cd "$GSTACK_DIR" && GSTACK_SKIP_COREUTILS=1 ./setup -q --no-prefix >> "$LOG_FILE" 2>&1) || \
    warn "gstack setup had warnings (non-fatal)"

# Configure for headless use
mkdir -p "$HOME/.gstack"
"$GSTACK_DIR/bin/gstack-config" set update_check false 2>/dev/null || true
"$GSTACK_DIR/bin/gstack-config" set routing_declined true 2>/dev/null || true
"$GSTACK_DIR/bin/gstack-config" set proactive true 2>/dev/null || true
"$GSTACK_DIR/bin/gstack-config" set telemetry off 2>/dev/null || true
touch "$HOME/.gstack/.proactive-prompted"
touch "$HOME/.gstack/.telemetry-prompted"
touch "$HOME/.gstack/.completeness-intro-seen"
touch "$HOME/.gstack/.welcome-seen"

# Link modastack skills
SKILLS_DIR="$INSTALL_DIR/.claude/skills"
mkdir -p "$SKILLS_DIR"
for skill_dir in \
    "$INSTALL_DIR/roles/engineer/process"/* \
    "$INSTALL_DIR/roles/engineer/practices"/* \
    "$INSTALL_DIR/roles/product_manager"/* \
    "$INSTALL_DIR/roles/tools"/*; do
    [[ -d "$skill_dir" ]] && [[ -f "$skill_dir/SKILL.md" ]] || continue
    name=$(basename "$skill_dir")
    link="$SKILLS_DIR/$name"
    rm -f "$link"
    ln -s "$(python3 -c "import os; print(os.path.relpath('$skill_dir', '$SKILLS_DIR'))")" "$link"
done

SKILL_COUNT=$(ls "$SKILLS_DIR" 2>/dev/null | wc -l | tr -d ' ')
success "${SKILL_COUNT} skills linked"

# ── 8. Authentication ───────────────────────────────────────────────
step 8 "Authentication"

echo ""
echo -e "  ${BOLD}Claude Code${RESET}"
if claude auth status &>/dev/null 2>&1; then
    success "Claude Code already authenticated"
else
    if $NON_INTERACTIVE; then
        warn "Claude Code not authenticated — run 'claude' to log in"
    else
        info "Claude Code needs authentication."
        info "This will open a browser window (or show a URL if headless)."
        wait_for_enter
        claude auth login < /dev/tty || warn "Auth skipped — run 'claude' later to authenticate"
    fi
fi

echo ""
echo -e "  ${BOLD}GitHub CLI${RESET}"
if gh auth status &>/dev/null 2>&1; then
    GH_USER=$(gh api user -q .login 2>/dev/null || echo "authenticated")
    success "GitHub CLI authenticated (${GH_USER})"
else
    if $NON_INTERACTIVE; then
        warn "GitHub CLI not authenticated — run 'gh auth login'"
    else
        info "GitHub CLI needs authentication."
        info "We'll use the browser flow."
        wait_for_enter
        gh auth login -w -p https < /dev/tty || warn "Auth skipped — run 'gh auth login' later"
    fi
fi

# ── 9. Task tracking ────────────────────────────────────────────────
step 9 "Task tracking"

TASK_SYSTEM="github-issues"
LINEAR_KEY=""
LINEAR_PROJECT=""

choice=$(ask_choice "Which task tracking system will you use?" \
    "GitHub Issues (no API key needed)" \
    "Linear (requires API key)" \
    "None (configure later)")

case "$choice" in
    1)
        TASK_SYSTEM="github-issues"
        success "Using GitHub Issues"
        ;;
    2)
        TASK_SYSTEM="linear"
        echo ""
        info "Create a Linear API key at: https://linear.app/settings/api"
        LINEAR_KEY=$(ask_secret "API key (lin_api_...)")

        if [[ -n "$LINEAR_KEY" ]]; then
            # Validate the key
            VALIDATE=$(curl -s -o /dev/null -w "%{http_code}" \
                -H "Authorization: $LINEAR_KEY" \
                -H "Content-Type: application/json" \
                -d '{"query":"{ viewer { id name } }"}' \
                https://api.linear.app/graphql)

            if [[ "$VALIDATE" == "200" ]]; then
                VIEWER=$(curl -s \
                    -H "Authorization: $LINEAR_KEY" \
                    -H "Content-Type: application/json" \
                    -d '{"query":"{ viewer { name } }"}' \
                    https://api.linear.app/graphql | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['viewer']['name'])" 2>/dev/null || echo "")
                success "Linear key valid${VIEWER:+ (${VIEWER})}"
            else
                warn "Could not validate Linear key (HTTP ${VALIDATE}) — saving anyway"
            fi

            LINEAR_PROJECT=$(ask "Default project prefix (e.g., ENG, BET)")
        else
            warn "No key entered — configure later in ~/.modastack/credentials.yaml"
        fi
        ;;
    3)
        TASK_SYSTEM=""
        info "Skipped — configure later"
        ;;
esac

# ── 10. Slack setup ─────────────────────────────────────────────────
step 10 "Slack integration"

SLACK_BOT_TOKEN=""
SLACK_APP_TOKEN=""

if ask_yn "Set up Slack integration?" "n"; then
    echo ""
    echo -e "  ${BOLD}Step 1: Create a Slack App${RESET}"
    echo "    1. Go to https://api.slack.com/apps"
    echo "    2. Click 'Create New App' → 'From scratch'"
    echo "    3. Name it 'Modabot' and pick your workspace"
    wait_for_enter

    echo ""
    echo -e "  ${BOLD}Step 2: Add Bot Scopes${RESET}"
    echo "    Go to OAuth & Permissions → Bot Token Scopes, add:"
    echo "      chat:write, channels:history, channels:read,"
    echo "      groups:history, groups:read, im:history, im:read, users:read"
    wait_for_enter

    echo ""
    echo -e "  ${BOLD}Step 3: Enable Socket Mode${RESET}"
    echo "    Go to Socket Mode → toggle ON"
    echo "    Generate an App-Level Token with scope: connections:write"
    echo ""
    SLACK_APP_TOKEN=$(ask_secret "App-Level Token (xapp-...)")

    if [[ -n "$SLACK_APP_TOKEN" ]] && [[ ! "$SLACK_APP_TOKEN" =~ ^xapp- ]]; then
        warn "Token doesn't start with xapp- — double-check this is the App-Level Token"
    fi

    echo ""
    echo -e "  ${BOLD}Step 4: Subscribe to Events${RESET}"
    echo "    Go to Event Subscriptions → toggle ON"
    echo "    Subscribe to bot events:"
    echo "      message.im, message.channels, message.groups, app_mention"
    wait_for_enter

    echo ""
    echo -e "  ${BOLD}Step 5: Install to Workspace${RESET}"
    echo "    Click 'Install to Workspace' at the top of OAuth & Permissions"
    echo ""
    SLACK_BOT_TOKEN=$(ask_secret "Bot User OAuth Token (xoxb-...)")

    if [[ -n "$SLACK_BOT_TOKEN" ]] && [[ ! "$SLACK_BOT_TOKEN" =~ ^xoxb- ]]; then
        warn "Token doesn't start with xoxb- — double-check this is the Bot Token"
    fi

    if [[ -n "$SLACK_BOT_TOKEN" ]]; then
        # Validate
        SLACK_TEST=$(curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
            https://slack.com/api/auth.test | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('team',''))" 2>/dev/null || echo "")
        if [[ -n "$SLACK_TEST" ]]; then
            success "Slack connected (workspace: ${SLACK_TEST})"
        else
            warn "Could not validate Slack token — saving anyway"
        fi

        echo ""
        echo -e "  ${BOLD}Step 6: Invite the bot${RESET}"
        echo "    In Slack, invite @Modabot to your engineering channel:"
        echo "      /invite @Modabot"
        wait_for_enter
    fi
else
    info "Skipped — configure later in ~/.modastack/config.yaml"
fi

# ── Write config files ──────────────────────────────────────────────
mkdir -p "$HOME/.modastack/manager"
mkdir -p "$HOME/.modastack/handoffs"
mkdir -p "$HOME/.modastack/logs"

# credentials.yaml
if [[ -n "$LINEAR_KEY" ]]; then
    CRED_NAME="${LINEAR_PROJECT:-default}"
    CRED_NAME=$(echo "$CRED_NAME" | tr '[:upper:]' '[:lower:]')
    if [[ -f "$HOME/.modastack/credentials.yaml" ]]; then
        # Append/update rather than overwrite
        python3 -c "
import yaml
from pathlib import Path
p = Path('$HOME/.modastack/credentials.yaml')
d = yaml.safe_load(p.read_text()) or {}
d.setdefault('$CRED_NAME', {})['linear_api_key'] = '$LINEAR_KEY'
p.write_text(yaml.dump(d, default_flow_style=False))
"
    else
        cat > "$HOME/.modastack/credentials.yaml" << CREDEOF
${CRED_NAME}:
  linear_api_key: "${LINEAR_KEY}"
CREDEOF
    fi
    success "Linear credentials saved"
fi

# config.yaml
GH_ACCOUNT=$(gh api user -q .login 2>/dev/null || echo "")
cat > "$HOME/.modastack/config.yaml" << CONFEOF
slack:
  bot_token: "${SLACK_BOT_TOKEN}"
  app_token: "${SLACK_APP_TOKEN}"

webhooks:
  port: 8080

github:
  default_account: "${GH_ACCOUNT}"

repos: []
CONFEOF
success "Config written to ~/.modastack/config.yaml"

# ── 11. Systemd service ─────────────────────────────────────────────
step 11 "Process management"

SERVICE_INSTALLED=false
if [[ "$OS" == "linux" ]]; then
    info "Installing systemd user service..."
    mkdir -p "$HOME/.config/systemd/user"
    cp "$INSTALL_DIR/deploy/modastack.service" "$HOME/.config/systemd/user/modastack.service"
    systemctl --user daemon-reload
    systemctl --user enable modastack
    loginctl enable-linger "$(whoami)" 2>/dev/null || true
    SERVICE_INSTALLED=true
    success "systemd service installed (auto-restarts on crash)"
else
    info "systemd not available on macOS — use 'modastack start' to run manually"
fi

# ── Launch ───────────────────────────────────────────────────────────
echo ""
STARTED=false
if ask_yn "Start modastack now?" "n"; then
    info "Initializing..."
    modastack init --non-interactive >> "$LOG_FILE" 2>&1 || true

    if $SERVICE_INSTALLED; then
        systemctl --user restart modastack
    else
        nohup bash -c "cd $INSTALL_DIR && source .venv/bin/activate && modastack start" \
            > "$HOME/.modastack/logs/modastack.log" 2>&1 &
    fi

    sleep 3
    STARTED=true
    success "modastack is running"
fi

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  modastack installed successfully${RESET}"
echo -e "${BOLD}  ════════════════════════════════════════════${RESET}"
echo ""
echo -e "  ${DIM}Location${RESET}     ${INSTALL_DIR}"
echo -e "  ${DIM}Config${RESET}       ~/.modastack/config.yaml"
echo -e "  ${DIM}Credentials${RESET}  ~/.modastack/credentials.yaml"
[[ -n "$TASK_SYSTEM" ]] && \
echo -e "  ${DIM}Tracking${RESET}     ${TASK_SYSTEM}"
[[ -n "$SLACK_BOT_TOKEN" ]] && \
echo -e "  ${DIM}Slack${RESET}        configured" || \
echo -e "  ${DIM}Slack${RESET}        not configured"
$SERVICE_INSTALLED && \
echo -e "  ${DIM}Service${RESET}      systemd (auto-restart)" || \
echo -e "  ${DIM}Service${RESET}      manual"
$STARTED && \
echo -e "  ${DIM}Status${RESET}       running" || \
echo -e "  ${DIM}Status${RESET}       not started"
echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo ""
echo "    # Activate the modastack environment"
echo "    source ${INSTALL_DIR}/.venv/bin/activate"
echo ""
echo "    # Register a repo"
echo "    modastack setup ~/path/to/your-repo"
echo ""
if $STARTED && $SERVICE_INSTALLED; then
echo "    # View logs"
echo "    journalctl --user -u modastack -f"
echo ""
echo "    # Restart"
echo "    systemctl --user restart modastack"
echo ""
elif $STARTED; then
echo "    # View logs"
echo "    tail -f ~/.modastack/logs/modastack.log"
echo ""
else
echo "    # Start modastack"
echo "    modastack start --webhooks"
echo ""
fi

# Add to PATH hint
SHELL_RC=""
case "$(basename "${SHELL:-bash}")" in
    zsh) SHELL_RC="~/.zshrc" ;;
    bash) SHELL_RC="~/.bashrc" ;;
    fish) SHELL_RC="~/.config/fish/config.fish" ;;
esac

if [[ -n "$SHELL_RC" ]]; then
    echo -e "  ${DIM}Add to ${SHELL_RC} for easy access:${RESET}"
    echo "    export PATH=\"${INSTALL_DIR}/.venv/bin:\$PATH\""
    echo ""
fi

log "Install complete"
