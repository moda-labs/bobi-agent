# modastack

Event-driven AI agent framework. Spawn persistent agents that subscribe
to real-world events, react autonomously, and stay interactive. Domain
behavior comes from agent teams — the framework has no topology opinions.

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
modastack setup                   # interactive onboarding: design, build, and install a team
modastack install <path>          # install an agent team from a local path or registry
modastack start                   # start the installed agent
modastack stop                    # stop the running instance
modastack restart                 # stop and restart
modastack start --fresh           # wipe session and start clean

modastack agents launch -w W --role R --task T  # launch an agent
modastack agents list             # list active agents
modastack agents show <id>        # inspect a specific agent
modastack agents cancel <id>      # cancel a running agent
modastack agents browse           # browse remote agent registry
modastack agents update <name>    # update agent teams from remote
modastack agents add-registry <repo>  # add a remote registry

modastack ask "question"          # ask an agent, block until response
modastack message "text"          # inject a message into any session
modastack compact                 # flush + rotate a session's context now (default: manager)
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

modastack costs                   # show cost attribution by provider
modastack costs --by model        # group by model
modastack costs --by role         # group by agent role
modastack costs --by session      # group by session

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
├── registry.py                   # Agent team registry (fetch, update, browse)
├── inbox.py                      # Per-session message delivery
├── prompts/                      # Agent prompts (no domain logic in framework)
│   ├── __init__.py               # Path exports
│   ├── base.md                   # Generic capabilities shared by all agents
│   └── resolver.py               # Prompt resolution: base + agent team role + tools
├── events/                       # Generic event infrastructure
│   ├── client.py                 # WebSocket client (connects to event server)
│   ├── server.py                 # Local event server launcher (Node.js)
│   ├── drain.py                  # Event queue → session inbox delivery
│   └── subscriptions.py          # Subscription key builder
├── workflow/
│   ├── orchestrator.py           # DAG executor with deterministic routing
│   ├── triggers.py               # Workflow discovery from installed pack
│   ├── schema.py                 # WorkflowDef, StepDef, YAML parsing
│   ├── state.py                  # JSON persistence for workflow runs
│   └── variables.py              # Variable resolution, safe condition evaluation
├── kb/                           # Knowledge base (FTS5 + semantic search)
│   ├── store.py                  # SQLite + FTS5 + sqlite-vec per named KB
│   ├── embedder.py               # Sidecar client (auto-start, embed())
│   └── sidecar.py                # HTTP server holding fastembed/ONNX model
└── monitors/                     # Background polling to fill webhook gaps
    ├── schema.py                 # Monitor record + interval parsing
    ├── registry.py               # Installed defaults + project overrides
    ├── checks.py                 # Native check runners (pr_conflicts, stale_prs)
    └── scheduler.py              # Interval scheduler; sole dedup + publish path for findings

skills/                           # Claude Code skill files (also in modastack/skills/ as package data)
├── create-agent.md               # Guide for designing new agent teams
├── modastack.md                  # Guide for using modastack
├── linear-setup.md               # Linear API key setup
└── slack-setup.md                # Slack bot setup

agents/                           # Agent teams (portable agent definitions)
├── registry.yaml                 # Local team index
└── eng-team-core/                 # Pristine, portable engineering org (reference impl)
    ├── agent.yaml                # Team config (entry point, services, credentials)
    ├── agent.md                  # Shared base prompt for all roles
    ├── roles/                    # Role-specific prompts (folder format)
    │   ├── director/ROLE.md
    │   ├── project_lead/ROLE.md
    │   └── engineer/ROLE.md
    ├── tools/                    # Service interaction guides (github.md, slack.md)
    ├── workflows/                # Workflow definitions
    │   ├── issue-lifecycle.yaml
    │   ├── pr-feedback.yaml
    │   └── ...
    └── monitors/                 # Background checks
        └── defaults.yaml
                                  # Moda's house team = `from: eng-team-core` +
                                  # overlay, in the private moda-agent-teams repo.

.modastack/                       # Per-project installed agent + runtime state
├── agent.yaml                    # Installed config (check-in-able, ${VAR} refs for secrets)
├── .env                          # Secrets (gitignored, created by `modastack install`)
├── .gitignore                    # Ignores .env
├── roles/                        # Installed role prompts
├── tools/                        # Installed tool guides
├── workflows/                    # Installed + project workflows
├── monitors/                     # Installed + project monitors
├── context/                      # Installed reference files (read on demand)
├── sessions/                     # Agent session state
└── state/                        # PID files, logs, event server state

workspace/                        # User-owned domain files + agent work products
                                  # (seeded once from the team's workspace/)
