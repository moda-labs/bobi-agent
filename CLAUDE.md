# agent-dispatch

Cron-based agent dispatch loop. Scans Linear + Slack for work, spawns coding agents, reports results.

## Setup

```bash
cd ~/dev/agent-dispatch
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
dispatch init --non-interactive
```

## First-time setup (agent guidance)

When setting up dispatch for a user, you MUST ask them for information.
Do NOT guess or skip these steps.

### Step 1: Install

```bash
cd ~/dev/agent-dispatch
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Step 2: Ask for Linear API key

Ask the user: "What's your Linear API key? You can create one at
https://linear.app/settings/api → click 'Create key'."

Then run:
```bash
dispatch init --non-interactive --linear-key <THEIR_KEY>
```

### Step 3: Ask about Slack

Ask the user: "Do you want Slack notifications when agents finish work?"

If yes, ask: "Do you already have a Slack bot token (starts with xoxb-)?"

- If they have one: collect it
- If not: walk them through creating one. The short version:
  1. Go to https://api.slack.com/apps → Create New App → From scratch
  2. OAuth & Permissions → add scopes: channels:history, channels:read,
     chat:write, im:history, im:read, users:read
  3. Install to Workspace → copy the Bot User OAuth Token (xoxb-...)
  4. /invite @YourBot in the target channel

Then ask: "What Slack channel should agent updates go to? (e.g., #eng-agents)"

Store the token:
```bash
dispatch init --non-interactive --linear-key <KEY> --slack-token <TOKEN>
```

### Step 4: Setup the repo

Ask the user: "What's your Linear project key? This is the prefix on
your issue IDs (e.g., if issues look like ENG-42, the key is ENG)."

Then run (include --slack-channel only if they provided one in step 3):
```bash
dispatch setup --linear-project <KEY> --slack-channel '#channel'
```

### Step 5: Verify

Show the user the generated `.dispatch.yaml` and ask if the detected
test command and skills look correct.

### Important

- NEVER guess the Linear project key — always ask
- NEVER skip the Slack question — always ask (user can say "skip" or "later")
- The `dispatch setup` command auto-detects test commands and skills, but
  Linear project and Slack channel MUST come from the user

For multi-workspace setups, edit `~/.dispatch/credentials.yaml` directly:

```yaml
workspace-name:
  linear_api_key: "lin_api_..."
  slack_bot_token: "xoxb-..."
```

Then set `credentials: "workspace-name"` in the repo's `.dispatch.yaml`.

## Commands

```bash
dispatch init              # configure Linear API key + Slack bot token
dispatch register <path>   # register a repo for automated dispatch
dispatch repos             # list registered repos
dispatch cycle             # run one scan/dispatch cycle
dispatch status            # show in-flight work
```

## Cron

```bash
* * * * * ~/dev/agent-dispatch/.venv/bin/python -m dispatch.cli cycle >> ~/.dispatch/dispatch.log 2>&1
```

## Adding a repo to dispatch

1. Drop `.dispatch.yaml` in the repo root (see `example.dispatch.yaml`)
2. Run `dispatch register ~/path/to/repo`
3. Label issues in Linear with your trigger label (default: "agent")

## Project structure

```
dispatch/
├── cli.py          # Click CLI entrypoint
├── config.py       # Global (~/.dispatch/config.yaml) + per-repo (.dispatch.yaml)
├── scanner.py      # Linear GraphQL + Slack API scanning
├── state.py        # JSON state with CAS, status machine
├── dispatcher.py   # Prompt assembly + agent spawning
├── engine.py       # Main loop: scan → classify → dispatch → check → report
└── reporter.py     # Post results to Linear + Slack
```

## State machine

```
TODO → DISPATCHED → WORKING → AUDITING → DONE
                                  ↓
                               FAILED / STUCK
```

State lives at `~/.dispatch/state.json`. Atomic writes prevent corruption from overlapping cron runs.

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```
