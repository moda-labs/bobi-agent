# agentd

Skills-first dispatch daemon for coding agents. Scans Linear for work, spawns Claude Code with the right skill for each phase, reports results via Linear.

## How it works

agentd has three core principles:

1. **Skills first** — each phase of work (triage, spec, implement, ship, feedback) is a self-contained skill. Skills are portable — they work both unattended via the daemon and manually in Claude Code.

2. **Persistent tmux sessions** — each issue gets one interactive Claude Code session in tmux that persists across phases. The daemon injects skill invocations into the same session instead of spawning new processes. Context carries forward naturally.

3. **Summarizer-driven handoffs** — agents don't write handoffs. A dedicated summarizer inspects the worktree (git status, commits, PRs, specs) and captures tmux pane output to determine what phase the agent reached, then writes `.dispatch/handoff.md`. The daemon reads the handoff to route to the next skill.

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
| `implementation_complete` | `/ship-pr` | |
| `feedback_addressed` | `/ship-pr` | |
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

### Question bridging

When an agent asks a question (via `AskUserQuestion`), the daemon detects it from the tmux pane, posts the question and options as a Linear comment, and moves the issue to Blocked. When a human replies on Linear, the daemon matches the reply to an option and injects the answer back into the tmux session.

### gstack integration

Every dispatch phase uses [gstack](https://github.com/garrytan/gstack) skills to enforce a real engineering lifecycle. No phase ships without quality gates.

| Dispatch phase | gstack skills used | What they do |
|---|---|---|
| `/pickup` (triage) | `/frontdoor` | Classify: update / inquiry / bug |
| | `/office-hours` | Complex/ambiguous issues → structured design doc |
| `/spec` (design) | `/plan-eng-review` | Architecture, edge cases, test coverage |
| | `/plan-design-review` | UX review, design dimensions scored 0-10 |
| | `/plan-ceo-review` | Scope review: too narrow? too wide? |
| `/implement` (build) | `/investigate` | Bugs only — root cause analysis before any fix |
| | `/build` | Staff engineer coding methodology |
| | `/review` | **Mandatory** pre-landing code review |
| | `/qa` | Browser-based QA (web frontends only) |
| `/ship-pr` (ship) | `/ship` | Full ship workflow: test, review, create PR |
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
  │  Daemon injects /ship-pr → creates PR → moves to In Review
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
