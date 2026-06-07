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

Workflow definitions are resolved from multiple tiers (most specific wins):

1. **Agent pack** (`agents/<pack>/workflows/`) — pack-specific workflow definitions
2. **Project-specific** (`<project>/.modastack/workflows/`) — custom overrides for a single project
3. **User overrides** (`~/.modastack/workflows/`) — personal workflow tweaks across all projects

When an event arrives, the dispatcher loads workflows from all sources and picks the most specific match. Within the same tier, the first match wins. This lets projects ship custom lifecycles that override the defaults without forking modastack.

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
- **Linear (optional)** — configure via credentials. Uses GraphQL API for polling and mutations.

The system defaults to GitHub Issues without prompting. All events emit generic `task.*` events regardless of which backend is configured.

### Handoff contract

Engineers write handoff files to `<project>/.modastack/sessions/<session-name>/handoff-<step>.yaml`:

```yaml
complexity: medium
needs_spec: true
notes: "Requires API changes"
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
uv tool install modastack
```

If `uv` isn't installed yet: `curl -LsSf https://astral.sh/uv/install.sh | sh`

Also available via Homebrew or pip:

```bash
brew tap moda-labs/modastack && brew install modastack
# or
pip install modastack
```

### Development setup

```bash
git clone https://github.com/moda-labs/modastack.git ~/dev/modastack
cd ~/dev/modastack
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Agent-assisted install

Paste this into Claude Code (or any AI coding agent):

```
Follow the instructions at https://raw.githubusercontent.com/moda-labs/modastack/main/deploy/INSTALL.md to install modastack on this machine.
```

## Running agents

### Starting an agent pack

`modastack start` requires an agent pack argument — the pack defines roles, workflows, and monitors:

```bash
# Start the engineering org agent pack
modastack start eng-org

# Start with a fresh session
modastack start eng-org --fresh

# Start in foreground mode
modastack start eng-org --foreground

# Add extra event subscriptions
modastack start eng-org --subscribe linear:MOD
```

### Launching agents directly

`modastack agents launch` launches individual agents with a workflow and role:

```bash
# Ad-hoc task
modastack agents launch -w adhoc --role engineer --task "Fix the login bug"

# Run a named workflow on an issue
modastack agents launch -w issue-lifecycle --role engineer --task "Work on #42"

# Blocking check (run, capture result, optionally post an event)
modastack agents launch -w adhoc --role engineer --wait --task "Check prod URL returns 200"

# Persistent agent with event subscriptions
modastack agents launch -w adhoc --role project_lead --subscribe github:org/repo --persistent
```

Key flags: `--task`, `-w`/`--workflow`, `--role`, `--timeout`, `--wait`, `--post-event`, `--requested-by`, `--persistent`, `--subscribe`. Run `modastack agents launch --help` for the full list.

### Commands

```bash
modastack start <agent>            # start an agent pack (e.g. modastack start eng-org)
modastack start <agent> --fresh    # wipe manager session and start clean
modastack stop                     # stop a running instance
modastack restart                  # stop and restart

modastack agents launch ...        # launch an agent with a workflow and role
modastack agents list              # list active agents
modastack agents show <id>         # inspect a specific agent
modastack agents cancel <id>       # cancel a running agent
modastack agents create            # design a new agent pack interactively
modastack agents browse            # browse remote agent registry
modastack agents update <name>     # update agent packs from remote
modastack agents add-registry <repo>  # add a remote registry
modastack status                   # show active agents — manager + engineer sessions
modastack message "text"           # send a message to any session
modastack ask "question"           # ask the manager a question, block until it responds

modastack events                   # show recent events and manager decisions
modastack transcript show <sess>   # show the transcript for a session
modastack transcript search <q>    # search conversation history

modastack workflows list           # list available workflow definitions
modastack workflows status         # show active and recent workflow runs
modastack workflows validate <f>   # validate a workflow YAML file

modastack roles list               # list available agent roles

modastack monitors list            # list background monitors (merged across tiers)
modastack monitors add <name>      # add a monitor (--interval, --description)
modastack monitors pause <name>    # disable a monitor
modastack monitors remove <name>   # remove a user-added monitor

modastack event-server start       # start the local event server
modastack event-server stop        # stop the local event server
modastack event-server status      # show event server status

modastack doctor                   # system health check
```

## Configuration

### Machine-wide config (`~/.modastack/config.yaml`)

Service credentials and connection URLs shared across all projects. Not checked in — contains secrets.

```yaml
slack:
  bot_token: xoxb-...
event_server:
  url: https://modastack-events.example.workers.dev
linear:
  api_key: lin_api_...
registries:
  - moda-labs/modastack-agents
```

### Credentials (`~/.config/modastack/credentials.yaml`)

Per-workspace API keys (Linear, etc.). GitHub Issues uses `gh` CLI auth — no key needed.

## Agent Packs

An agent pack is a portable bundle containing everything an agent needs to operate in a domain: role prompts, workflows, monitors, and check functions.

```
agents/<pack>/
├── defaults.yaml              # Pack metadata (version, entry role, event sources)
├── agent.md                   # Shared base prompt for all roles
├── roles/                     # Role-specific prompts
│   ├── director.md
│   ├── project_lead.md
│   └── engineer.md
├── workflows/                 # Pack-specific workflow definitions
│   ├── issue-lifecycle.yaml
│   ├── pr-feedback.yaml
│   └── ...
└── monitors/                  # Pack-specific monitors
    ├── defaults.yaml
    └── github_checks.py
