# modastack

Event-driven AI agent framework. Spawn persistent agents that subscribe
to real-world events, react autonomously, and stay interactive. Domain
behavior comes from agent packs — the framework has no topology opinions.

## Install

```bash
uv tool install modastack
```

For development:

```bash
cd ~/dev/modastack
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Commands

```bash
modastack install <path>          # install an agent pack from a local path or registry
modastack start                   # start the installed agent
modastack stop                    # stop the running instance
modastack restart                 # stop and restart
modastack start --fresh           # wipe session and start clean

modastack agents launch -w W --role R --task T  # launch an agent
modastack agents list             # list active agents
modastack agents show <id>        # inspect a specific agent
modastack agents cancel <id>      # cancel a running agent
modastack agents browse           # browse remote agent registry
modastack agents update <name>    # update agent packs from remote
modastack agents add-registry <repo>  # add a remote registry

modastack ask "question"          # ask an agent, block until response
modastack message "text"          # inject a message into any session
modastack status                  # show active agents
modastack events                  # show recent events and decisions
modastack transcript show <sess>  # session transcript
modastack transcript search <q>   # search conversation history
modastack doctor                  # system health check

modastack workflows list          # list available workflows
modastack workflows status        # show active workflow runs
modastack workflows validate <f>  # validate a workflow YAML
modastack monitors list           # list background monitors
modastack monitors add <name>     # add a monitor
modastack monitors pause <name>   # disable a monitor
modastack monitors remove <name>  # remove a user-added monitor
modastack roles list              # list available agent roles

modastack kb create <name>        # create a named knowledge base
modastack kb add <name> --file F  # index a file into a KB
modastack kb add <name> --text T  # add inline text to a KB
modastack kb search <name> "q"    # hybrid FTS + semantic search
modastack kb list                 # list all knowledge bases
modastack kb info <name>          # show KB statistics
modastack kb remove <name>        # delete a knowledge base

modastack skill                   # print the modastack usage guide
modastack skill <name>            # print a specific skill guide

modastack event-server start      # start the local event server
modastack event-server stop       # stop the local event server
```

## Architecture

Every agent is a symmetric node — it subscribes to event topics and
receives events via a centralized event server (Cloudflare Worker or
local Node.js). The event server supports topic-based pub/sub plus
webhook ingestion for GitHub, Linear, Slack, and any custom source.

```
modastack/                        # Framework (Python package)
├── cli.py                        # Click CLI entrypoint
├── config.py                     # Per-project config (.modastack/agent.yaml)
├── session.py                    # Claude Code SDK session wrapper
├── subagent.py                   # Agent executor (blocking + detached)
├── sdk.py                        # Session registry, activity logging
├── registry.py                   # Agent pack registry (fetch, update, browse)
├── inbox.py                      # Per-session message delivery
├── prompts/                      # Agent prompts (no domain logic in framework)
│   ├── __init__.py               # Path exports
│   ├── base.md                   # Generic capabilities shared by all agents
│   └── resolver.py               # Prompt resolution: base + agent pack role + tools
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
├── kb/                           # Knowledge base (FTS5 + semantic search)
│   ├── store.py                  # SQLite + FTS5 + sqlite-vec per named KB
│   ├── embedder.py               # Sidecar client (auto-start, embed())
│   └── sidecar.py                # HTTP server holding sentence-transformers model
└── monitors/                     # Background polling to fill webhook gaps
    ├── schema.py                 # Monitor record + interval parsing
    ├── registry.py               # Three-tier load/merge + writes
    ├── checks.py                 # Native check runners (pr_conflicts, stale_prs)
    └── scheduler.py              # Interval scheduler, dedup, event injection

skills/                           # Claude Code skill files
├── create-agent.md               # Guide for designing new agent packs
├── modastack.md                  # Guide for using modastack
├── linear-setup.md               # Linear API key setup
└── slack-setup.md                # Slack bot setup

