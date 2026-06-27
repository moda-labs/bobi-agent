# bobi

Event-driven AI agent framework. Spawn persistent agents that subscribe
to real-world events, react autonomously, and stay interactive. Domain
behavior comes from agent teams — the framework has no topology opinions.

## Install

```bash
uv tool install bobi
```

For development:

```bash
cd ~/dev/bobi
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Commands

```bash
bobi setup                              # interactive onboarding
bobi agents install <path> --name <name> # install a Bobi Agent package
bobi agents list                       # list installed Bobi Agents
bobi agents browse                     # browse remote agent registry
bobi agents update <name>              # update agent packages from remote
bobi agents add-registry <repo>  # add a remote registry

bobi agent <name> start             # start the named Bobi Agent
bobi agent <name> stop              # stop the running instance
bobi agent <name> restart           # stop and restart
bobi agent <name> start --fresh     # wipe session and start clean
bobi agent <name> status            # show manager and sub-agent status
bobi agent <name> doctor            # system health check

bobi agent <name> subagents launch -w W --role R --task T
bobi agent <name> subagents list
bobi agent <name> subagents show <id>
bobi agent <name> subagents cancel <id>

bobi agent <name> ask "question"    # ask the manager, block until response
bobi agent <name> message "text"    # inject a message into a session
bobi agent <name> compact           # flush + rotate a session's context
bobi agent <name> events            # recent events and decisions
bobi agent <name> transcript show <sess>
bobi agent <name> transcript search <q>

bobi agent <name> workflows list
bobi agent <name> workflows status
bobi agent <name> workflows validate <f>
bobi agent <name> monitors list
bobi agent <name> monitors add <monitor>
bobi agent <name> monitors pause <monitor>
bobi agent <name> monitors remove <monitor>
bobi agent <name> roles list

bobi agent <name> kb create <kb>
bobi agent <name> kb add <kb> --file F
bobi agent <name> kb add <kb> --text T
bobi agent <name> kb search <kb> "q"
bobi agent <name> kb list
bobi agent <name> kb info <kb>
bobi agent <name> kb remove <kb>

bobi agent <name> costs
bobi agent <name> costs --by model
bobi agent <name> costs --by role
bobi agent <name> costs --by session

bobi skill                   # print the bobi usage guide
bobi skill <name>            # print a specific skill guide

bobi create-slack-bot        # generate a Slack app manifest + one-click create link
bobi agent <name> event-server start      # start the local event server
bobi agent <name> event-server stop       # stop the local event server
```

## Architecture

Every agent is a symmetric node — it subscribes to event topics and
receives events via a centralized event server (Cloudflare Worker or
local Node.js). The event server supports topic-based pub/sub plus
webhook ingestion for GitHub, Linear, Slack, and any custom source.

```
bobi/                        # Framework (Python package)
├── cli.py                        # Click CLI entrypoint
├── config.py                     # Runtime config (run/package/agent.yaml)
├── session.py                    # Brain session + inbox drain loop
├── subagent.py                   # Agent executor (blocking + detached)
├── sdk.py                        # Session registry, activity logging
├── registry.py                   # Agent team registry (fetch, update, browse)
├── inbox.py                      # Per-session message delivery
├── brain/                        # Pluggable agent "brain" (#485)
│   ├── base.py                   # BrainSession/BrainFactory + normalized messages
│   └── claude.py                 # Claude Code adapter (the only claude_agent_sdk site)
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

skills/                           # Canonical Claude Code skill files
├── create-agent.md               # Guide for designing new agent teams
├── bobi.md                  # Guide for using bobi
├── linear-setup.md               # Linear API key setup
└── slack-setup.md                # Slack bot setup

agents/                           # Agent teams (portable agent definitions)
├── registry.yaml                 # Local team index
└── eng-team/                 # Pristine, portable engineering org (reference impl)
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
                                  # Moda's house team = `from: eng-team` +
                                  # overlay, in the private moda-agent-teams repo.

$BOBI_HOME/                       # Defaults to ~/.bobi; env-var configurable
├── config.yaml                   # Machine-level registry/config
├── cache/                        # Registry packages and build artifacts
└── agents/<name>/
    ├── src/                      # Editable Bobi Agent source by default
    └── run/                      # Runtime root, exported as BOBI_ROOT
        ├── package/              # Frozen installed package
        │   ├── agent.yaml
        │   ├── roles/
        │   ├── tools/
        │   ├── workflows/
        │   ├── monitors/
        │   └── context/
        ├── state/                # Sessions, pid files, logs, policy, KBs
        ├── workspace/            # User-owned domain files and outputs
        └── .env                  # Runtime credentials
