# agentd

Dispatch loop for coding agents. Scans Linear for work, spawns Claude Code to implement, reports results via Linear comments.

Skills-first: agents use [gstack](https://github.com/garrytan/gstack) skills (`/review`, `/ship`, `/office-hours`, `/plan-eng-review`) to enforce a real engineering lifecycle instead of vibe coding. The dispatch engine auto-discovers installed skills and injects them into every agent prompt.

## Architecture

Agentd has two core principles:

1. **Skills first** — agents are prompted with a structured methodology (spec → implement → review → ship) and use gstack skills for each step. The engine discovers skills from `~/.claude/skills`, `~/.codex/skills`, and `~/.cursor/skills`, then injects them into every spawn.

2. **Atomic handoffs** — each agent does exactly one phase (spec, implement, or address feedback) then exits. It writes handoff documents to the worktree before exiting. The engine reads those documents to determine the next state transition. No agent ever runs two phases.

### Handoff documents

Agents communicate with the engine through files in the worktree:

| Document | Purpose | Triggers |
|----------|---------|----------|
| `.dispatch/state.md` | Carries phase, context, and next-step instructions between spawns | Read on spawn, written before exit |
| `.dispatch/history.md` | Append-only audit trail of actions taken | Appended after each action |
| `.dispatch-progress.md` | Live progress checklist (stall detection reads mtime) | Updated during work |
| `.dispatch-question.md` | Agent has a question for a human | Engine moves issue → Blocked |
| `specs/*.md` | Implementation spec from the planning phase | Engine moves issue → Design Review |
| PR (via `gh pr create`) | Implementation complete | Engine moves issue → In Review |

The engine never parses agent stdout. All state transitions are driven by the presence or absence of these files.

### Engine cycle

```
Every N seconds (daemon loop):

  SCAN              →  Linear API (all issues for the team, grouped by state)
                              │
  RECONCILE         →  Kill agents whose issues moved to Done/Canceled
                              │
  STALL DETECTION   →  Kill agents with no activity for 10 min
                       Detect code changes to auto-transition Planning → Implementing
                              │
  STATE TRANSITIONS →  Exited agents: read handoff documents from worktree
                       Move Linear issue to the appropriate state
                       Post 🤖 comments (spec ready, PR link, questions)
                              │
  RE-SPAWN          →  Design Review / In Review / Blocked:
                       check for human replies → re-spawn agent with reply context
                              │
  MERGE DETECTION   →  In Review issues: check if PR merged → move to Done
                              │
  DISPATCH          →  Todo issues with trigger label:
                       move to Planning, spawn `claude -p` in a git worktree
                       track PID + worktree in state.json
```

Linear is the source of truth for issue state. The engine owns all state transitions and posts status comments (🤖 prefix). Agents just do work, write handoff documents, and exit. The engine only tracks running processes (PID, worktree, activity timestamp) in `state.json`.

### Agent prompt assembly

Each agent receives a single prompt built from layered templates:

```
prompts/preamble.md     — unattended agent rules, decision handling, progress tracking
prompts/lifecycle.md    — phase definitions, state file format, one-phase-per-spawn rule
prompts/spec.md         — spec methodology (classify → scope → verify → plan)
prompts/implement.md    — implementation methodology (tests first, /review before ship)
prompts/tools/github.md — gh CLI recipes (draft PRs, implementation PRs, push updates)
```

Plus issue-specific context (title, description, branch, test command) and auto-discovered skills.

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

Each issue moves through phases. At every phase boundary, the agent exits and writes handoff documents. The engine reads those documents and moves the Linear issue to the next state.

```
Todo
  │  Engine moves to Planning, spawns agent
  ▼
Planning (Phase 1: Spec)
  │  Agent writes spec to specs/, creates draft PR
  │  Writes .dispatch/state.md with phase=spec, status=complete
  │  Exits → engine reads handoff docs → moves to Design Review
  ▼
Design Review
  │  Human reviews spec, replies "approved" on Linear
  │  Engine detects reply → re-spawns agent with approval context
  │  Moves to Implementing
  ▼
Implementing (Phase 2: Implement)
  │  Agent reads spec from specs/, implements, runs /review
  │  Creates PR via gh pr create
  │  Writes .dispatch/state.md with phase=implement, status=complete
  │  Exits → engine reads handoff docs → moves to In Review
  ▼
In Review
  │  Human reviews PR
  │  ├─ Changes requested → engine re-spawns agent (Phase 3: Feedback)
  │  ├─ PR merged → engine auto-detects → moves to Done
  │  └─ Agent pushes fixes → back to In Review
  ▼
Done
```

**Blocked** — at any point, if an agent writes `.dispatch-question.md` and exits, the engine moves the issue to Blocked and posts the question as a 🤖 comment. When a human replies, the agent is re-spawned with the answer.

**Stall detection** — agents with no file activity for 10 minutes are killed. Failed agents retry up to 3 times before moving to Blocked.

**Live state detection** — while an agent runs, the engine checks `git diff` in its worktree. If code changes appear during Planning, the engine auto-transitions to Implementing.

## Project structure

```
dispatch/
├── cli.py           # Click CLI entrypoint
├── config.py        # Global (~/.dispatch/) + per-repo (.dispatch.yaml)
├── scanner.py       # Linear GraphQL scanning + complexity classification
├── state.py         # Running agent tracking (PID, worktree, activity)
├── dispatcher.py    # Prompt assembly + agent spawning (loads prompts/, discovers skills)
├── engine.py        # Main loop: scan → reconcile → read handoff docs → transitions → dispatch
├── linear_state.py  # Linear API: state transitions, comments, handoff doc checks
├── conversation.py  # Detect human replies on Linear issues
├── skills.py        # Skill pack discovery across ~/.claude, ~/.codex, ~/.cursor
├── setup.py         # Auto-generate .dispatch.yaml from repo inspection
└── board_setup.py   # Bootstrap Linear board with required workflow states

prompts/
├── preamble.md      # Unattended agent rules
├── lifecycle.md     # Phase definitions + handoff document format
├── spec.md          # Spec methodology (classify, scope, verify, plan)
├── implement.md     # Implementation methodology (tests first, /review)
└── tools/
    └── github.md    # gh CLI recipes for PRs
```

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Skills first | Agents use gstack skills (`/review`, `/ship`) instead of raw commands — enforces engineering process |
| One phase per spawn | Agent does spec OR implement OR feedback, then exits. Simpler, more predictable, easier to debug |
| Handoff documents | `.dispatch/state.md` carries context between spawns — no coupling between agent process and engine |
| Engine owns all transitions | One place for all Linear moves and comments — agents never touch Linear |
| Daemon with short poll interval | Inherits full shell environment (Keychain, OAuth); no cron env issues |
| Linear as source of truth | No internal state machine — issue state in Linear drives all behavior |
| State file with atomic writes | Prevents double-dispatch across overlapping cycles |
| Per-repo manifest | Engine is global, config is local. Repo opts in via `.dispatch.yaml` |
| Complexity from labels + estimates | Uses data already in Linear, no separate classification step |
