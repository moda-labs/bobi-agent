# agentd

Skills-first dispatch daemon for coding agents. Scans Linear for work, spawns Claude Code with the right skill for each phase, reports results via Linear.

## How it works

agentd has four core principles:

1. **Skills first** — each phase of work (triage, spec, implement, ship, feedback) is a self-contained skill. Skills are portable — they work both unattended via the daemon and manually in Claude Code.

2. **Persistent tmux sessions** — each issue gets one interactive Claude Code session in tmux that persists across phases. The daemon injects skill invocations into the same session instead of spawning new processes. Context carries forward naturally.

3. **Summarizer-driven handoffs** — agents don't write handoffs. A dedicated summarizer inspects the worktree (git status, commits, PRs, specs) and captures tmux pane output to determine what phase the agent reached, then writes `.dispatch/handoff.md`. The daemon reads the handoff to route to the next skill.

4. **Manager-driven orchestration** — a Claude-powered manager reads full context (Linear issues, GitHub PRs, worker sessions, Slack messages) every tick and decides what to do next. Instead of hard-coded routing rules, the manager reasons about the whole picture — assigning work, routing phases, answering questions, and escalating to humans when needed.

### Handoff contract

The summarizer writes `.dispatch/handoff.md` after each phase:

```yaml
---
issue_id: AGD-12
title: Add rate limiting
worktree: /path/to/worktree
branch: agent/agd-12
phase: implementation_complete
complexity: medium
needs_spec: true
spec_path: specs/agd-12-rate-limiting.md
---

## Status
Implementation pushed. 3 files changed.

## Agent activity
(captured from tmux pane)
```

The summarizer determines `phase` by inspecting worktree state — commits beyond main, spec files, open PRs, pushed branches. The daemon reads the `phase` field and routes to the next skill.

### Daemon cycle

```
Every N seconds:

  POLL         →  Linear API (all issues grouped by state)
                        │
  MONITOR      →  For each active tmux session, detect state:
  SESSIONS          working → update activity timestamp
                    exited → summarizer writes handoff, route next skill
                    waiting_input → summarizer writes handoff, route next skill
                    asking_question → post question to Linear, move to Blocked
                        │
  MERGED PRs   →  In Review issues: check if PR merged → Done
                        │
  NEW WORK     →  Todo issues with trigger label → spawn tmux session,
                    inject /pickup
                        │
  REPLIES      →  Blocked/In Progress + human replied on Linear
                    → inject answer into tmux session
                        │
  STALL        →  Kill sessions with no activity for 10 min
```

### Phase routing

The daemon maps handoff phases to skills:

| Handoff phase | Next skill | Condition |
|---|---|---|
| `triage_complete` | `/spec` | `needs_spec: true` |
| `triage_complete` | `/implement` | `needs_spec: false` |
| `spec_complete` | (wait) | human must reply "approved" |
| `implementation_complete` | `/prepare-pr` | |
| `feedback_addressed` | `/prepare-pr` | |
| `in_review` | (wait) | human reviews PR |
| `blocked` | (wait) | human must reply |

### Sub-agents

Within each phase, the skill uses sub-agents to keep context isolated:

```
/pickup
├── Sub-agent: explore codebase → returns relevant files + complexity
└── Self: triage, exit (summarizer writes handoff)

/implement
├── Sub-agent: write tests → commits test files
├── Sub-agent: implement → commits code
├── Sub-agent: review → checks diff only
└── Self: push, exit (summarizer writes handoff)
```

Each sub-agent gets only the context it needs. The implement sub-agent never sees the test-writing process. The reviewer only sees the diff.

### Manager (outer agent loop)

The manager is a reasoning layer above the dispatch daemon. Instead of hard-coded routing rules, it gathers full context each tick, calls Claude to decide what actions to take, and executes them. Think of it as an engineering manager checking their dashboard every 60 seconds.

