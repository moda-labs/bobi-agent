# modastack

Event-driven AI engineering team. A persistent Claude Code manager monitors Linear, GitHub, Slack, and engineer sessions — assigning work, routing phases, answering questions, and communicating with humans. Engineers are Claude Code sessions in tmux that execute skills for each phase of the development lifecycle.

## How it works

modastack has four core principles:

1. **Skills first** — each phase of work (triage, spec, implement, ship, feedback) is a self-contained skill. Skills are portable — they work both unattended via the manager and manually in Claude Code.

2. **Persistent tmux sessions** — each issue gets one interactive Claude Code session in tmux that persists across phases. The manager injects skill invocations into the same session instead of spawning new processes. Context carries forward naturally.

3. **Event-driven architecture** — events from Linear, GitHub, Slack, and engineer sessions flow through an in-process bus. The consumer batches events and feeds them to the persistent manager session, which reasons about what to do next.

4. **Manager-driven orchestration** — the manager is a long-lived interactive Claude Code session that reads event files and acts directly. It uses curl for external APIs (Linear, Slack, GitHub) and tmux commands for engineer sessions. No hard-coded routing rules, no executor — the manager reasons about the full picture and handles everything via tools.

### Event flow

```
Event producers (threads)          Event bus          Consumer          Manager session
─────────────────────────          ─────────          ────────          ───────────────

Worker poller (5s)    ──┐
Linear poller (30s)   ──┤
Slack poller (10s)    ──┼──→  thread-safe  ──→  drain + batch  ──→  write to file
Webhook server (HTTP) ──┤      queue            format events       + inject trigger
Slack Socket Mode     ──┘                                           into manager tmux

                                                                    Manager reads file,
                                                                    acts directly:
                                                                    ├─ curl for APIs
                                                                    │  (Slack, Linear, GitHub)
                                                                    └─ tmux for engineers
                                                                       (spawn, inject, kill)
```

Events arrive from multiple sources — pollers run in background threads, webhooks via an HTTP server, and Slack via Socket Mode WebSocket. All push to the same in-process bus. The consumer drains the bus every few seconds, writes events to `~/.modastack/manager/pending_events.md`, and injects a short trigger message into the manager's tmux session. The manager reads the event file and acts directly — no intermediate executor or JSON action protocol.

### Handoff contract

Engineers write `~/.modastack/handoffs/<issue_id>.md` to track phase state:

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
```

The manager reads handoff files and event data to decide which skill to route next.

### Phase routing

The manager maps handoff phases to skills:

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
└── Self: triage, write handoff

/implement
├── Sub-agent: write tests → commits test files
├── Sub-agent: implement → commits code
├── Sub-agent: review → checks diff only
└── Self: push
```

Each sub-agent gets only the context it needs. The implement sub-agent never sees the test-writing process. The reviewer only sees the diff.

### Question bridging

