# agentd

Dispatch loop for coding agents. Scans Linear for work, spawns Claude Code to implement, reports results via Linear comments.

Requires [gstack](https://github.com/garrytan/gstack) — agents use `/review`, `/ship`, `/office-hours`, and `/plan-eng-review` to enforce a real engineering lifecycle instead of vibe coding.

## Architecture

```
Every N seconds (daemon loop):

  SCAN              →  Linear API (all issues for the team, grouped by state)
                              │
  RECONCILE         →  Kill agents whose issues moved to Done/Canceled
                              │
  STALL DETECTION   →  Kill agents with no activity for 10 min
                              │
  STATE TRANSITIONS →  Exited agents: check worktree for spec/PR/question
                       Move Linear issue to the appropriate state
                       Post 🤖 comments (spec ready, PR link, questions)
                              │
  RE-SPAWN          →  Design Review / In Review / Blocked:
                       check for human replies → re-spawn agent
                              │
  MERGE DETECTION   →  In Review issues: check if PR merged → move to Done
                              │
  DISPATCH          →  Todo issues with trigger label:
                       move to Planning, spawn `claude -p`
                       track PID + worktree in state.json
```

Linear is the source of truth for issue state. The engine owns all state transitions and posts status comments (🤖 prefix) — agents just do work and exit. The engine only tracks running processes (PID, worktree, activity timestamp) in `state.json`.

## Setup

### One-liner (paste into any coding agent)

From inside any repo you want to wire up:

> Set up agentd for this repo: run `bash <(curl -sL https://raw.githubusercontent.com/underminedsk/agentd/main/bootstrap.sh)` — this clones, installs, and runs `dispatch setup` in the current directory.

Or if already cloned locally:

> Set up agentd: run `~/dev/agentd/bootstrap.sh`

The bootstrap script also installs gstack if not already present.

### Manual

```bash
git clone https://github.com/underminedsk/agentd.git ~/dev/agentd
cd ~/dev/agentd
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
dispatch init
```

### Quick reference

```bash
dispatch init              # initialize config + start daemon in tmux
dispatch setup [path]      # auto-generate .dispatch.yaml and register a repo
dispatch register <path>   # register a repo (if .dispatch.yaml already exists)
dispatch repos             # list registered repos
dispatch daemon            # run as a long-running daemon (default: 5s poll)
dispatch cycle             # run one dispatch cycle (manual/debugging)
dispatch status            # show in-flight work
dispatch watch             # live dashboard (refreshes every 5s)
```

## Per-repo config

Drop `.dispatch.yaml` in any repo, or run `dispatch setup` to auto-generate one:

```yaml
credentials: "default"          # credential set from ~/.dispatch/credentials.yaml

linear:
  project: "PROJ"               # Linear project key (e.g., ENG)
  team: "My Team"               # optional: for display
  trigger_labels: ["agent"]     # issues with these labels get picked up
  skip_labels: ["blocked", "human-only"]

complexity:
  trivial: "label:typo OR label:docs OR label:config"
  medium: "default"
  heavy: "label:feature OR label:refactor OR estimate>3"

agent:
  tool: "claude"                # agent to spawn
  skills: ["review", "ship"]   # gstack skills to inject into prompts
  max_parallel: 2               # max concurrent agents on this repo

verify:
  test_command: "pytest"        # how to run tests
  review_required: true         # agent can't close its own issue
  auto_merge: false             # human approves the PR
```

Credentials are stored per-project in `~/.dispatch/credentials.yaml` (not in the repo).

## Running the daemon

The recommended way to run agentd is as a long-running daemon in tmux:

```bash
dispatch init                # creates config + starts daemon in tmux
```

Or manually:

```bash
dispatch daemon              # foreground, polls every 5s
dispatch daemon --interval 10  # custom poll interval
tmux new -d -s dispatch 'dispatch daemon'  # background in tmux
```

You can also use cron for one-shot cycles:

```bash
* * * * * ~/dev/agentd/.venv/bin/dispatch cycle >> ~/.dispatch/dispatch.log 2>&1
```

## Issue lifecycle

Linear states drive the lifecycle. The engine moves issues between states automatically:

```
Todo              →  Move to Planning, dispatch agent (writes spec)
Planning          →  Agent working on spec
Design Review     →  Spec ready — wait for human to reply "approved"
                     → re-spawn to implement (moves to Implementing)
Implementing      →  Agent working on implementation
In Review         →  PR created — wait for human review
                     → re-spawn to address feedback (moves to Implementing)
                     → auto-detect PR merge → move to Done
Blocked           →  Agent had a question — wait for human reply
                     → re-spawn to continue (moves to Implementing)
Done / Canceled / Duplicate →  Kill agent, stop tracking
```

When an agent exits, the engine inspects its worktree (spec files, PR status, question file) and moves the Linear issue to the correct state. The engine posts all 🤖 comments — agents don't interact with Linear directly.

Each daemon cycle polls for human replies on Design Review, In Review, and Blocked issues. When you respond, the agent is re-spawned with your reply in context.

Stall detection: agents with no activity for 10 minutes are killed. Failed agents retry up to 3 times automatically.

## Project structure

```
dispatch/
├── cli.py           # Click CLI entrypoint
├── config.py        # Global (~/.dispatch/) + per-repo (.dispatch.yaml)
├── scanner.py       # Linear GraphQL scanning + complexity classification
├── state.py         # Running agent tracking (PID, worktree, activity)
├── dispatcher.py    # Prompt assembly + agent spawning
├── engine.py        # Main loop: scan → reconcile → transitions → dispatch
├── linear_state.py  # Linear API: state transitions, comments, worktree checks
├── conversation.py  # Detect human replies on Linear issues
├── skills.py        # gstack skill discovery for prompt injection
├── setup.py         # Auto-generate .dispatch.yaml from repo inspection
└── board_setup.py   # Bootstrap Linear board with required workflow states
```

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Daemon with short poll interval | Inherits full shell environment (Keychain, OAuth); no cron env issues |
| Linear as source of truth | No internal state machine — issue state in Linear drives all behavior |
| State file with atomic writes | Prevents double-dispatch across overlapping cycles |
| Per-repo manifest | Engine is global, config is local. Repo opts in via `.dispatch.yaml` |
| Tiered prompts | Trivial work gets minimal context, heavy gets full methodology |
| Worker can't self-close | `review_required: true` means independent verification before closing |
| Complexity from labels + estimates | Uses data already in Linear, no separate classification step |
| Engine owns state transitions | One place for all Linear moves and comments — agents just do work and exit |
