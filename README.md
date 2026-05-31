# modastack

Event-driven AI engineering team. A persistent Claude Code manager monitors GitHub, Slack, and engineer sessions — assigning work, routing phases, answering questions, and communicating with humans. Engineers are Claude Code SDK sessions that execute skills for each phase of the development lifecycle.

## How it works

modastack has five core principles:

1. **Skills first** — each phase of work (triage, spec, implement, ship, feedback) is a self-contained skill. Skills are portable — they work both unattended via the manager and manually in Claude Code.

2. **Claude Agent SDK sessions** — the manager and all engineer sessions run via the Claude Agent SDK (`ClaudeSDKClient`). The manager is a long-lived interactive session. Engineers are blocking sub-agent sessions spawned per-phase by the workflow executor. Sessions survive restarts via saved session IDs.

3. **Event-driven architecture** — events from GitHub webhooks (via a centralized Cloudflare Worker event server), Slack Socket Mode, and engineer sessions flow through the system. The workflow dispatcher matches events to YAML workflow definitions and executes them automatically.

4. **Workflow executor** — deterministic YAML DAGs with hybrid LLM reasoning. Each workflow is a directed acyclic graph of typed nodes (bash, action, prompt, manager, approval, gate) executed synchronously in topological order. Prompt nodes launch sub-agent sessions via `run_phase_blocking()` and block until the agent finishes. State persists to disk after every node transition, so workflows survive restarts and resume from the last completed node.

5. **Input routing** — when an engineer sub-agent calls `AskUserQuestion`, a `PreToolUse` hook defers the call. The executor routes the question through an `on_input_needed` callback (currently auto-selects the first option; future: routes to the manager for human escalation via Slack).