```
Every 60 seconds (or on change via watcher):

  GATHER       →  Poll all channels:
  CONTEXT           Linear issues (state, comments, assignee)
                    GitHub PRs (status, review comments)
                    Workers (tmux sessions — state, activity, questions)
                    Slack DMs (new messages)
                          │
  HASH CHECK   →  Compare context hash to last tick.
                    No change → sleep. Changed → continue.
                          │
  CALL CLAUDE  →  claude -p with manager prompt + full context.
                    Returns a JSON array of actions.
                          │
  EXECUTE      →  Run each action:
                    spawn_worker    — assign a ticket to a new engineer session
                    spawn_task      — ad-hoc work without a ticket
                    route_skill     — inject the next phase (/implement, /prepare-pr, etc.)
                    inject_into_worker — send guidance to an engineer
                    answer_worker_question — respond to an AskUserQuestion prompt
                    move_linear_issue — transition tickets (assign/close)
                    comment_linear  — post status updates
                    send_slack      — notify humans
                    update_memory   — persist state across ticks
                    kill_worker     — kill stuck sessions
                          │
  LOG          →  Write reasoning + actions + outcomes to decisions.jsonl
```

The manager is stateless between ticks — all persistent state lives in gathered context and `~/.dispatch/manager/memory.md`. The watcher (`manager/watcher.py`) provides cheap 5-second polling with hash-based change detection, only invoking the expensive Claude call when something actually changed.

#### Running the manager

```bash
dispatch start             # start the watcher (5s poll, manager wakes on changes)
dispatch tick              # run one manager tick (debugging)
dispatch decisions         # show recent manager decisions
```

Or directly:

```bash
python -m manager.watcher         # fast poll mode (default)
python -m manager.loop             # fixed 60s interval mode
python -m manager.loop --once      # single tick (debugging)
```

### Question bridging

When an agent asks a question (via `AskUserQuestion`), the manager detects it from the tmux session, reasons about whether it can answer from context, and either answers directly or posts the question to Linear and moves the issue to Blocked. When a human replies on Linear, the manager injects the answer back into the tmux session.

### gstack integration