When an engineer asks a question (via `AskUserQuestion`), the worker poller detects it from the tmux pane and pushes a `worker.asking_question` event. The manager sees the event, reasons about whether it can answer from context, and either answers directly (via the executor's `answer_worker_question`) or posts the question to Linear/Slack and waits for a human reply.

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

```bash
git clone https://github.com/underminedsk/modastack.git ~/dev/modastack
cd ~/dev/modastack
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
modastack init
```

### Per-repo setup

```bash
modastack setup ~/path/to/repo --linear-key <KEY> --linear-project <PROJECT>
```

This stores credentials in `~/.modastack/credentials.yaml`, registers the repo in `~/.modastack/config.yaml`, bootstraps the Linear board with required workflow states, and installs engineer skills as symlinks in `.claude/skills/`.

### Commands

```bash
modastack start                # start event loop (polling mode)
modastack start --webhooks     # start with webhook server + polling
modastack start --webhooks --port 9090
modastack tick                 # check manager session state
modastack tick "message"       # inject a message into the manager session
modastack status               # show active engineer sessions
modastack events               # show recent events from the bus
modastack decisions            # show recent manager decisions
modastack init                 # initialize global config
modastack setup [path]         # set up a repo — install skills, store credentials, register
modastack register <target>    # register a repo (local path or org/repo)
modastack repos                # list registered repos
```

## Per-repo config

All repo config lives in `~/.modastack/config.yaml` under the `repos` list.
Run `modastack register <target>` or `modastack setup <path>` to add a repo:

```yaml
repos:
  - path: /Users/zach/dev/bettertab
    remote: moda-labs/bettertab     # optional — enables auto-clone
    linear_project: BT
    credentials: default            # key into credentials.yaml
```

Settings like `test_command` and `skills` are auto-detected from the repo at runtime.

## Issue lifecycle

Linear states: **Todo → In Progress → In Review → Done** (+ Blocked)

```
Todo
  │  Manager spawns tmux session + injects /pickup, moves to In Progress
  ▼
In Progress
  │  /pickup triages → writes handoff
  │  Manager reads events → injects /spec or /implement into same session
  │  /spec writes spec → waits for approval
  │  Human replies "approved" → manager injects /implement
  │  /implement builds, tests, pushes → writes handoff
  │  Manager injects /prepare-pr → creates PR → moves to In Review
  ▼
In Review
  │  Human reviews PR
  │  ├─ Changes requested → manager injects /feedback
  │  └─ PR merged → manager moves to Done
  ▼
Done
```

**Blocked** — when an engineer asks a question, the manager detects it via the worker poller, posts it to Linear/Slack, and waits. When a human replies, the event flows through the bus and the manager injects the answer back into the engineer's session.

## Project structure

```
modastack/                        # CLI + infrastructure
├── cli.py                        # Click CLI entrypoint
├── config.py                     # Global config (~/.modastack/config.yaml)
├── scanner.py                    # Linear GraphQL polling
├── session.py                    # Engineer tmux session management (spawn, inject, capture)
├── setup.py                      # Repo setup — skill install, auto-detection
└── board_setup.py                # Bootstrap Linear board with workflow states

manager/                          # Persistent manager + event system
├── session.py                    # Manager tmux session (start, resume, inject, capture)
├── prompt.md                     # Manager personality, decision rules, available actions
└── events/
    ├── bus.py                    # Thread-safe in-process event queue
    ├── consumer.py               # Drain bus → write events file → trigger manager
    ├── pollers.py                # Background threads: workers (5s), Linear (30s), Slack (10s)
    ├── webhook_server.py         # HTTP endpoints: /webhooks/github, /linear, /slack
    └── slack_socket.py           # Slack Socket Mode WebSocket client

engineer/                         # Skills
├── process/                      # Daemon-routed lifecycle phases
│   ├── pickup/SKILL.md           # Take ticket, create worktree, triage
│   ├── spec/SKILL.md             # Write implementation spec
│   ├── implement/SKILL.md        # Build from spec, TDD, sub-agents
│   ├── prepare-pr/SKILL.md       # Create/update PR
│   └── feedback/SKILL.md         # Address review comments
└── practices/                    # Org-specific "how we work here"
    ├── triage/SKILL.md           # Task intake & classification
    ├── build/SKILL.md            # Staff engineer coding methodology
    ├── design-critic/SKILL.md    # Adversarial design doc reviewer
    ├── code-review/SKILL.md      # Mandatory quality gates
    ├── ticketing-policy/SKILL.md # Who moves tickets when
    ├── source-control-conventions/SKILL.md
    └── brand-identity/SKILL.md   # Design system enforcement

tools/                            # Shared tool reference (used by both manager and engineers)
├── git/SKILL.md                  # Git CLI commands
├── github/SKILL.md               # gh CLI commands
├── linear/SKILL.md               # Linear GraphQL API
├── slack/SKILL.md                # Slack setup & API
├── webhooks/SKILL.md             # Webhook setup guide
└── notion/SKILL.md               # Notion integration (placeholder)
```

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Skills first | Each phase is a self-contained skill — portable, testable, works manually or via the manager |
| Persistent tmux sessions | One session per issue, reused across phases. Context carries forward, no cold starts |
| Event-driven bus | Decouples event sources from the manager. Adding a new source means writing one poller or webhook handler — push to the bus |
| Persistent manager session | Long-lived interactive Claude Code in tmux. Survives restarts via `--resume`. Reads event files and acts directly using tools |
| File-based event delivery | Consumer writes events to a file instead of injecting long text into tmux (paste buffer is unreliable). Manager reads the file reliably |
| Manager uses curl | MCP tools have built-in write confirmations that block automation. The manager calls APIs directly via curl for Linear, Slack, and GitHub |
| No executor | The manager handles everything directly — curl for APIs, tmux commands for engineer sessions, bash for everything else. No intermediate action protocol |
| Handoff contract | `~/.modastack/handoffs/<id>.md` is the interface between phases — minimal, structured |
| Sub-agents | Context isolation within phases. Reviewer only sees the diff, not the spec |
| Question bridging | Agent questions detected via worker poller, manager decides how to answer — directly or escalate to human |
| Simple Linear states | 4 states (Todo, In Progress, In Review, Done + Blocked). Internal phases are invisible to humans |
| Daemon, not cron | Inherits full shell environment (Keychain, OAuth). No cron env issues |
| Centralized config | All repo settings in `~/.modastack/config.yaml`. No modastack files in target repos |
| Webhooks + polling | Webhooks for real-time events when available, pollers as fallback. Both push to the same bus |
| Slack Socket Mode | WebSocket connection to Slack — no public URL needed, real-time DMs and mentions |
