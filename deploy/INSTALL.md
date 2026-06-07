# Install modastack

You are an AI coding agent installing modastack on this machine. Follow these
instructions step by step. Run commands, ask the user questions when needed,
and debug any failures using the troubleshooting section at the bottom.

## Overview

modastack is an event-driven AI engineering team — a persistent Claude Code
manager that monitors task trackers, GitHub, and Slack, assigning work to
engineer sessions. You are installing:

1. System dependencies (git, python3, node, bun, gh, claude, tmux)
2. The modastack repo + Python environment
3. GStack (methodology skills for engineer sessions)
4. Configuration (task tracking, Slack)

## Step 1: Detect platform

Run `uname -s` and `uname -m` to determine OS and architecture.

- **macOS**: use `brew` for packages. Install Homebrew first if missing.
- **Linux**: detect distro from `/etc/os-release`. Use `apt` (Debian/Ubuntu),
  `dnf` (Fedora/RHEL), `pacman` (Arch), or `apk` (Alpine).

## Step 2: Install system dependencies

Check each tool and install only what's missing. Run all checks first,
then install in a single batch.

### Required tools

| Tool | Min version | macOS | Debian/Ubuntu | Check |
|------|------------|-------|---------------|-------|
| git | any | `brew install git` | `sudo apt install -y git` | `git --version` |
| python3 | 3.11+ | `brew install python@3.12` | `sudo apt install -y python3 python3-venv` | `python3 --version` |
| node | 18+ | `brew install node` | See NodeSource below | `node --version` |
| jq | any | `brew install jq` | `sudo apt install -y jq` | `jq --version` |
| curl | any | (preinstalled) | `sudo apt install -y curl` | `curl --version` |
| unzip | any | (preinstalled) | `sudo apt install -y unzip` | `unzip -v` |
| tmux | any | `brew install tmux` | `sudo apt install -y tmux` | `tmux -V` (optional — process wrapper) |

**Node.js on Linux (if not installed or <18):**
```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

**Bun (all platforms):**
```bash
curl -fsSL https://bun.sh/install | bash
export BUN_INSTALL="$HOME/.bun"
export PATH="$BUN_INSTALL/bin:$PATH"
```

**GitHub CLI:**
- macOS: `brew install gh`
- Debian/Ubuntu:
```bash
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install -y gh
```

**Claude Code:**
```bash
npm install -g @anthropic-ai/claude-code
```

## Step 3: Install modastack

### Via uv (recommended)

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install modastack
uv tool install modastack
```

### Via pip

```bash
pip install modastack
```

### From source (development)

```bash
INSTALL_DIR="${MODASTACK_DIR:-$HOME/dev/modastack}"
mkdir -p "$(dirname "$INSTALL_DIR")"

if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull origin main --ff-only
else
    git clone https://github.com/moda-labs/modastack.git "$INSTALL_DIR"
fi

python3 -m venv "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"
pip install -e "$INSTALL_DIR" -q
```

### Verify

```bash
modastack --version
```

## Step 4: Install GStack skills

```bash
GSTACK_DIR="${GSTACK_DIR:-$HOME/dev/gstack}"

if [ -d "$GSTACK_DIR" ]; then
    git -C "$GSTACK_DIR" pull origin main --ff-only
else
    git clone https://github.com/garrytan/gstack.git "$GSTACK_DIR"
fi

cd "$GSTACK_DIR"
bun install --frozen-lockfile || bun install
GSTACK_SKIP_COREUTILS=1 ./setup -q --no-prefix
cd -
```

Configure gstack for headless use (suppress interactive prompts):
```bash
mkdir -p ~/.gstack
"$GSTACK_DIR/bin/gstack-config" set update_check false
"$GSTACK_DIR/bin/gstack-config" set routing_declined true
"$GSTACK_DIR/bin/gstack-config" set proactive true
"$GSTACK_DIR/bin/gstack-config" set telemetry off
touch ~/.gstack/.proactive-prompted
touch ~/.gstack/.telemetry-prompted
touch ~/.gstack/.completeness-intro-seen
touch ~/.gstack/.welcome-seen
```

Link modastack skills:
```bash
INSTALL_DIR="${MODASTACK_DIR:-$HOME/dev/modastack}"
SKILLS_DIR="$INSTALL_DIR/.claude/skills"
mkdir -p "$SKILLS_DIR"
for skill_dir in \
    "$INSTALL_DIR/roles/engineer/process"/* \
    "$INSTALL_DIR/roles/engineer/practices"/* \
    "$INSTALL_DIR/roles/product_manager"/* \
    "$INSTALL_DIR/roles/tools"/*; do
    [ -d "$skill_dir" ] && [ -f "$skill_dir/SKILL.md" ] || continue
    name=$(basename "$skill_dir")
    link="$SKILLS_DIR/$name"
    rm -f "$link"
    ln -s "$(python3 -c "import os; print(os.path.relpath('$skill_dir', '$SKILLS_DIR'))")" "$link"
done
echo "$(ls "$SKILLS_DIR" | wc -l | tr -d ' ') skills linked"
```

