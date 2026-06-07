# modastack

Event-driven AI engineering team. A persistent Claude Code manager monitors Linear, GitHub, Slack, and engineer sessions — assigning work, routing phases, answering questions, and communicating with humans.

## Install

```bash
uv tool install modastack
```

If `uv` isn't installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`

Also available via Homebrew: `brew tap moda-labs/modastack && brew install modastack`

For development, clone and install in editable mode:

```bash
cd ~/dev/modastack
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
modastack start eng-org
```

## Commands

```bash
modastack start <agent>        # start an agent pack (e.g. modastack start eng-org)
modastack stop                 # stop the running instance
modastack restart              # stop and restart
modastack start <agent> --fresh  # wipe manager session and start clean
modastack agents launch -w W --role R --task T  # launch an agent
modastack agents list          # list active agents
modastack agents show <id>     # inspect a specific agent
modastack agents cancel <id>   # cancel a running agent
modastack agents browse        # browse remote agent registry
modastack agents update <name> # update agent packs from remote
modastack agents add-registry <repo>  # add a remote registry
modastack workflows list       # list available workflows
modastack workflows status     # show active workflow runs
modastack workflows validate <file>   # validate a workflow YAML
modastack monitors list        # list background monitors (merged across tiers)
modastack monitors add <name>  # add a monitor (--interval, --description)
modastack monitors pause <name>  # disable a monitor
modastack monitors remove <name> # remove a user-added monitor
modastack roles list           # list available agent roles
modastack status               # show active engineer sessions
modastack events               # show recent events and decisions
modastack message "text"       # inject a message into any session
modastack ask "question"       # ask the manager a question, block until response
modastack transcript show <session>  # show session transcript
modastack transcript search <query>  # search conversation history
modastack doctor               # system health check
modastack event-server start   # start the local event server
modastack event-server stop    # stop the local event server
```

## Architecture

Modastack is a generic event-driven agent framework. Every agent is a
symmetric node — any agent can subscribe to event topics and receive
events. The framework has no topology opinions. All domain-specific
behavior comes from agent packs (role prompts, workflows, monitors).

Events flow through a centralized event server (Cloudflare Worker or
local Node.js) to subscribing agents via WebSocket. The event server
supports generic topic-based pub/sub (`POST /events/{topic}`) plus
webhook ingestion for GitHub, Linear, and Slack.

```
modastack/                        # Framework (Python package)
├── cli.py                        # Click CLI entrypoint
├── config.py                     # Machine-wide config (~/.modastack/config.yaml)
├── session.py                    # Claude Code SDK session wrapper
├── subagent.py                   # Agent executor (blocking + detached)
├── sdk.py                        # Session registry, activity logging
├── registry.py                   # Agent pack registry (fetch, update, browse)
├── inbox.py                      # Per-session message delivery
├── prompts/                      # Agent prompts (no domain logic in framework)
│   ├── __init__.py               # AGENTS_CACHE_DIR, BASE_PATH exports
│   ├── base.md                   # Generic capabilities shared by all agents
│   └── resolver.py               # Prompt resolution: base + agent pack role
├── events/                       # Generic event infrastructure
│   ├── client.py                 # WebSocket client (connects to event server)
│   ├── server.py                 # Local event server launcher (Node.js)
│   ├── drain.py                  # Event queue → session inbox delivery
│   └── subscriptions.py          # Subscription key builder
├── workflow/
│   ├── orchestrator.py           # DAG executor with deterministic routing
│   ├── triggers.py               # Workflow discovery, three-tier resolution
│   ├── schema.py                 # WorkflowDef, StepDef, YAML parsing
│   ├── state.py                  # JSON persistence for workflow runs
│   └── variables.py              # Variable resolution, safe condition evaluation
└── monitors/                     # Background polling to fill webhook gaps
    ├── schema.py                 # Monitor record + interval parsing
    ├── registry.py               # Three-tier load/merge + writes
    ├── checks.py                 # Native check runners (pr_conflicts, stale_prs)
    └── scheduler.py              # Interval scheduler, dedup, event injection

skills/                           # Claude Code skill files for working with modastack
├── create-agent.md               # Guide for designing new agent packs
└── modastack.md                  # Guide for using modastack day-to-day

agents/                           # Agent packs (portable agent definitions)
├── registry.yaml                 # Local pack index
└── eng-org/                      # Example: engineering org agent pack
    ├── defaults.yaml             # Pack metadata (version, entry role, event sources)
    ├── agent.md                  # Shared base prompt for all roles
    ├── roles/                    # Role-specific prompts (folder format)
    │   ├── director/
    │   │   └── ROLE.md           # Main role prompt
    │   ├── project_lead/
    │   │   └── ROLE.md
    │   └── engineer/
    │       └── ROLE.md
    ├── tools/                    # Service interaction guides (loaded into all roles)
    │   ├── github.md             # How to interact with GitHub
    │   ├── linear.md             # How to interact with Linear
    │   └── slack.md              # How to interact with Slack
    ├── workflows/                # Pack-specific workflow definitions
    │   ├── issue-lifecycle.yaml
    │   ├── pr-feedback.yaml
    │   └── ...
    └── monitors/                 # Pack-specific monitors
        ├── defaults.yaml
        └── github_checks.py

.modastack/                       # Per-project runtime state (not config)
├── sessions/                     # Agent session state
└── state/                        # PID files, logs, event server state
```

