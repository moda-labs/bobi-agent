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

When setting up dispatch for a user, guide them through credentials:

1. Run `dispatch init --non-interactive` to create the config skeleton
2. Ask the user for their **Linear API key** (get it at https://linear.app/settings/api → Create key)
3. Ask the user for their **Slack bot token** (see docs/SLACK_SETUP.md for the 5-minute walkthrough)
4. Run `dispatch init --linear-key <KEY> --slack-token <TOKEN>` to store them
5. Run `dispatch setup` in the target repo to auto-generate `.dispatch.yaml`
6. Ask the user to confirm the detected Linear project key and Slack channel

Credentials can also be set via env vars: `LINEAR_API_KEY`, `SLACK_BOT_TOKEN`.

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
*/5 * * * * ~/dev/agent-dispatch/.venv/bin/python -m dispatch.cli cycle >> ~/.dispatch/dispatch.log 2>&1
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