```

### Agent teams

A portable bundle of role prompts, workflows, monitors, and tool guides.
Teams are the distribution unit — install one and get a working agent
for a domain.

**Resolution order.** An install source can be a local source directory,
local `.tar.gz`, public `.tar.gz` URL, or registry name. Registry packages are
cached under `$BOBI_HOME/cache/agents/`; installed named agents live under
`$BOBI_HOME/agents/<name>/`.

**Inheritance (`from:`).** A team may declare `from: <base-team>` and contribute
only its delta — Docker-style composition (`bobi/compose.py`, #446/#451). At
**install/deploy time**, compose walks the `from:` chain (`base → … → leaf`,
local-always-wins + fail-fast on a pin mismatch) and freezes one flat
`run/package/` image: prose surfaces (agent.md, ROLE.md) **concatenate** in chain
order (`replace: true` frontmatter to override); structured surfaces (tools,
workflows, monitors, agent.yaml) **deep-merge by key** (`build` deps accrete,
`prune:` removes inherited items). Nothing downstream learns about layers — the
runtime resolver reads only the frozen output. `install --pinned` resolves
registry-only at locked versions for reproducible CI/deploy. The pristine
`agents/eng-team/` is the public base; Moda's house team is
`from: eng-team` + an overlay in the private `moda-agent-teams` repo.

**Role prompts** are read by the runtime resolver from the frozen
`$BOBI_HOME/agents/<agent>/run/package/roles/<role>/ROLE.md` — which is compose
**output**. Customize by editing the leaf team source (or adding a
`replace: true` overlay ROLE.md), then reinstall.

**Tools** are markdown service guides in `tools/`. All tools load into
every role's context. In a `from:` chain, later layers' tools override
earlier ones by filename.

**Tool library** (`tool_library:` in `agent.yaml`) is an opt-in catalog of
baked CLI tools (`bobi/tool_library/`). A team lists entries by id
(`tool_library: [codex, venn]`) and `compose.py` expands each into its
`requires:` + `build:` + a `tools/<id>.md` guide at build time — one pinned
definition + one guide, reusable across teams, with the pin de-duped to a
single string across `from:` layers. Only `kind: cli` ships today (`codex`,
`venn`, `openai`); `kind: mcp`/`skill` are reserved (#398/#428). A local
`tools/<id>.md` or explicit `requires:` for the same name wins; the key is
consumed at compose and never emitted. See `docs/specs/416-tool-library.md`.

**Context** files in `context/` are team-shipped reference content
(rubrics, methodology, examples). Installed frozen to
`run/package/context/`; agents get an index (path + first line) in their
prompt and read files on demand — contents are never inlined.

**Workspace** files in `workspace/` are user-owned domain content and
agent work products. Install seeds
`$BOBI_HOME/agents/<agent>/run/workspace/` from the team's `workspace/` — each
file is copied only if absent, so reinstall never overwrites user edits. What
lives there is defined by role prompts, not the framework.

### Workflows

YAML DAGs with three step types: **prompt** (agent executes + writes
handoff), **route** (deterministic branch on handoff value), **await**
(suspend until external event). Loaded exclusively from the installed
pack image at `run/package/workflows/`.

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

The **`policy-curator`** (`curator: true`) is the one flavor whose check
agent **writes an artifact** instead of returning a verdict: it distills new
agent transcripts (windowed on `messages.id` since a success-advanced cursor,
under a per-run input cap) into the team-scoped, capped, rewritten-in-place
`run/state/policy.md` — the curated learning substrate that replaces the
old append-only decision log, injected read-only into every agent's prompt as
`## Team Policy`. On a rewrite it publishes `policy.updated`; delivery is
passive by default (agents re-read on their next prompt), with an inbox push
only for `urgent` changes. See `docs/specs/456-policy-curator.md`.

### Recovery layers

The director's liveness is defended in depth — each layer recovers a failure
the one inside it structurally cannot (#464):

```
Fly Machines init (machine restart policy)     ← outermost backstop (process death)
  └─ bobi supervise (self-heal watchdog)   ← restarts a wedged DIRECTOR
       └─ bobi agent <name> start (manager process)
            └─ director session (claude subprocess)
                 └─ stall-recovery (director→ENGINEER)  ← restarts wedged engineers
```

- **stall-recovery** runs inside the director and recovers stalled *engineer*
  sessions; it cannot recover the director itself.
- **`bobi supervise`** (`bobi/watchdog.py`) is the layer below the
  director: the container entrypoint runs it as the parent, it spawns the
  manager as a child, and it polls the `/health` endpoint's `manager` block
  (`status` + server-derived `idle_seconds`). It restarts the manager **iff the
  director is in an active turn state (`starting`/`running`) AND idle past
  `WATCHDOG_STALL_THRESHOLD`** — so a healthy *idle* director (frozen
  `last_activity` parked at `inbox.recv`) is never false-killed. Bounded retry +
  backoff with shared crash-loop containment; on budget exhaustion it escalates
  (loud log + optional Slack via `WATCHDOG_ALERT_CHANNEL`) and exits non-zero so
  Fly restarts the machine. It runs no agent loop, so it cannot wedge from the
  same cause. Tunables: `WATCHDOG_*` env vars. This is **defense-in-depth**, not
  a replacement for #456/PR #460 (which bounds the one *known* rotation-reconnect
  hang); the watchdog backstops *unknown* wedge classes. See
  `docs/specs/464-manager-self-heal-watchdog.md`.

### Handoff contract

Each workflow step writes a handoff to
`$BOBI_HOME/agents/<agent>/run/state/sessions/<session>/handoff-<step>.yaml`.
The orchestrator validates required fields and injects values into
the variable context for downstream steps.

### Config

`BOBI_HOME` is the single low-level home root. It is configurable only by
environment variable and defaults to `~/.bobi`. Runtime identity is selected
by the named-agent CLI or inherited `BOBI_ROOT`; code does not infer a Bobi
Agent from the current working directory.

- `$BOBI_HOME/config.yaml` — machine-level registry/config.
- `run/package/agent.yaml` — frozen installed package config. Declares agent,
  roles, services, entry point, monitors, and `${ENV_VAR}` references.
- `run/.env` — runtime credentials for that named agent. Created by
  `bobi agents install`.
- `run/package/roles/`, `tools/`, `workflows/`, `monitors/`, `context/` —
  generated install output. Edit source in `src/` or another explicit source
  directory and reinstall.

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
the `bobi` bot. **Keep it current:** whenever an issue is opened, closed,
assigned, unblocked, or moves tracks during a session, update the relevant table
there in the same session, and bump its "Last reviewed" date + open-count. Read
it first when asked about the state of the work or what to pick up next.

## Design System (bobi setup web UI)

Before any visual or UX decision on the `bobi setup` web UI, read `DESIGN.md`
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