## Step 4b: Chromium sandbox for /browse

The engineer skills use gstack's `/browse` — a headless Chromium driven by
Playwright — for QA and dogfooding. On **Ubuntu 23.10+ and other recent
kernels**, this needs one piece of host configuration, or the browser won't
launch at all.

### The problem

Chromium isolates each renderer in a sandbox built on **unprivileged user
namespaces**. Ubuntu 23.10+ ships an AppArmor policy that restricts
unprivileged user namespaces by default:

```
kernel.apparmor_restrict_unprivileged_userns = 1
```

With this set, Chromium cannot create its sandbox and fails to start. You'll
see errors like `No usable sandbox!` or
`Failed to move to new namespace`, and `/browse` will be unable to take a
snapshot or navigate.

Check the current value:
```bash
cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns
# 1 = restricted (Chromium sandbox blocked), 0 = unrestricted (works)
# (file absent = older kernel, restriction doesn't apply)
```

### Fixes (pick one)

**Recommended — per-binary AppArmor exception.** Allow only Chromium to use
unprivileged user namespaces, leaving the restriction in place for everything
else. Create an AppArmor profile naming the Playwright Chromium binary:

```bash
CHROME=$(ls -d ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome | sort -V | tail -1)
sudo tee /etc/apparmor.d/chromium-playwright >/dev/null <<EOF
abi <abi/4.0>,
include <tunables/global>

profile chromium-playwright "$CHROME" flags=(unconfined) {
  userns,
  include if exists <local/chromium-playwright>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/chromium-playwright
```

This is the narrowest change — the userns attack surface stays closed for all
other processes. The tradeoff is maintenance: the profile pins a specific
Chromium path, so it must be refreshed when Playwright upgrades to a new
Chromium revision (a different `chromium-<rev>` directory).

**Quick fix — global sysctl disable.** Acceptable on a dedicated dev machine.
Disables the restriction for the whole host:

```bash
# Apply now
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0

# Persist across reboots
echo "kernel.apparmor_restrict_unprivileged_userns = 0" | \
    sudo tee /etc/sysctl.d/99-chromium-sandbox.conf
```

> **Security tradeoff:** this lets *any* unprivileged process on the machine
> create user namespaces. User namespaces have historically been an attack
> surface for container escapes and local privilege escalation, which is
> exactly why Ubuntu restricts them by default. On a shared or
> internet-exposed host, prefer the per-binary exception above. On a
> single-tenant dev box this is a reasonable, common choice.

**Fallback — run Chromium with `--no-sandbox`.** If you can't change host
sysctl/AppArmor at all (e.g. an unprivileged container), launch Chromium with
its internal sandbox disabled. This is the **least secure** option — it turns
off Chromium's own renderer isolation, so only do it where the browser visits
trusted pages. Set it for gstack browse via the Playwright launch args, e.g.:

```bash
export PLAYWRIGHT_CHROMIUM_ARGS="--no-sandbox"
```

(Some setups also honor `CHROMIUM_FLAGS`/`BROWSER_ARGS` — check your gstack
browse version.)

### Automated detection

`modastack setup` runs a quick headless Chromium launch at the end and, if it
detects this exact sandbox failure, explains the issue and offers to apply the
sysctl fix (with a sudo confirmation). You can also check or fix it any time:

```bash
modastack doctor          # report Playwright / Chromium / browse daemon health
modastack doctor --fix    # offer to apply the sandbox fix if that's the cause
```

## Step 5: Authentication

### Claude Code

Check: `claude auth status`

If not authenticated, tell the user:
> Claude Code needs to be authenticated. Please run `! claude auth login`
> in this terminal and follow the browser flow.

Wait for them to confirm it's done, then verify with `claude auth status`.

### GitHub CLI

Check: `gh auth status`

If not authenticated, tell the user:
> GitHub CLI needs to be authenticated. Please run `! gh auth login`
> and follow the prompts (browser flow recommended, HTTPS protocol).

After auth, verify: `gh api user -q .login`

## Step 6: Configure — ask the user

This is where you ask the user questions to set up their instance.
Use your agent UI to ask these questions (not shell prompts).

