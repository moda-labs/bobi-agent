# modastack

Event-driven AI engineering team. A persistent Claude Code manager monitors GitHub, Slack, and engineer sessions — assigning work, routing phases, answering questions, and communicating with humans. Engineers are Claude Code SDK sessions that execute skills for each phase of the development lifecycle.

## How it works

modastack has five core principles:

1. **Skills first** — each phase of work (triage, spec, implement, ship, feedback) is a self-contained skill. Skills are portable — they work both unattended via the manager and manually in Claude Code.

2. **Claude Agent SDK sessions** — the manager and all engineer sessions run via the Claude Agent SDK (`ClaudeSDKClient`). The manager is a long-lived interactive session. Engineers run as blocking SDK sessions via `run_phase_blocking()` — the orchestrator drives one engineer through each step of a workflow. Sessions survive restarts via saved session IDs.

3. **Event-driven architecture** — events from GitHub webhooks (via a centralized Cloudflare Worker event server) and Slack flow through the event consumer to the manager. The manager receives every event and decides what to do — launch an ad-hoc engineer, run a structured workflow, or handle the event directly with its own tools. Events that match a workflow trigger are dispatched automatically.

4. **Prompt-driven orchestrator** — workflows are deterministic, *pure-code* state machines (no LLM in the orchestrator itself). A workflow is a sequence of steps loaded from YAML. There are three step types: **prompt** steps inject a prompt into the engineer session and wait for it to write a handoff; **route** steps branch deterministically on a handoff value; **await** steps suspend the workflow until an external event arrives. Each prompt step calls `run_phase_blocking()` and blocks until the agent finishes, then validates the handoff against the step's required fields (re-prompting on missing fields). Handoff outputs flow into a variable context so later steps can reference earlier results.

5. **Input routing** — when an engineer session calls `AskUserQuestion`, a `PreToolUse` hook defers the call. `run_phase_blocking()` routes the deferred question through an `on_input_needed` callback and resumes the agent with the answer — so a question can be answered by the manager (or a human via Slack) without the agent getting stuck.