### Agent Packs

An agent pack is a portable bundle containing everything an agent needs:
role prompts, workflows, monitors, and check functions. Packs are the
distribution unit for agents.

**Resolution order for agent packs:**
1. `<project>/agents/<name>/` — project-level (visible)
2. `<project>/.modastack/agents/<name>/` — project override (hidden)
3. `~/.modastack/agents/<name>/` — user cache (fetched from remote registry)

**Resolution order for role prompts:**
1. `<project>/.modastack/roles/<role>/ROLE.md` — project override
2. Agent pack `roles/<role>/ROLE.md` — from resolved agent pack
3. Built-in `modastack/prompts/agents/<role>/ROLE.md` — framework-shipped

Roles are folders — `roles/<name>/ROLE.md` is the main prompt, and the folder
can contain additional resources (extra prompts, scripts, reference data) that
the role may need.

**Tools (service interaction guides):**

Tools are markdown files that describe how to interact with external services
(e.g. `github.md`, `linear.md`, `gmail.md`). All tools from the pack's `tools/`
directory are loaded into every agent's context. Project-level tools in
`.modastack/tools/` override pack tools with the same filename.

### Machine-wide config (`~/.modastack/config.yaml`)

Service credentials and connection URLs shared across all projects.
Not checked in — contains secrets.

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

Per-workspace API keys (Linear, etc.). GitHub Issues uses `gh` CLI auth.

## Issue lifecycle (SDLC use case)

The manager agent matches incoming events against workflow trigger
descriptions (natural language conditions) to decide what to do:

| Condition | Action |
|---|---|
| Issue assigned that needs code changes | run `issue-lifecycle` workflow |
| Engineer session state changes | read handoff, run next workflow step |
| Pull request merged | run `pr-merged` workflow |
| Reviewer requests changes | run `pr-feedback` workflow |
| CI check fails | run `build-failure` workflow |
| Engineer session stalls | run `stall-recovery` workflow |
| Human replied | inject answer into engineer session |

Internal phases (triage, spec, implement) happen within "In Progress".
Per-step handoff files in the session directory track sub-phase state.

## Handoff contract

Each workflow step writes a handoff file at
`<project>/.modastack/sessions/<session-name>/handoff-<step>.yaml`:

```yaml
complexity: medium
needs_spec: true
notes: "Requires API changes"
```

Each agent reads the handoff, does its work, then goes idle. The
manager detects state changes via the worker poller and routes to
the next skill.

## Custom workflows

Workflows are YAML DAGs loaded from multiple tiers (most specific wins):
1. Agent pack `workflows/` — pack-specific definitions
2. `<project>/.modastack/workflows/` — project-specific overrides
3. `~/.modastack/workflows/` — user-level overrides

See `skills/create-agent.md` for the workflow YAML reference.

## Background monitors

Monitors are scheduled polling tasks that fill webhook gaps — conditions
no webhook fires for (merge conflicts, stale PRs, deploy health). A monitor
is a small human-readable YAML record (`name`, `description`, `interval`,
`event`) loaded from the agent pack's `monitors/` directory and project
overrides.

A monitor with a `check:` field uses a native runner (deterministic,
deduplicated). Without one, the scheduler launches a short-lived,
non-interactive check agent out-of-band
(`modastack agents launch -w adhoc --role engineer --wait --task "..." --post-event <event>`):
it performs the check from the `description`, captures the result, and
posts an event back to the bus *only* if it finds something.

## Releasing

1. Bump `version` in `pyproject.toml` and `VERSION`
2. `git tag v<version> && git push --tags`
3. GitHub Actions publishes to PyPI

The publish workflow (`.github/workflows/publish-pypi.yml`) triggers on `v*` tags.
Users upgrade with `uv tool upgrade modastack`.

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/  # unit tests (~30s)
pytest tests/                              # all tests including integration (~5min)
```

Integration tests drive real Claude Code sessions. Run them before
pushing to main or opening a PR — not on every edit.

**Production bug = integration test gap.** Any time an issue is found
in production, write or update an integration test that covers that
scenario before fixing the code. The test must fail without the fix
and pass with it. No exceptions — if it broke in prod, it means our
tests didn't cover that path.
