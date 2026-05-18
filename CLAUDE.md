# agentd

Skills-first dispatch daemon. Scans Linear for work, spawns Claude Code with the right skill for each phase, reports results via Linear.

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

## Architecture

Skills-first: each phase of work is a self-contained skill. The daemon
polls Linear, manages persistent tmux sessions, and injects skills into
them. A dedicated summarizer inspects worktree state to write handoffs.

```
skills/
├── pickup/SKILL.md      # take ticket, create worktree, triage complexity
├── spec/SKILL.md        # write implementation spec (non-trivial work)
├── implement/SKILL.md   # build from spec, TDD, sub-agents for tests/code/review
├── ship-pr/SKILL.md     # create/update PR
├── feedback/SKILL.md    # address review comments
└── e2e-test/SKILL.md    # integration test tiers and instructions

dispatch/
├── daemon.py        # Poll → monitor tmux sessions → route phases → bridge questions
├── scanner.py       # Linear GraphQL polling + complexity classification
├── linear_api.py    # Minimal Linear helpers (state IDs, move, comment)
├── conversation.py  # Detect human replies on Linear issues
├── session.py       # Tmux session management (spawn, inject, capture, detect state)
├── summarizer.py    # Inspect worktree + tmux pane → determine phase → write handoff
├── state.py         # Running agent tracking
├── config.py        # Global (~/.dispatch/) + per-repo (.dispatch.yaml)
├── setup.py         # Auto-generate .dispatch.yaml from repo inspection
├── board_setup.py   # Bootstrap Linear board with required workflow states
└── cli.py           # Click CLI entrypoint
```

## Issue lifecycle

Linear states: Todo → In Progress → In Review → Done (+ Blocked)

The daemon routes based on Linear state:

| Linear state | Trigger | Action |
|---|---|---|
| Todo + agent label | new issue | spawn tmux session + inject `/pickup`, move to In Progress |
| In Progress | session idle/exited | summarizer writes handoff, daemon injects next skill |
| In Review | PR merged | move to Done |
| In Review | changes requested | inject `/feedback` into session |
| Blocked | human replied | inject answer into tmux session |

Internal phases (triage, spec, implement) happen within "In Progress".
The handoff file (`.dispatch/handoff.md`) tracks which sub-phase the
agent is in. Linear doesn't need to know.

## Handoff contract

The summarizer writes `.dispatch/handoff.md` in the worktree by
inspecting git state (commits, PRs, specs) and tmux pane output:

```yaml
---
issue_id: AGD-12
title: Add rate limiting
worktree: /path/to/worktree
branch: agent/agd-12
phase: spec_complete
spec_path: specs/agd-12-rate-limiting.md
complexity: medium
---

## Status
Spec written: specs/agd-12-rate-limiting.md

## Agent activity
(captured from tmux pane)
```

Each agent reads the handoff, does its work, then goes idle. The
summarizer detects the idle state, inspects what changed, and writes
the updated handoff for the daemon to route.

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/
```

Do NOT run `tests/integration/` — they create real Linear issues.