```

**Resolution order for agent packs:**
1. `<project>/agents/<name>/` — project-level (visible)
2. `<project>/.modastack/agents/<name>/` — project override (hidden)
3. `~/.modastack/agents/<name>/` — user cache (fetched from remote registry)

Packs are the distribution unit for agents. Install from a remote registry:

```bash
modastack agents browse                    # see available packs
modastack agents update eng-org            # install or update a pack
modastack agents add-registry myorg/agents # add a custom registry
```

## Background monitors

Monitors are scheduled polling tasks that fill webhook gaps — conditions no webhook fires for (merge conflicts, stale PRs, deploy health). A monitor is a small YAML record (`name`, `description`, `interval`, `event`) loaded from the agent pack's `monitors/` directory and project overrides.

The scheduler runs each monitor on its interval, deduplicates detected conditions, and injects a synthetic event onto the same queue webhooks use — so the manager routes it like any other event. A monitor with a `check:` field uses a native runner in the agent pack's `monitors/` directory; without one, the scheduler launches a short-lived `modastack agents launch --wait` check that posts an event back only if it finds something. PR conflict detection ships as a default in the `eng-org` agent pack.

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
modastack/                        # CLI + infrastructure (Python package)
├── cli.py                        # Click CLI entrypoint
├── config.py                     # Machine-wide config (~/.modastack/config.yaml)
├── subagent.py                   # Agent executor — run_phase_blocking(), SDK client, hooks
├── sdk.py                        # Session registry, activity logging
├── session.py                    # Session helpers (sync, cleanup, skill loading)
├── setup.py                      # Repo setup — skill install, auto-detection
├── registry.py                   # Agent pack registry (fetch, update, browse)
├── inbox.py                      # Per-session message delivery
├── scanner.py                    # Linear GraphQL polling
├── history.py                    # Conversation history indexer (SQLite + FTS5)
├── board_setup.py                # Bootstrap Linear board / workflow states
├── browser.py                    # Headless browser helpers (/browse, doctor)
├── relay.py                      # Chat relay — mirror manager I/O to Slack/Discord
├── doctor.py                     # System health checks
├── prompts/                      # Agent prompts
│   ├── base.md                   # Generic capabilities shared by all agents
│   ├── agents/                   # Built-in agent prompts
│   │   └── builder.md            # Agent pack builder prompt
│   └── resolver.py               # Prompt resolution: base + agent pack role
├── events/                       # Generic event infrastructure
│   ├── client.py                 # WebSocket client (connects to event server)
│   ├── server.py                 # Local event server launcher (Node.js)
│   ├── drain.py                  # Event queue → session inbox delivery
│   └── subscriptions.py          # Subscription key builder
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

agents/                           # Agent packs (portable agent definitions)
├── registry.yaml                 # Local pack index
└── eng-org/                      # Engineering org agent pack
    ├── defaults.yaml             # Pack metadata (version, entry role, event sources)
    ├── agent.md                  # Shared base prompt for all roles
    ├── roles/                    # director.md, project_lead.md, engineer.md
    ├── workflows/                # issue-lifecycle, pr-feedback, build-failure, etc.
    └── monitors/                 # PR conflict detection, stale PR checks

event-server/                     # Event server (Cloudflare Worker / local Node.js)
```

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Skills first | Each phase is a self-contained skill — portable, testable, works manually or via the manager |
| Claude Agent SDK | Manager and engineers all use `ClaudeSDKClient`. Sessions resume via saved IDs |
| Prompt-driven orchestrator | The orchestrator is pure code with no LLM — it sequences steps, injects prompts, validates handoffs, and routes. The agent does all the reasoning via its tools |
| Blocking steps | Each step runs to completion before the next starts. No polling, no async futures — sub-agents block via `run_phase_blocking()` |
| Agent packs | Portable bundles of roles, workflows, and monitors. The distribution unit for agents — install a pack and get a working agent for a domain |
| Sub-agents | Context isolation within phases. The reviewer only sees the diff, not the spec |
| AskUserQuestion deferral | A PreToolUse hook defers agent questions; `run_phase_blocking()` routes them through a callback and resumes the agent with the answer |
| Event-driven consumer | Decouples event sources from the manager. The centralized event server aggregates webhooks from all repos |
| GitHub Issues default | No API key needed — uses `gh` CLI auth. Linear available as an option for teams already using it |
| Machine-wide config | Service credentials in `~/.modastack/config.yaml`. Domain behavior comes from agent packs, not config files |

## Releasing

1. Bump `version` in `pyproject.toml` and `VERSION`
2. `git tag v<version> && git push --tags`
3. GitHub Actions publishes to PyPI

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/
```

## Extra setup

**`/qa`** — requires the gstack `browse` binary (a compiled Playwright wrapper for headless browser testing). Without it, the skill can't take snapshots or interact with web pages. Install gstack separately or skip `/qa` and have engineers do browser testing manually.

Practice skills (`/review`, `/investigate`, `/ship`, `/autoplan`, `/plan-eng-review`, `/plan-design-review`, `/plan-ceo-review`, `/office-hours`) were adapted from [gstack](https://github.com/garrytan/gstack) (MIT license).