```

### Agent teams

A portable bundle of role prompts, workflows, monitors, and tool guides.
Teams are the distribution unit — install one and get a working agent
for a domain.

**Resolution order:**
1. `<project>/agents/<name>/` — project-level (checked in)
2. `<project>/.modastack/agents/<name>/` — local agents (overrides + cached)

**Inheritance (`from:`).** A team may declare `from: <base-team>` and contribute
only its delta — Docker-style composition (`modastack/compose.py`, #446/#451). At
**install/deploy time**, compose walks the `from:` chain (`base → … → leaf`,
local-always-wins + fail-fast on a pin mismatch) and freezes one flat
`.modastack/` image: prose surfaces (agent.md, ROLE.md) **concatenate** in chain
order (`replace: true` frontmatter to override); structured surfaces (tools,
workflows, monitors, agent.yaml) **deep-merge by key** (`build` deps accrete,
`prune:` removes inherited items). Nothing downstream learns about layers — the
runtime resolver reads only the frozen output. `install --pinned` resolves
registry-only at locked versions for reproducible CI/deploy. The pristine
`agents/eng-team-core/` is the public base; Moda's house team is
`from: eng-team-core` + an overlay in the private `moda-agent-teams` repo.

**Role prompts** are read by the runtime resolver from the frozen
`<project>/.modastack/roles/<role>/ROLE.md` — which is now compose **output**.
Customize by editing the leaf team source (or adding a `replace: true` overlay
ROLE.md), not by dropping an override into `.modastack/`.

**Tools** are markdown service guides in `tools/`. All tools load into
every role's context. In a `from:` chain, later layers' tools override
earlier ones by filename.

**Context** files in `context/` are team-shipped reference content
(rubrics, methodology, examples). Installed frozen to
`.modastack/context/`; agents get an index (path + first line) in their
prompt and read files on demand — contents are never inlined.

**Workspace** files in `workspace/` are user-owned domain content and
agent work products. Install seeds `<project>/workspace/` from the
team's `workspace/` — each file is copied only if absent, so reinstall
never overwrites user edits. What lives there is defined by role
prompts, not the framework.

### Workflows

YAML DAGs with three step types: **prompt** (agent executes + writes
handoff), **route** (deterministic branch on handoff value), **await**
(suspend until external event). Loaded exclusively from the installed
pack image at `.modastack/workflows/`.

See `skills/create-agent.md` for the full YAML reference.

### Monitors

Scheduled polling for conditions no webhook covers (merge conflicts,
stale PRs, deploy health). Every monitor flavor (notify, command, native
`check:`, description-only check agent) is just a condition detector;
dedup and publishing are one shared path — the scheduler reconciles
detected conditions against persisted state and publishes new ones
through the event server, like any other event. A description-only
monitor's check agent runs out-of-band, only observes, and returns a
verdict; the scheduler converts it to conditions and publishes.

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
  from the agent team by `modastack install`.

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

## Ticket state

`docs/TICKET_STATE.md` is the living overview of all open GitHub issues —
tracks/epics, what's blocked vs. ready, and which one-offs are ready to hand to
the `modastack` bot. **Keep it current:** whenever an issue is opened, closed,
assigned, unblocked, or moves tracks during a session, update the relevant table
there in the same session, and bump its "Last reviewed" date + open-count. Read
it first when asked about the state of the work or what to pick up next.

## Design System (modastack setup web UI)

Before any visual or UX decision on the `modastack setup` web UI, read `DESIGN.md`
at the repo root. It defines the design direction (warm light chrome + single
dark CRT slab, mono-accented identity, system-fonts-only, static space-retro),
the mode-aware stage machine, the policy/customization model, and the
digestion-prompt architecture. Live mockups in `docs/design/`. Do not deviate
without explicit approval. `DESIGN.md` supersedes the visual/UX assumptions in
`~/.claude/plans/sleepy-crunching-pnueli.md`.

## Contributing

**Feature PRs must not bump the version or edit `CHANGELOG.md`.** Leave
`VERSION`, the `version` field in `pyproject.toml`, and `CHANGELOG.md`
untouched — version bumps and changelog entries are added at release time only
(see [Releasing](#releasing)). This keeps the changelog clean and avoids merge
conflicts when several PRs land together.

Write a PR description with enough detail that the changelog entry can be
written from it at release time: what changed, why, and the ticket id.

## Releasing

Version bumps and `CHANGELOG.md` entries happen **only at release time** — never
in feature PRs (see [Contributing](#contributing)). To cut a release:

1. Bump `version` in `pyproject.toml` and `VERSION`, and add a `CHANGELOG.md`
   entry summarizing the PRs merged since the last release.
2. Publish a GitHub Release: `gh release create v<version> --target main …`
   (creates the tag **and** publishes the Release — that event is the gate).
3. Publishing the Release runs `release.yml` — one gated pipeline:
   subscription-login smoke → build the wheel once → build the canary **from that
   wheel** + `CANARY-OK` smoke (the gate) → then, in parallel: publish the same
   wheel to **PyPI** (+ Cloudflare event-server + Homebrew) **and** roll the Fly
   fleet → reconcile team packages + secrets.

The canary runs the exact wheel we publish, so it gates both the PyPI upload and
the fleet roll. PyPI trusted publishing is configured for `release.yml` +
environment `pypi`. A bare `git push --tags` no longer publishes — publish a Release.