### 6a: Task tracking system

Ask the user which task tracking system they want to use:

- **GitHub Issues** (default) — no API key needed, uses `gh` CLI auth
- **Linear** — requires an API key

If they choose Linear:
1. Ask for their Linear API key. Tell them: "Create one at
   https://linear.app/settings/api — click 'Create key'."
2. Validate the key:
   ```bash
   curl -s -o /dev/null -w "%{http_code}" \
       -H "Authorization: <KEY>" \
       -H "Content-Type: application/json" \
       -d '{"query":"{ viewer { id name } }"}' \
       https://api.linear.app/graphql
   ```
   HTTP 200 means valid. Any other code means the key is wrong — ask them
   to double-check.
3. Ask for their project prefix (e.g., ENG, BET — the prefix on issue IDs).

### 6b: Slack integration

Ask the user if they want to set up Slack. If yes, walk them through
creating a Slack app. They need to do these steps in the Slack admin UI:

1. **Create a Slack App**: Go to https://api.slack.com/apps, click
   "Create New App" > "From scratch". Name it "Modabot", pick workspace.

2. **Add Bot Scopes**: Go to OAuth & Permissions > Bot Token Scopes, add:
   `chat:write`, `channels:history`, `channels:read`, `groups:history`,
   `groups:read`, `im:history`, `im:read`, `users:read`

3. **Enable Socket Mode**: Go to Socket Mode > toggle ON. Generate an
   App-Level Token with scope `connections:write`. Ask user for this token
   (starts with `xapp-`).

4. **Subscribe to Events**: Go to Event Subscriptions > toggle ON.
   Subscribe to bot events: `message.im`, `message.channels`,
   `message.groups`, `app_mention`

5. **Install to Workspace**: Click "Install to Workspace" at the top of
   OAuth & Permissions. Ask user for the Bot User OAuth Token (starts
   with `xoxb-`).

6. **Invite the bot**: Tell user to run `/invite @Modabot` in their
   engineering channel.

Validate the bot token:
```bash
curl -s -H "Authorization: Bearer <BOT_TOKEN>" https://slack.com/api/auth.test
```
Check that `"ok": true` in the response.

## Step 7: Write configuration files

Create the config directory and files based on user answers from Step 6.

```bash
mkdir -p ~/.modastack ~/.config/modastack
```

### ~/.modastack/config.yaml

```yaml
slack:
  bot_token: "<SLACK_BOT_TOKEN or empty string>"
  app_token: "<SLACK_APP_TOKEN or empty string>"

webhooks:
  port: 8080

github:
  default_account: "<result of: gh api user -q .login>"

repos: []
```

### ~/.config/modastack/credentials.yaml

Only write this if the user chose Linear:

```yaml
<project_prefix_lowercase>:
  linear_api_key: "<LINEAR_API_KEY>"
```

### Initialize modastack

```bash
source "$INSTALL_DIR/.venv/bin/activate"
modastack start eng-org --foreground
```

## Step 8: Start modastack (optional)

Ask the user if they want to start modastack now. If yes:

```bash
INSTALL_DIR="${MODASTACK_DIR:-$HOME/dev/modastack}"

# Kill any previous instance
pkill -f "modastack start" 2>/dev/null || true
tmux kill-session -t modastack-consumer 2>/dev/null || true
sleep 1

# Start modastack in a tmux session (manager + events + Slack + dashboard — all in one process)
tmux new-session -d -s modastack-consumer \
    "cd $INSTALL_DIR && source .venv/bin/activate && modastack start"
```

Verify:
```bash
# Check the tmux session is running
tmux list-sessions | grep modastack

# Check modastack status (manager + engineer sub-agents)
source "$INSTALL_DIR/.venv/bin/activate"
modastack status
```

You should see the manager running. When issues are assigned, engineer
sub-agents will appear automatically.

## Step 9: Summary

Print a summary of what was installed:

- modastack location and version
- Config file locations
- Task tracking system chosen
- Slack: configured or not
- Running: yes or no

Tell the user their next step:
```
# Register a repo for modastack to manage
modastack register ~/path/to/your-repo

# Check status
modastack status
```

---

## Troubleshooting

Use this section to debug failures. Read error messages carefully and
apply the relevant fix.

### Homebrew not found (macOS)

If `brew` is not installed:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Then add to PATH:
```bash
eval "$(/opt/homebrew/bin/brew shellenv)"    # Apple Silicon
eval "$(/usr/local/bin/brew shellenv)"       # Intel
```

### Python version too old

`python3 --version` reports <3.11:
- macOS: `brew install python@3.12`
- Ubuntu <24.04: `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv`
- Ubuntu 24.04+: system Python is 3.12, should work

