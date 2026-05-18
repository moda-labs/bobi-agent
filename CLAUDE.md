# agentd

Dispatch loop for coding agents. Scans Linear for work, spawns Claude Code to implement, reports results via Linear comments.

## Setup

```bash
cd ~/dev/agentd
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
cd ~/dev/agentd
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
dispatch init --non-interactive
```

### Step 2: Setup the repo

Ask the user TWO things:

1. "What's your Linear API key? You can create one at
   https://linear.app/settings/api → click 'Create key'."

2. "What's your Linear project key? This is the prefix on your issue
   IDs (e.g., if issues look like ENG-42, the key is ENG)."

Then run:
```bash
dispatch setup --linear-key <API_KEY> --linear-project <PROJECT_KEY>
```

This stores the API key per-project (in ~/.dispatch/credentials.yaml,
not in the repo) and generates `.dispatch.yaml`.

### Step 3: Verify

Show the user the generated `.dispatch.yaml` and ask if the detected
test command and skills look correct.

### Important

- NEVER guess the Linear project key — always ask
- NEVER guess the Linear API key — always ask
- Credentials are per-project, stored in ~/.dispatch/credentials.yaml
- `.dispatch.yaml` is safe to commit (no secrets, just references a credential name)

## Commands

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

## Running the daemon

```bash
dispatch init              # creates config + starts daemon in tmux
dispatch daemon            # or run in foreground
```

Or via cron:

```bash
* * * * * ~/dev/agentd/.venv/bin/dispatch cycle >> ~/.dispatch/dispatch.log 2>&1
```

## Adding a repo to dispatch

1. Run `dispatch setup ~/path/to/repo` (auto-generates `.dispatch.yaml`)
2. Or drop `.dispatch.yaml` manually and run `dispatch register ~/path/to/repo`
3. Label issues in Linear with your trigger label (default: "agent")

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

## Issue lifecycle

Linear is the source of truth. The engine owns all state transitions — agents just do work and exit. Running processes are tracked in `~/.dispatch/state.json` (atomic writes prevent corruption).

- **Todo** → move to Planning, dispatch new agent
- **Planning** → agent working on spec
- **Design Review** → spec ready, wait for human approval → re-spawn to implement
- **Implementing** → agent working on implementation
- **In Review** → PR created, wait for human review → re-spawn for feedback; auto-detect merge → Done
- **Blocked** → agent had a question, wait for human reply → re-spawn to continue
- **Done / Canceled** → kill agent, stop tracking

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```