agents/                           # Agent packs (portable agent definitions)
├── registry.yaml                 # Local pack index
└── eng-org/                      # Engineering org agent pack (reference impl)
    ├── agent.yaml                # Pack config (entry point, services, credentials)
    ├── agent.md                  # Shared base prompt for all roles
    ├── roles/                    # Role-specific prompts (folder format)
    │   ├── director/ROLE.md
    │   ├── project_lead/ROLE.md
    │   └── engineer/ROLE.md
    ├── tools/                    # Service interaction guides
    │   ├── github.md
    │   ├── linear.md
    │   └── slack.md
    ├── workflows/                # Workflow definitions
    │   ├── issue-lifecycle.yaml
    │   ├── pr-feedback.yaml
    │   └── ...
    └── monitors/                 # Background checks
        └── agent.yaml

.modastack/                       # Per-project installed agent + runtime state
├── agent.yaml                    # Installed config (check-in-able, ${VAR} refs for secrets)
├── .env                          # Secrets (gitignored, created by `modastack install`)
├── .gitignore                    # Ignores .env
├── roles/                        # Installed role prompts
├── tools/                        # Installed tool guides
├── workflows/                    # Installed + project workflows
├── monitors/                     # Installed + project monitors
├── sessions/                     # Agent session state
└── state/                        # PID files, logs, event server state
```

### Agent packs

A portable bundle of role prompts, workflows, monitors, and tool guides.
Packs are the distribution unit — install one and get a working agent
for a domain.

**Resolution order:**
1. `<project>/agents/<name>/` — project-level (checked in)
2. `<project>/.modastack/agents/<name>/` — local agents (overrides + cached)

**Role prompts** resolve from:
1. `<project>/.modastack/roles/<role>/ROLE.md` — project override
2. Agent pack `roles/<role>/ROLE.md`

**Tools** are markdown service guides in `tools/`. All tools load into
every role's context. Project tools in `.modastack/tools/` override
pack tools with the same filename.

### Workflows

YAML DAGs with three step types: **prompt** (agent executes + writes
handoff), **route** (deterministic branch on handoff value), **await**
(suspend until external event). Loaded from three tiers (most specific
wins): agent pack → project `.modastack/workflows/` → user
`~/.modastack/workflows/`.

See `skills/create-agent.md` for the full YAML reference.

### Monitors

Scheduled polling for conditions no webhook covers (merge conflicts,
stale PRs, deploy health). A monitor with `check:` uses a native
runner. Without one, the scheduler launches a short-lived check agent
that posts an event only if it finds something.

### Handoff contract

Each workflow step writes a handoff to
`<project>/.modastack/sessions/<session>/handoff-<step>.yaml`.
The orchestrator validates required fields and injects values into
the variable context for downstream steps.

### Config

All config is per-project. No global `~/.modastack/` directory — each
project is fully self-contained.

- `.modastack/agent.yaml` — check-in-able. Declares agent, roles,
  services, entry point, monitors. Secrets use `${ENV_VAR}` references.
- `.modastack/.env` — gitignored. Holds `SLACK_BOT_TOKEN`,
  `LINEAR_API_KEY`, `VENN_API_KEY`, etc. Created by `modastack install`.
- `.modastack/roles/`, `tools/`, `workflows/`, `monitors/` — installed
  from the agent pack by `modastack install`.

Per-project overrides in `.modastack/` for roles, workflows, monitors,
and tools.

## Tests

```bash
pytest tests/ --ignore=tests/integration/  # unit tests (~30s)
pytest tests/                              # all tests (~5min)
```

Integration tests drive real Claude Code sessions. Run before pushing.

**CI failure or production bug = integration test gap.** When a problem
is found in CI or a deployed system, STOP and write an integration test
that reproduces the failure BEFORE writing the fix. The test must fail
first, then the fix makes it pass. No exceptions.

## Releasing

1. Bump `version` in `pyproject.toml` and `VERSION`
2. `git tag v<version> && git push --tags`
3. GitHub Actions publishes to PyPI