Every dispatch phase uses [gstack](https://github.com/garrytan/gstack) skills to enforce a real engineering lifecycle. No phase ships without quality gates.

| Dispatch phase | gstack skills used | What they do |
|---|---|---|
| `/pickup` (triage) | `/triage` | Classify: update / inquiry / bug |
| | `/office-hours` | Complex/ambiguous issues → structured design doc |
| `/spec` (design) | `/plan-eng-review` | Architecture, edge cases, test coverage |
| | `/plan-design-review` | UX review, design dimensions scored 0-10 |
| | `/plan-ceo-review` | Scope review: too narrow? too wide? |
| `/implement` (build) | `/investigate` | Bugs only — root cause analysis before any fix |
| | `/build` | Staff engineer coding methodology |
| | `/review` | **Mandatory** pre-landing code review |
| | `/qa` | Browser-based QA (web frontends only) |
| `/prepare-pr` (ship) | `/ship` | Full ship workflow: test, review, create PR |
| `/feedback` (iterate) | `/investigate` | If feedback points to a bug |
| | `/review` | **Mandatory** review of fixes before pushing |

Key enforcement points:
- **`/review` is mandatory** in both `/implement` and `/feedback`. Code cannot advance to PR without passing code review.
- **`/investigate` before fixing bugs.** No guessing at fixes — root cause first (Iron Law).
- **Triple review on specs.** Non-trivial specs get engineering, design, and CEO-level scope review before implementation starts.
- **`/ship` handles PR creation.** Agents don't use raw `gh pr create` — `/ship` runs tests, reviews the diff, and creates a proper PR.

## Setup

### One-liner

From inside any repo:

```bash
bash <(curl -sL https://raw.githubusercontent.com/underminedsk/agentd/main/bootstrap.sh)
```

### Manual

```bash
git clone https://github.com/underminedsk/agentd.git ~/dev/agentd
cd ~/dev/agentd
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
dispatch init
```

### Per-repo setup

```bash
dispatch setup ~/path/to/repo --linear-key <KEY> --linear-project <PROJECT>
```

This generates `.dispatch.yaml` and stores credentials in `~/.dispatch/credentials.yaml`.

### Commands

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

Drop `.dispatch.yaml` in any repo, or run `dispatch setup` to auto-generate:

```yaml
credentials: "default"          # credential set from ~/.dispatch/credentials.yaml

linear:
  project: "PROJ"               # Linear project key (e.g., ENG)
  trigger_labels: ["agent"]     # issues with these labels get picked up
  skip_labels: ["blocked", "human-only"]

agent:
  tool: "claude"
  max_parallel: 2               # max concurrent agents on this repo

verify:
  test_command: "pytest"
  review_required: true
  auto_merge: false
```

## Issue lifecycle

Linear states: **Todo → In Progress → In Review → Done** (+ Blocked)

```
Todo
  │  Daemon spawns tmux session + injects /pickup, moves to In Progress
  ▼
In Progress
  │  /pickup triages → goes idle → summarizer writes handoff
  │  Daemon reads handoff → injects /spec or /implement into same session
  │  /spec writes spec → waits for approval
  │  Human replies "approved" → daemon injects /implement
  │  /implement builds, tests, pushes → summarizer writes handoff
  │  Daemon injects /prepare-pr → creates PR → moves to In Review
  ▼
In Review
  │  Human reviews PR
  │  ├─ Changes requested → daemon injects /feedback
  │  └─ PR merged → daemon moves to Done
  ▼
Done
```

**Blocked** — when an agent asks a question, the daemon posts it to Linear and moves to Blocked. When a human replies, the daemon injects the answer into the tmux session and moves back to In Progress.

## Project structure

```
manager/
├── loop.py          # Gather context → call claude -p → execute actions
├── context.py       # Aggregate context from all channels, hash for change detection
├── executor.py      # Parse and execute manager action JSON (spawn, route, move, etc.)
├── watcher.py       # Fast 5s poll loop, wakes manager only on context changes
├── prompt.md        # Manager personality, decision rules, available actions
└── channels/        # Pluggable context sources
    ├── linear.py    # Linear issues (state, comments, assignee)
    ├── github.py    # GitHub PRs (status, review comments)
    ├── workers.py   # Tmux sessions (state, activity, questions)
    └── slack.py     # Slack DMs

engineer/
├── process/                          # daemon-routed lifecycle
│   ├── pickup/SKILL.md               # take ticket, create worktree, triage
│   ├── spec/SKILL.md                 # write implementation spec
│   ├── implement/SKILL.md            # build from spec, TDD, sub-agents
│   ├── prepare-pr/SKILL.md           # create/update PR
│   └── feedback/SKILL.md             # address review comments
├── practices/                        # org-specific "how we work here"
│   ├── triage/SKILL.md               # task intake & classification
│   ├── build/SKILL.md                # staff engineer coding methodology
│   ├── design-critic/SKILL.md        # adversarial design doc reviewer
│   ├── code-review/SKILL.md          # mandatory quality gates
│   ├── ticketing-policy/SKILL.md     # who moves tickets when
│   ├── source-control-conventions/SKILL.md  # branching, commit, PR format
│   └── brand-identity/SKILL.md       # design system enforcement
└── tools/                            # mechanical API reference
    ├── linear/SKILL.md               # Linear GraphQL API
    ├── git/SKILL.md                  # git CLI commands
    ├── github/SKILL.md               # gh CLI commands
    ├── slack/SKILL.md                # Slack setup & API
    └── notion/SKILL.md               # Notion integration (placeholder)

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

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Skills first | Each phase is a self-contained skill — portable, testable, works manually or via daemon |
| Persistent tmux sessions | One session per issue, reused across phases. Context carries forward, no cold starts |
| Summarizer writes handoffs | Agents don't write handoffs — the summarizer inspects worktree state + tmux output to determine phase. Decouples agents from the dispatch protocol |
| Handoff contract | `.dispatch/handoff.md` is the interface between phases — minimal, structured |
| Sub-agents | Context isolation within phases. Reviewer only sees the diff, not the spec |
| Question bridging | Agent questions are posted to Linear, human replies injected back into tmux. Humans interact on Linear, never in tmux |
| Simple Linear states | 4 states (Todo, In Progress, In Review, Done + Blocked). Internal phases are invisible to humans |
| Daemon, not cron | Inherits full shell environment (Keychain, OAuth). No cron env issues |
| Per-repo config | Daemon is global, config is local. Repo opts in via `.dispatch.yaml` |
| Manager over rules | Claude reasons about full context each tick instead of hard-coded routing. Handles edge cases (conflicting files, stuck workers, ambiguous comments) that rules can't |
| Cheap poll, expensive think | Watcher polls every 5s (API calls + tmux checks). Only calls Claude when context hash changes. Avoids burning tokens on idle ticks |
| Channel architecture | Each input source (Linear, GitHub, workers, Slack) is a pluggable channel module. Adding a new source means adding one file — no changes to the manager loop |
