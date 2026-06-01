# modastack

Event-driven AI engineering team. A persistent Claude Code manager monitors Linear, GitHub, Slack, and engineer sessions — assigning work, routing phases, answering questions, and communicating with humans.

## Setup

```bash
cd ~/dev/modastack
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
modastack init --non-interactive
```

## First-time setup (agent guidance)

When setting up dispatch for a user, you MUST ask them for information.
Do NOT guess or skip these steps.

### Step 1: Install

```bash
cd ~/dev/modastack
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
modastack init --non-interactive
```

### Step 2: Setup the repo

Ask the user TWO things:

1. "What's your Linear API key? You can create one at
   https://linear.app/settings/api → click 'Create key'."

2. "What's your Linear project key? This is the prefix on your issue
   IDs (e.g., if issues look like ENG-42, the key is ENG)."

Then run:
```bash
modastack setup --linear-key <API_KEY> --linear-project <PROJECT_KEY>
```

This stores the API key per-project (in ~/.modastack/credentials.yaml,
not in the repo), registers the repo in ~/.modastack/config.yaml, and
installs engineer skills.

### Step 3: Verify

Show the user the registered repo entry and ask if the detected
Linear project and skills look correct.

### Important

- NEVER guess the Linear project key — always ask
- NEVER guess the Linear API key — always ask
- Credentials are per-project, stored in ~/.modastack/credentials.yaml
- All repo config lives in ~/.modastack/config.yaml (nothing in the target repo)

## Commands

```bash
modastack start                # start event loop
modastack spawn --repo R --task T  # spawn an ad-hoc engineer agent
modastack workflow run <name>  # run a named workflow
modastack workflow list        # list available workflows
modastack monitor list         # list background monitors (merged across tiers)
modastack monitor add <name>   # add a monitor (--interval, --description, --repo)
modastack monitor pause <name> # disable a monitor
modastack monitor remove <name>  # remove a user-added monitor
modastack status               # show active engineer sessions
modastack events               # show recent events from the bus
modastack message "text"       # inject a message into the manager session
modastack consult "question"   # ask the manager a question, block until response
modastack init                 # initialize global config
modastack setup [path]         # set up a repo — install skills, store credentials, register
modastack register <target>    # register a repo (local path or org/repo)
modastack repos                # list registered repos
```

## Architecture

Event-driven: events from GitHub, Linear, and Slack flow through a
Cloudflare event server to a persistent Claude Code manager session.
The manager receives ALL events and decides what to do — spawn an
ad-hoc engineer, run a structured workflow, or handle it directly.

```
modastack/                        # All Python code
├── cli.py                        # Click CLI entrypoint
├── config.py                     # Global config (~/.modastack/config.yaml)
├── scanner.py                    # Linear GraphQL polling
├── session.py                    # Engineer tmux session management
├── setup.py                      # Repo setup — skill install, auto-detection
├── board_setup.py                # Bootstrap Linear board with workflow states
├── manager/                      # Persistent manager + event system
│   ├── session.py                # Manager tmux session (start, resume, inject, capture)
│   └── events/
│       ├── bus.py                # Thread-safe in-process event queue
│       ├── consumer.py           # Drain bus → write events file → trigger manager
│       ├── pollers.py            # Background threads: workers (5s), Linear (30s), Slack (10s)
│       ├── webhook_server.py     # HTTP endpoints: /webhooks/github, /linear, /slack
│       └── slack_socket.py       # Slack Socket Mode WebSocket client
├── workflow/
│   ├── engine.py                 # DAG executor with hybrid LLM + deterministic nodes
│   ├── triggers.py               # Event → workflow matching, resolution chain
│   └── schema.py                 # WorkflowDef, NodeDef, YAML parsing
└── monitors/                     # Background polling to fill webhook gaps
    ├── schema.py                 # Monitor record + interval parsing
    ├── registry.py               # Three-tier load/merge + writes (add/pause/remove)
    ├── checks.py                 # Native check runners (pr_conflicts, stale_prs)
    └── scheduler.py              # Interval scheduler, dedup, synthetic event injection

monitors/defaults.yaml            # Built-in monitor defaults (shipped, read-only)

roles/                            # All skill/prompt content (no Python)
├── manager/
│   ├── prompt.md                 # Core manager behavior (general-purpose)
│   └── engineering.md            # Engineering manager role (domain-specific)
├── engineer/
│   ├── process/                  # Manager-routed lifecycle phases
│   │   ├── pickup/SKILL.md       # Take ticket, create worktree, triage
│   │   ├── spec/SKILL.md         # Write implementation spec
│   │   ├── implement/SKILL.md    # Build from spec, TDD, sub-agents
│   │   ├── prepare-pr/SKILL.md   # Create/update PR
│   │   └── feedback/SKILL.md     # Address review comments
│   └── practices/                # Modastack-native methodology skills
│       ├── triage/SKILL.md       # Task intake & classification
│       ├── build/SKILL.md        # Staff engineer coding methodology
│       ├── code-review/SKILL.md  # Mandatory quality gates
│       ├── ticketing-policy/SKILL.md
│       └── source-control-conventions/SKILL.md
├── product_manager/
│   ├── brand-identity/SKILL.md   # Brand discovery & visual identity
│   └── design-critic/SKILL.md    # Adversarial design doc reviewer
└── tools/                        # Shared tool reference (manager + engineers)
    ├── git/SKILL.md              # Git CLI commands
    ├── github/SKILL.md           # gh CLI commands
    ├── linear/SKILL.md           # Linear GraphQL API
    ├── slack/SKILL.md            # Slack setup & API
    ├── webhooks/SKILL.md         # Webhook setup guide
    └── notion/SKILL.md           # Notion integration (placeholder)

# GStack skills (review, ship, autoplan, investigate, office-hours,
# qa, plan-*-review) come from user-level ~/.claude/skills/ via
# gstack setup — not copied into this repo.
```