### `python3 -m venv` fails with "ensurepip not available"

Missing the venv module (common on Debian/Ubuntu):
```bash
sudo apt install python3-venv
```

### pip install fails with "externally managed environment"

Python is managed by the system package manager (PEP 668). This should
not happen inside a venv. If it does, the venv was not activated:
```bash
source "$INSTALL_DIR/.venv/bin/activate"
```

### `modastack` command not found after pip install

The venv is not activated. Run:
```bash
source "$INSTALL_DIR/.venv/bin/activate"
```
Or use the full path: `$INSTALL_DIR/.venv/bin/modastack`

### `truststore` import error on startup

modastack imports `truststore` at CLI startup. If pip install succeeded
but this fails, reinstall:
```bash
pip install truststore
```

### Node.js version too old

`node --version` reports <18:
- macOS: `brew install node`
- Linux: use NodeSource:
  ```bash
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt install -y nodejs
  ```

### Bun install fails

The Bun install script at `bun.sh/install` requires `curl` and `unzip`.
Make sure both are installed first. After install, add to PATH:
```bash
export BUN_INSTALL="$HOME/.bun"
export PATH="$BUN_INSTALL/bin:$PATH"
```

### gstack setup fails

gstack setup warnings are usually non-fatal. Common issues:
- Missing `unzip`: `sudo apt install unzip` (Linux)
- Playwright browser deps: `bunx playwright install-deps chromium`
- If `./setup` fails entirely, check that bun is installed and on PATH

The gstack setup installs skills into `~/.claude/skills/`. Verify:
```bash
ls ~/.claude/skills/
```

See [Chromium sandbox for /browse](#chromium-sandbox-for-browse) below if
`/browse` fails to launch a browser.

### /browse fails to launch a browser (Chromium sandbox)

On Ubuntu 23.10+ Chromium can't start because AppArmor restricts unprivileged
user namespaces. Symptoms: `/browse` errors with `No usable sandbox!` or
`Failed to move to new namespace`, and `modastack doctor` reports the
"Chromium launches" check as failed.

Diagnose and fix:
```bash
modastack doctor          # shows which check fails
modastack doctor --fix    # offers to apply the sysctl fix (sudo)
```

Or apply manually:
```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
echo "kernel.apparmor_restrict_unprivileged_userns = 0" | \
    sudo tee /etc/sysctl.d/99-chromium-sandbox.conf
```

See [Chromium sandbox for /browse](#chromium-sandbox-for-browse) for the
security tradeoffs and the narrower per-binary AppArmor alternative.

### Claude Code auth fails

If `claude auth login` doesn't open a browser (headless server), it will
print a URL. Tell the user to open it in their local browser.

If auth succeeds but `claude auth status` still shows unauthenticated,
check `~/.claude/` for credential files.

### GitHub CLI auth fails

If `gh auth login` fails with browser flow on a headless server, use
the token flow instead:
```bash
gh auth login --with-token <<< "<GITHUB_PAT>"
```
The user needs a GitHub Personal Access Token with `repo` and
`admin:repo_hook` scopes.

### modastack crashes immediately

If the tmux session exits right away:
1. Try running the command directly (without tmux) to see the error:
   ```bash
   cd ~/dev/modastack && source .venv/bin/activate && modastack start eng-org --foreground
   ```
2. Check the log in `.modastack/state/manager.log`
3. Common causes: missing Slack tokens, missing event server config,
   Claude Code not authenticated

### Slack token validation fails

- Bot token must start with `xoxb-`
- App-level token must start with `xapp-`
- If `auth.test` returns `"ok": false`, the token is invalid or revoked.
  Have the user regenerate it in the Slack admin.

### Linear API key validation fails

- Key must start with `lin_api_`
- HTTP 401 means the key is invalid
- HTTP 403 means the key lacks permissions
- Test: `curl -s -H "Authorization: <KEY>" -H "Content-Type: application/json" -d '{"query":"{ viewer { name } }"}' https://api.linear.app/graphql`

### Permission denied errors

- On Linux, package installation needs `sudo`
- If the user can't sudo, they need to install deps manually or ask an admin
- npm global installs may need `sudo` on Linux (or configure npm prefix)

### Already installed — re-running the installer

The install process is idempotent. Re-running will:
- Pull latest code instead of re-cloning
- Skip already-installed dependencies
- Overwrite config files (warn the user about this)
- Re-link skills

To do a clean reinstall:
```bash
rm -rf ~/dev/modastack/.venv
cd ~/dev/modastack && python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```