6. **Composable skills** — skills come from two layers that compose at runtime. Modastack ships process skills (pickup, spec, implement, prepare-pr, feedback), practice skills (triage, build, code-review), tool references (git, github, linear, slack), and product manager skills (brand-identity, design-critic). Methodology skills (review, ship, autoplan, investigate, office-hours, qa, plan-*-review) come from [GStack](https://github.com/garrytan/gstack) installed at user-level (`~/.claude/skills/`). Claude Code's built-in skill resolution merges both layers — repo-level symlinks and user-level skills are all available in engineer sessions.

### Workflow resolution

Workflow definitions are resolved via a three-tier priority chain (most specific wins):

1. **Repo-specific** (`<repo>/.modastack/workflows/`) — custom lifecycles for a single repo
2. **User overrides** (`~/.modastack/workflows/`) — personal workflow tweaks across all repos
3. **Built-in defaults** (`workflows/`) — modastack's standard workflows (issue-lifecycle, pr-feedback, etc.)

When an event arrives, the dispatcher loads workflows from all three sources and picks the most specific match. A repo-specific workflow only matches events from that repo. Within the same tier, the first match wins. This lets repos ship custom lifecycles that override the defaults without forking modastack.

### Event normalization

Both GitHub Issues and Linear emit events in different formats. The event system normalizes them to a common `task.*` schema so workflows trigger on `task.assigned`, `task.created`, etc. regardless of the source:

| Source event | Normalized event |
|---|---|
| GitHub `issues.opened` | `task.opened` |
| GitHub `issues.assigned` | `task.assigned` |
| GitHub `issues.closed` | `task.closed` |
| Linear `Issue.create` | `task.created` |
| Linear `Issue.update` + assignee | `task.assigned` |
| Linear `Issue.update` + state "Done" | `task.closed` |
| Linear `Issue.remove` | `task.closed` |

For Linear events, the webhook handler resolves the project prefix (e.g., `AGD` from `AGD-12`) to a repo path via the global config, so workflows can match events to the correct repo.

### Event flow

```
Event sources                      Dispatcher          Workflow Executor
─────────────                      ──────────          ─────────────────

GitHub webhooks  ──┐
  (via event       │
   server WS)      │
                   ├──→  match event  ──→  spawn thread  ──→  execute DAG
Slack Socket     ──┤      to YAML          per workflow       nodes in order
  Mode (DMs)       │      trigger                             (blocking)
                   │
Manager session  ──┘                       ├─ bash: run command
                                           ├─ action: slack.post, ticket.move
                                           ├─ prompt: run_phase_blocking()
                                           │    └─ ClaudeSDKClient session
                                           ├─ manager: consult manager LLM
                                           ├─ gate: conditional routing
                                           └─ approval: suspend for event
```

Events arrive from the centralized event server (GitHub webhooks via WebSocket) and Slack (Socket Mode). The workflow dispatcher matches each event against YAML trigger definitions. Matched events spawn a daemon thread running the workflow executor, which walks the DAG synchronously — each node blocks until completion. Unmatched events are injected into the manager session for freeform reasoning.

### Task tracking

modastack supports pluggable task tracking systems:

- **GitHub Issues (default)** — uses `gh` CLI for authentication. Issues are labeled with `status:todo`, `status:in-progress`, `status:blocked`, `status:in-review`. No API key needed.
- **Linear (optional)** — pass `--linear-key` and `--linear-project` during setup. Uses GraphQL API for polling and mutations.

The system defaults to GitHub Issues without prompting. All webhook events emit generic `task.*` events regardless of which backend is configured.

### Handoff contract

Engineers write `~/.modastack/handoffs/<issue_id>.md` to track phase state:

```yaml
---
issue_id: AGD-12
title: Add rate limiting
worktree: /path/to/worktree
branch: agent/agd-12
phase: implement_complete
complexity: medium
needs_spec: true
spec_url: https://github.com/org/repo/issues/12
---

## Status
Implementation pushed. 3 files changed.
```

The workflow executor reads handoff files after each prompt node completes to extract outputs for downstream nodes.

### Phase routing

The workflow executor routes based on gate nodes in the YAML DAG:

| Triage result | Gate branch | Next phase |
|---|---|---|
| `needs_spec: true` | `needs_spec` | `/spec` then `/implement` |
| `needs_spec: false` | `skip_spec` | `/implement` directly |

Approval nodes (e.g., spec approval) suspend the workflow until an external event (GitHub PR review) arrives. The executor persists state and resumes when the event is fed.

### Sub-agents

Each workflow phase spawns a blocking sub-agent via `run_phase_blocking()`. Within each phase, the skill uses Claude Code's built-in Agent tool for context isolation:

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

When an engineer sub-agent calls `AskUserQuestion`, a `PreToolUse` hook intercepts the call and defers it. The `ResultMessage` carries the deferred question back to the executor, which routes it through the `on_input_needed` callback. The agent is then resumed with `client.query(answer)`. This allows the manager (or a human via Slack) to answer agent questions without the agent getting stuck.

### Skill integration

Every dispatch phase uses skills to enforce a real engineering lifecycle. No phase ships without quality gates.

| Dispatch phase | Skills used | What they do |
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

## Install

Paste this into Claude Code (or any AI coding agent):

```
Follow the instructions at https://raw.githubusercontent.com/moda-labs/modastack/main/deploy/INSTALL.md to install modastack on this machine.
```

The agent will install all dependencies, walk you through auth and configuration, and debug any issues. Works on macOS and Linux.

### Manual setup

If you prefer to install without an agent:

```bash
curl -sL https://raw.githubusercontent.com/moda-labs/modastack/main/deploy/install.sh | bash
```

Or step by step:

```bash
git clone https://github.com/moda-labs/modastack.git ~/dev/modastack
cd ~/dev/modastack
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
modastack init
```

### Per-repo setup

```bash
# GitHub Issues (default — no API key needed)
modastack register ~/path/to/repo

# Or with Linear
modastack register ~/path/to/repo --linear-key <KEY> --linear-project <PROJECT>

# Remote repos (auto-clones via gh)
modastack register org/repo
```

`register` handles the full setup: generates `.modastack.yaml`, bootstraps labels (GitHub) or workflow states (Linear), adds `.modastack/` and `worktrees/` to `.gitignore`, installs engineer skills as symlinks, and registers the repo in `~/.modastack/config.yaml`.

### Commands

```bash
modastack start                    # start everything (manager + events + Slack + dashboard)
modastack status                   # show active agents — manager + engineer sub-agents
modastack message "text"           # send a message to the manager
modastack events                   # show recent events
modastack decisions                # show recent manager decisions
modastack init                     # initialize global config
modastack register <target>        # register a repo + full setup (local path or org/repo)
modastack setup [path]             # set up a repo — install skills, store credentials, register
modastack repos                    # list registered repos
modastack engineers                # list active engineer sub-agents
modastack workflow list            # list available workflow definitions
modastack workflow status          # show active and recent workflow runs
modastack self-update              # pull from origin/main + reinstall
modastack rollback                 # restore to pre-update state
```

### Web dashboard

`modastack start` includes a web dashboard on port 8095 with:

- **Active sessions** — SDK session state for manager and all engineer sub-agents
- **Event log** — filterable by source/type, paginated
- **Conversation view** — live agent output with markdown rendering

### Self-updating

modastack checks for updates hourly by comparing against `origin/main`. When a new version is available, the manager posts to Slack for approval. `modastack self-update` pulls, stashes any dirty state, and reinstalls. `modastack rollback` restores the pre-update state if something breaks.

## Configuration

### Global config (`~/.modastack/config.yaml`)

All repo settings live here. Run `modastack register` to add a repo:

```yaml
slack:
  bot_token: xoxb-...
  app_token: xapp-...
event_server:
  url: https://modastack-events.example.workers.dev
  deployment_id: <uuid>
  api_key: <key>
repos:
  - /home/user/dev/myproject
```

### Per-repo config (`.modastack.yaml`)

Generated by `modastack register`, lives in the repo root:

```yaml
task_tracking:
  system: github-issues    # or "linear"
  project: MY              # label/project prefix
```

### Credentials (`~/.modastack/credentials.yaml`)

Per-project API keys (Linear, etc.). GitHub Issues uses `gh` CLI auth — no key needed.

## Issue lifecycle

GitHub Issues labels: **status:todo → status:in-progress → status:in-review → Done** (+ status:blocked)

```
Todo
  │  Assign to @modastack → webhook → event server → task.assigned event
  │  Workflow dispatcher matches issue-lifecycle.yaml
  ▼
In Progress
  │  Executor runs DAG: spawn → notify → triage → route → implement
  │  Each prompt node blocks on a sub-agent session
  │  Triage determines complexity, routes to spec or implement
  │  Implement: TDD, code review, security review, push
  ▼
In Review
  │  Executor runs prepare-pr: /ship creates PR with full review gauntlet
  │  Human reviews PR
  │  ├─ Changes requested → pr-feedback workflow triggers
  │  └─ PR merged → pr-merged workflow triggers → Done
  ▼
Done
```

## Project structure

```
modastack/                        # CLI + infrastructure
├── cli.py                        # Click CLI entrypoint
├── config.py                     # Global config (~/.modastack/config.yaml)
├── subagent.py                   # Sub-agent executor — run_phase_blocking(), SDK client
├── sdk.py                        # Session registry, activity logging
├── session.py                    # Session helpers (sync, cleanup, skill loading)
├── setup.py                      # Repo setup — skill install, auto-detection
├── github_issues.py              # GitHub Issues scanning + label bootstrap
├── scanner.py                    # Linear GraphQL polling
├── history.py                    # Conversation history indexer (SQLite + FTS5)
├── manager/
│   ├── session.py                # Manager SDK session (start, resume, inject, query)
│   └── events/
│       ├── consumer.py           # Event loop orchestrator
│       ├── event_client.py       # WebSocket client to centralized event server
│       └── slack_socket.py       # Slack Socket Mode WebSocket client
└── workflow/
    ├── executor.py               # Blocking DAG executor — walks nodes synchronously
    ├── engine.py                 # Legacy polling executor (deprecated)
    ├── schema.py                 # Workflow/node schema, YAML parsing, topological sort
    ├── state.py                  # JSON persistence for workflow runs
    ├── triggers.py               # Event→workflow matching, dispatch, resume
    ├── actions.py                # Action registry (slack.post, ticket.move, etc.)
    └── variables.py              # Variable resolution, safe condition evaluation

workflows/                        # Workflow definitions (YAML DAGs)
├── issue-lifecycle.yaml          # Full issue: spawn → triage → route → impl → PR
├── pr-feedback.yaml              # PR change-request handling
├── pr-merged.yaml                # Post-merge cleanup
└── build-failure.yaml            # CI failure handling

roles/                            # All skill/prompt content (no Python)
├── manager/
│   ├── prompt.md                 # Core manager behavior
│   └── engineering.md            # Engineering manager role
├── engineer/
│   ├── process/                  # Manager-routed lifecycle phases
│   │   ├── pickup/SKILL.md
│   │   ├── spec/SKILL.md
│   │   ├── implement/SKILL.md
│   │   ├── prepare-pr/SKILL.md
│   │   └── feedback/SKILL.md
│   └── practices/                # Methodology skills
│       ├── triage/SKILL.md
│       ├── build/SKILL.md
│       ├── code-review/SKILL.md
│       └── ...
└── tools/                        # Shared tool reference
    ├── git/SKILL.md
    ├── github/SKILL.md
    └── ...
```

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Skills first | Each phase is a self-contained skill — portable, testable, works manually or via the manager |
| Claude Agent SDK | Manager and engineers all use `ClaudeSDKClient`. No tmux, no process management. Sessions resume via saved IDs |
| Blocking executor | Each workflow node runs to completion before the next starts. No polling, no async futures. Sub-agents block via `asyncio.run()` with a fresh event loop |
| Crash-resumable | State persisted to `~/.modastack/workflow/runs/` after every node transition. On restart, the dispatcher resumes in-flight workflows from the last completed node |
| AskUserQuestion deferral | PreToolUse hook defers agent questions. Executor routes them through a callback. Agent resumes with the answer via `client.query()` |
| Event-driven bus | Decouples event sources from the manager. Centralized event server aggregates webhooks from all repos |
| Handoff contract | `~/.modastack/handoffs/<id>.md` is the interface between phases — minimal, structured |
| Sub-agents | Context isolation within phases. Reviewer only sees the diff, not the spec |
| GitHub Issues default | No API key needed — uses `gh` CLI auth. Linear available as an option for teams already using it |
| Centralized config | All repo settings in `~/.modastack/config.yaml`. No modastack files in target repos except `.modastack.yaml` |
| Slack Socket Mode | WebSocket connection to Slack — no public URL needed, real-time DMs and mentions |

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/
```

## Extra setup

**`/qa`** — requires the gstack `browse` binary (a compiled Playwright wrapper for headless browser testing). Without it, the skill can't take snapshots or interact with web pages. Install gstack separately or skip `/qa` and have engineers do browser testing manually.

Practice skills (`/review`, `/investigate`, `/ship`, `/autoplan`, `/plan-eng-review`, `/plan-design-review`, `/plan-ceo-review`, `/office-hours`) were adapted from [gstack](https://github.com/garrytan/gstack) (MIT license).