## Issue lifecycle

Linear states: Todo → In Progress → In Review → Done (+ Blocked)

The manager routes based on events:

| Event | Action |
|---|---|
| New issue with agent label | spawn tmux session + inject `/pickup`, move to In Progress |
| Worker state change | read handoff, inject next skill |
| PR merged | move to Done |
| Changes requested | inject `/feedback` into session |
| Human replied | inject answer into tmux session |

Internal phases (triage, spec, implement) happen within "In Progress".
The handoff file (`~/.modastack/handoffs/<issue_id>.md`) tracks which
sub-phase the agent is in. Linear doesn't need to know.

## Handoff contract

Engineers write `~/.modastack/handoffs/<issue_id>.md`:

```yaml
---
issue_id: AGD-12
title: Add rate limiting
worktree: /path/to/worktree
branch: agent/agd-12
phase: spec_complete
spec_url: https://github.com/org/repo/issues/12
complexity: medium
---

## Status
Spec written to issue description.
```

Each agent reads the handoff, does its work, then goes idle. The
manager detects state changes via the worker poller and routes to
the next skill.

## Custom workflows

Workflows are YAML DAGs loaded from three tiers (most specific wins):
1. `<repo>/.modastack/workflows/` — repo-specific overrides
2. `~/.modastack/workflows/` — user-level overrides
3. `<modastack>/workflows/` — built-in defaults

Per-repo context from `.modastack.yaml`'s `context:` section is available
as `${{repo.key}}` in workflow templates. See `docs/CUSTOM_WORKFLOWS.md`
for the full reference and `workflows/examples/` for non-dev examples
(content review, research).

## Background monitors

Monitors are scheduled polling tasks that fill webhook gaps — conditions
no webhook fires for (merge conflicts, stale PRs, deploy health). A monitor
is a small human-readable YAML record (`name`, `description`, `interval`,
`event`) loaded from three tiers, later tiers overriding earlier by `name`:

1. `monitors/defaults.yaml` — built-in, shipped, read-only at runtime
2. `~/.modastack/monitors.yaml` — user globals (apply to all repos)
3. `<repo>/.modastack.yaml` under `monitors:` — repo-specific

Set `enabled: false` on a repo-specific entry to opt that repo out of an
inherited monitor. The scheduler (a thread in the manager process) runs each
monitor on its interval, deduplicates detected conditions against
`~/.modastack/monitor_state.json`, and injects a synthetic event onto the
same queue webhooks use — so the manager routes it like any other event.

A monitor with a `check:` field uses a native runner in
`modastack/monitors/checks.py` (deterministic, deduplicated). Without one,
the scheduler launches a short-lived, non-interactive check agent out-of-band
(`modastack spawn --non-interactive --post-event <event>`): it performs the
check from the `description`, captures the result, and posts an event back to
the bus *only* if it finds something. The manager never sees the check
process — only the resulting finding — so its context stays clean and
responsive. PR conflict detection ships as a default and works out of the box.

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/  # unit tests (~30s)
pytest tests/                              # all tests including integration (~5min)
```

Integration tests drive real Claude Code sessions. Run them before
pushing to main or opening a PR — not on every edit.