6. **Composable skills** — skills come from two layers that compose at runtime. Modastack ships process skills (pickup, spec, implement, prepare-pr, feedback), practice skills (triage, build, code-review), tool references (git, github, linear, slack), and product manager skills (brand-identity, design-critic). Methodology skills (review, ship, autoplan, investigate, office-hours, qa, plan-*-review) come from [GStack](https://github.com/garrytan/gstack) installed at user-level (`~/.claude/skills/`). Claude Code's built-in skill resolution merges both layers — repo-level symlinks and user-level skills are all available in engineer sessions.

### Workflow resolution

Workflow definitions are resolved via a three-tier priority chain (most specific wins):

1. **Repo-specific** (`<repo>/.modastack/workflows/`) — custom lifecycles for a single repo
2. **User overrides** (`~/.modastack/workflows/`) — personal workflow tweaks across all repos
3. **Built-in defaults** (`workflows/`) — modastack's standard workflows (issue-lifecycle, pr-feedback, etc.)

When an event arrives, the dispatcher loads workflows from all three sources and picks the most specific match. A repo-specific workflow only matches events from that repo. Within the same tier, the first match wins. This lets repos ship custom lifecycles that override the defaults without forking modastack.

### Event normalization

GitHub Issues and Linear emit events in different formats. The event system normalizes them to a common `task.*` schema so workflows trigger on `task.assigned`, `task.created`, etc. regardless of the source:

| Source event | Normalized event |
|---|---|
| GitHub `issues.opened` | `task.opened` |
| GitHub `issues.assigned` | `task.assigned` |
| GitHub `issues.closed` | `task.closed` |
| Linear `Issue.create` | `task.created` |
| Linear `Issue.update` + assignee | `task.assigned` |
| Linear `Issue.update` + state "Done" | `task.closed` |
| Linear `Issue.remove` | `task.closed` |

For Linear events, the project prefix (e.g., `AGD` from `AGD-12`) is resolved to a repo path via the global config, so workflows can match events to the correct repo.

### Event flow

```
Event sources                      Consumer            Orchestrator
─────────────                      ────────            ────────────

GitHub webhooks  ──┐
  (via event       │
   server WS)      │
                   ├──→  drain queue  ──→  match event  ──→  run workflow
Slack            ──┤      into batch        to YAML            step by step
                   │                        trigger
Manager session  ──┘                        │
                                            ├─ prompt: run_phase_blocking()
                                            │    └─ engineer SDK session
                                            ├─ route: branch on handoff value
                                            └─ await: suspend for event
```

Events arrive from the centralized event server (GitHub webhooks via WebSocket) and Slack. The consumer drains the queue and injects batched events into the manager. When an event matches a workflow trigger, the dispatcher runs the prompt-driven orchestrator, which walks the workflow's steps in order — each prompt step blocks until the engineer finishes and writes its handoff. Events that don't match a workflow are handled directly by the manager.

### Task tracking

modastack supports pluggable task tracking systems:

- **GitHub Issues (default)** — uses `gh` CLI for authentication. Issues are labeled with `status:todo`, `status:in-progress`, `status:blocked`, `status:in-review`. No API key needed.
- **Linear (optional)** — pass `--linear-key` and `--linear-project` during setup. Uses GraphQL API for polling and mutations.

The system defaults to GitHub Issues without prompting. All events emit generic `task.*` events regardless of which backend is configured.

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

After each prompt step, the orchestrator reads the handoff file, validates it against the step's required fields, and extracts outputs into the variable context for downstream steps.

### Phase routing

The orchestrator routes via **route** steps that branch on a handoff value:

| Triage result | Route condition | Next phase |
|---|---|---|
| `needs_spec: true` | branch taken | `/spec` then `/implement` |
| `needs_spec: false` | else branch | `/implement` directly |

**Await** steps (e.g., waiting for spec approval or a PR review) suspend the workflow until an external event arrives, then the orchestrator resumes from where it left off.

### Sub-agents

Each workflow step drives an engineer via `run_phase_blocking()`. Within a phase, the skill uses Claude Code's built-in Agent tool for context isolation:

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

When an engineer session calls `AskUserQuestion`, a `PreToolUse` hook intercepts the call and defers it. The deferred question is carried back through `run_phase_blocking()` and routed through the `on_input_needed` callback. The agent is then resumed with the answer via `client.query(answer)`. This lets the manager (or a human via Slack) answer agent questions without the agent getting stuck.

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

```bash
brew tap moda-labs/modastack
brew install modastack
modastack init
```

Works on macOS and Linux via [Homebrew](https://brew.sh). Also available on [PyPI](https://pypi.org/project/modastack/):

```bash
pip install modastack
# or
uv tool install modastack
```

### Development setup

```bash
git clone https://github.com/moda-labs/modastack.git ~/dev/modastack
cd ~/dev/modastack
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
modastack init
```

### Agent-assisted install

Paste this into Claude Code (or any AI coding agent):

```
Follow the instructions at https://raw.githubusercontent.com/moda-labs/modastack/main/deploy/INSTALL.md to install modastack on this machine.
```

## Running engineers

`modastack agent` is the single entrypoint for launching an engineer — either an ad-hoc task or a structured workflow.

```bash
# Ad-hoc task
modastack agent --repo myrepo --task "Fix the login bug"

# Run a named workflow on an issue
modastack agent --workflow issue-lifecycle --repo myrepo --issue 42

# Blocking check (run, capture result, optionally post an event)
modastack agent --wait --task "Check prod URL returns 200"
```

Key flags: `--repo`, `--task`, `-w`/`--workflow`, `--issue`, `--title`, `--event-json`, `--timeout`, `--wait`, `--post-event`, `--requested-by`. Run `modastack agent --help` for the full list.

### Commands

```bash
modastack start                    # start everything (manager + events + Slack + dashboard)
modastack stop                     # stop a running instance
modastack restart                  # stop and restart

modastack agent ...                # launch an engineer — ad-hoc task or workflow (see above)
modastack status                   # show active agents — manager + engineer sessions
modastack engineers                # list active engineers, or inspect/cancel a specific one
modastack message "text"           # send a message to the manager (or an engineer)
modastack consult "question"       # ask the manager a question, block until it responds

modastack events                   # show recent events from the event bus
modastack decisions                # show recent manager decisions
modastack log <session>            # show the full transcript for a session
modastack history                  # index and search Claude Code conversation history

modastack workflow list            # list available workflow definitions from all sources
modastack workflow status          # show active and recent workflow runs
modastack workflow validate <f>    # validate a workflow YAML file

modastack monitor list             # list background monitors (merged across tiers)
modastack monitor add <name>       # add a monitor (--interval, --description, --repo)
modastack monitor pause <name>     # disable a monitor
modastack monitor remove <name>    # remove a user-added monitor

modastack init                     # initialize global config
modastack dashboard                # start the web dashboard
modastack doctor                   # system health check
modastack slack-reply              # post a message to Slack
```

### Web dashboard

`modastack start` includes a web dashboard (also launchable on its own with `modastack dashboard`):

- **Active sessions** — SDK session state for the manager and all engineer sessions
- **Event log** — filterable by source/type, paginated
- **Conversation view** — live agent output with markdown rendering

## Configuration

### Global config (`~/.modastack/config.yaml`)

All repo settings live here:

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

Lives in the repo root:

```yaml
task_tracking:
  system: github-issues    # or "linear"
  project: MY              # label/project prefix
```

### Credentials (`~/.modastack/credentials.yaml`)

Per-project API keys (Linear, etc.). GitHub Issues uses `gh` CLI auth — no key needed.

## Background monitors

Monitors are scheduled polling tasks that fill webhook gaps — conditions no webhook fires for (merge conflicts, stale PRs, deploy health). A monitor is a small YAML record (`name`, `description`, `interval`, `event`) loaded from three tiers, later tiers overriding earlier by `name`:

1. `monitors/defaults.yaml` — built-in, shipped, read-only at runtime
2. `~/.modastack/monitors.yaml` — user globals (apply to all repos)
3. `<repo>/.modastack.yaml` under `monitors:` — repo-specific

The scheduler runs each monitor on its interval, deduplicates detected conditions, and injects a synthetic event onto the same queue webhooks use — so the manager routes it like any other event. A monitor with a `check:` field uses a native runner in `modastack/monitors/checks.py`; without one, the scheduler launches a short-lived `modastack agent --wait` check that posts an event back only if it finds something. PR conflict detection ships as a default.

## Issue lifecycle

GitHub Issues labels: **status:todo → status:in-progress → status:in-review → Done** (+ status:blocked)

```
Todo
  │  Assign to @modastack → webhook → event server → task.assigned event
  │  Dispatcher matches issue-lifecycle.yaml
  ▼
In Progress
  │  Orchestrator runs the workflow steps: spawn → triage → route → implement
  │  Each prompt step blocks on the engineer session
  │  Triage determines complexity; a route step picks spec or implement
  │  Implement: TDD, code review, push
  ▼
In Review
  │  prepare-pr step: /ship creates the PR with the full review gauntlet
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
├── subagent.py                   # Engineer executor — run_phase_blocking(), SDK client, hooks
├── sdk.py                        # Session registry, activity logging
├── session.py                    # Session helpers (sync, cleanup, skill loading)
├── setup.py                      # Repo setup — skill install, auto-detection
├── github_issues.py              # GitHub Issues scanning + label bootstrap
├── scanner.py                    # Linear GraphQL polling
├── history.py                    # Conversation history indexer (SQLite + FTS5)
├── board_setup.py                # Bootstrap Linear board / workflow states
├── browser.py                    # Headless browser helpers (/browse, doctor)
├── relay.py                      # Chat relay — mirror manager I/O to Slack/Discord
├── manager/
│   ├── session.py                # Manager SDK session (start, resume, inject, query)
│   └── events/
│       ├── consumer.py           # Event loop — drain queue, batch, inject into manager
│       ├── event_client.py       # WebSocket client to centralized event server
│       └── slack_responder.py    # Format + post manager replies to Slack
├── workflow/
│   ├── orchestrator.py           # Prompt-driven orchestrator — pure-code step machine
│   ├── schema.py                 # Workflow/step schema, YAML parsing
│   ├── state.py                  # JSON persistence for workflow runs
│   ├── triggers.py               # Event→workflow matching + dispatch
│   └── variables.py              # Variable resolution, safe condition evaluation
└── monitors/                     # Background polling to fill webhook gaps
    ├── schema.py                 # Monitor record + interval parsing
    ├── registry.py               # Three-tier load/merge + writes
    ├── checks.py                 # Native check runners (pr_conflicts, etc.)
    └── scheduler.py              # Interval scheduler, dedup, synthetic events

workflows/                        # Workflow definitions (YAML)
├── issue-lifecycle.yaml          # Full issue: spawn → triage → route → impl → PR
├── pr-feedback.yaml              # PR change-request handling
├── pr-merged.yaml                # Post-merge cleanup
├── build-failure.yaml            # CI failure handling
├── stall-recovery.yaml           # Recover stalled engineer sessions
└── examples/                     # Non-dev examples (content review, research)

dashboard/                        # Web dashboard (FastAPI app)
monitors/defaults.yaml            # Built-in monitor defaults (shipped, read-only)

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
| Claude Agent SDK | Manager and engineers all use `ClaudeSDKClient`. Sessions resume via saved IDs |
| Prompt-driven orchestrator | The orchestrator is pure code with no LLM — it sequences steps, injects prompts, validates handoffs, and routes. The agent does all the reasoning via its tools |
| Blocking steps | Each step runs to completion before the next starts. No polling, no async futures — sub-agents block via `run_phase_blocking()` |
| Handoff contract | `~/.modastack/handoffs/<id>.md` is the interface between steps — minimal, structured, validated against required fields |
| Sub-agents | Context isolation within phases. The reviewer only sees the diff, not the spec |
| AskUserQuestion deferral | A PreToolUse hook defers agent questions; `run_phase_blocking()` routes them through a callback and resumes the agent with the answer |
| Event-driven consumer | Decouples event sources from the manager. The centralized event server aggregates webhooks from all repos |
| GitHub Issues default | No API key needed — uses `gh` CLI auth. Linear available as an option for teams already using it |
| Centralized config | All repo settings in `~/.modastack/config.yaml`. No modastack files in target repos except `.modastack.yaml` |

## Releasing

1. Bump `version` in `pyproject.toml`
2. `git tag v<version> && git push --tags`
3. GitHub Actions publishes to PyPI and auto-updates the Homebrew formula

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/
```

## Extra setup

**`/qa`** — requires the gstack `browse` binary (a compiled Playwright wrapper for headless browser testing). Without it, the skill can't take snapshots or interact with web pages. Install gstack separately or skip `/qa` and have engineers do browser testing manually.

Practice skills (`/review`, `/investigate`, `/ship`, `/autoplan`, `/plan-eng-review`, `/plan-design-review`, `/plan-ceo-review`, `/office-hours`) were adapted from [gstack](https://github.com/garrytan/gstack) (MIT license).
