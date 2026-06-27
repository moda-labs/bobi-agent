# Bobi

[![CI](https://github.com/moda-labs/bobi-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/moda-labs/bobi-agent/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bobi)](https://pypi.org/project/bobi/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Bobi is a CLI toolkit for building proactive agent teams** — agents that are
both human-responsive and act autonomously in response to real-world events:
GitHub PRs, Slack messages, ticket updates, incoming emails, or any webhook. You
define the roles and behavior of your team; Bobi builds and runs it for you.

Under the hood, every agent is a [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
session — so the whole system runs on a flat-rate Claude Pro or Max plan with **no
per-token API costs** (API keys work too).

## Why Bobi

- **Proactive, not just reactive.** Agents subscribe to event topics and act on
  GitHub, Slack, Linear, and custom webhooks — not only when a human types.
- **Flat-rate by design.** Each agent is a Claude Code session, so a Pro/Max plan
  covers the whole team with no per-token metering.
- **Agent teams are portable.** A team bundles roles, workflows, monitors, and
  tool guides into one installable unit. Install one and get a working agent for a
  domain; share your own via a registry.
- **Deterministic workflows.** YAML DAGs force multi-step work through a fixed
  recipe (code review before merge, CI before PRs) instead of hoping the model
  remembers.
- **Monitors fill the gaps.** Scheduled checks detect conditions no webhook fires
  for — merge conflicts, stale PRs, deploy drift — and inject them onto the same
  event bus.
- **No topology opinions.** The framework is generic; the shape of your org —
  roles, relationships, subscriptions — lives entirely in the agent team.

## Installation

### What you need

- Python 3.11+
- Git
- Node.js + npm
- [`uv`](https://astral.sh/uv/) (or `pipx`)

### 1) Install and authenticate [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

This is the reasoning engine that powers Bobi:

```bash
npm install -g @anthropic-ai/claude-code
claude
```

Follow the prompts to log in with your Anthropic account (Pro, Max, or API key).

### 2) Install Bobi

Once Claude Code is set up, paste this into your Claude Code session:

```plaintext
Install bobi using https://raw.githubusercontent.com/moda-labs/bobi-agent/main/scripts/install.sh
```

Or install manually:

```bash
uv tool install bobi
```

On macOS you can also use Homebrew:

```bash
brew install moda-labs/bobi-agent/bobi
```

See [scripts/install.sh](scripts/install.sh) for what the installer does.

## Quick Start

Install a pre-built team and start talking to it:

```bash
# Install the reference engineering team and start it
bobi agents install eng-team --name eng-team
bobi agent eng-team start

# Ask the manager a question (blocks until it responds)
bobi agent eng-team ask "What can I help with right now?"

# Hand it a one-off task
bobi agent eng-team subagents launch --role engineer --task "Fix the login bug"
```

Prefer to design your own team from scratch? Run the interactive wizard instead:

```bash
bobi setup            # go from an idea to a runnable team, interactively
```

### Add integrations (optional)

To trigger agents from Slack or let them act on Linear, the team needs
credentials. `bobi agents install` prompts for any secrets the team's
`agent.yaml` references and writes them to `run/.env` (never commit this file).
Then start the event server so webhooks can reach your agents:

```bash
bobi agent eng-team event-server start    # receive webhooks locally
bobi create-slack-bot --app-name "Bobi"   # generate a Slack app to create
```

Step-by-step guides: **[Slack setup](skills/slack-setup.md)** ·
**[Linear setup](skills/linear-setup.md)**.

## How It Works

The topology below is just one example — the [`eng-team`](agents/eng-team/) team
for software orgs. The event server, monitor scheduler, and agent messaging are
infrastructure every deployment gets; the arrangement of agents, their roles, and
what they subscribe to is defined entirely by the agent team.

```
─ GitHub · Slack · Linear · any webhooks
                 │
                 ▼
    ┌───────────────────────────┐
    │       Event Server        │
    │  (Cloudflare or local)    │
    │                           │
    │  pub/sub · cursor replay  │
    └─────────────┬─────────────┘
                  │ WebSocket
┌─────────────────┼──────────────────────────────────────┐
│ Agent Team      │                                       │
│                 │          ┌──────────────────────┐     │
│                 │          │      Monitors        │     │
│                 │          │    (runs locally)    │     │
│                 │          │  pr_conflicts  15m   │     │
│                 │          │  stale_prs     2h    │     │
│                 │          │  deploy_drift  1h    │     │
│                 │          └──────────┬───────────┘     │
│                 ▼                     │                 │
│       ┌──────────────────────────┐    │                 │
│       │        Director     ◄─────────┘                 │
│       │       (persistent)       │                      │
│       └─────┬──────────────┬─────┘                      │
│             │              │                            │
│             ▼              ▼                            │
│    ┌──────────────┐ ┌──────────────┐                    │
│    │ Project Lead │ │ Project Lead │                    │
│    │ (persistent) │ │ (persistent) │                    │
│    │   (repo-a)   │ │   (repo-b)   │                    │
│    └──────┬───────┘ └──────┬───────┘                    │
│           │                │                            │
│      ┌────┴────┐      ┌────┴────┐                       │
│      ▼         ▼      ▼         ▼                       │
│   ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────────┐           │
│   │ task │ │ task │ │ task │ │   workflow   │           │
│   │agent │ │agent │ │agent │ │  (YAML DAG)  │           │
│   └──────┘ └──────┘ └──────┘ │ step → step  │           │
│                              └──────────────┘           │
└─────────────────────────────────────────────────────────┘
```

## Agent Teams

An agent team is everything an agent needs to operate in a domain: role prompts,
workflows, monitors, tools, and extra context/content. Teams are the distribution
unit — install one and get a working agent.

```
agents/eng-team/            # ← browse the reference team
├── agent.yaml              # team config: entry role, services, event sources
├── agent.md                # shared base prompt for all roles
├── roles/
│   ├── director/ROLE.md    # engineering director
│   ├── project_lead/ROLE.md
│   └── engineer/ROLE.md
├── workflows/
│   ├── issue-lifecycle.yaml
│   ├── pr-feedback.yaml
│   └── build-failure.yaml
├── monitors/
│   └── defaults.yaml       # watch for PR conflicts, stale PRs
└── tools/
    ├── github.md           # service interaction guides
    ├── linear.md
    └── slack.md
```

### Create your own team

Run this prompt in your chat assistant of choice (ChatGPT, Claude, etc.) to launch
a guided process that generates a team for your domain:

```plaintext
Read https://raw.githubusercontent.com/moda-labs/bobi-agent/main/skills/create-agent.md and help me build a bobi agent
```

### Agent team registry

Bobi maintains a team registry at [`agents/`](agents/). Install teams from the
registry, or run your own private registry and add it to your installation:

```bash
bobi agents browse                     # see available teams from all registries
bobi agents update eng-team            # install or update
bobi agents add-registry myorg/agents  # add a private registry
```

Built a general-purpose team worth sharing? Open a PR to add it to the global
registry.

## Event Server

Agents receive real-world events (GitHub, Slack, Linear, custom webhooks) through
a centralized event server. Three options:

| | Local | Self-hosted Cloudflare | Hosted (coming soon) |
|---|---|---|---|
| Setup | `bobi agent <name> event-server start` | Deploy the worker yourself | Sign up at [bobi.dev](https://bobi.dev) |
| Hosting | Runs on your machine | Your Cloudflare account, always on | Managed by Moda Labs |
| Webhook routing | Requires [ngrok](https://ngrok.com/) or similar tunnel | Stable URL, no tunnel | Stable URL, no tunnel |
| GitHub/Slack apps | Create your own | Create your own | Install our pre-built apps |
| Storage | In-memory | Durable Objects (persistent) | Durable Objects (persistent) |
| Best for | Local dev, quick experimentation | Self-hosted production | Fastest path to production |

## Monitors

Not every event comes from a webhook. Monitors are scheduled checks that detect
conditions the outside world doesn't notify you about — merge conflicts, stale
PRs, deploy drift, SLA breaches — and inject them into the same event bus as
webhooks.

```yaml
# monitors/defaults.yaml
monitors:
  - name: pr_conflicts
    description: Check for PRs with merge conflicts
    interval: 15m
    event: monitor/pr.conflict_detected
    check: pr_conflicts              # native Python check function

  - name: stale_prs
    description: PRs open longer than 3 days with no activity
    interval: 2h
    event: monitor/pr.stale_detected
```

A monitor with a `check:` field runs a deterministic native function — fast,
deduplicated, no LLM needed. A monitor without one spawns a short-lived agent that
evaluates the description and posts an event only if it finds something. Either
way, the resulting event is indistinguishable from a webhook — subscribing agents
handle it the same way.

```bash
bobi agent <name> monitors list              # see all active monitors
bobi agent <name> monitors add stale-deploys --interval 1h --description "Deploys older than 24h"
bobi agent <name> monitors pause pr_conflicts
```

## Workflows

Knowledge work often requires multi-step processes where LLM variation or skipped
steps aren't acceptable — for example, requiring that every change pass automated
code review and CI before a PR opens.

Workflows are YAML DAGs that force agents to follow a recipe in order. Each step
can route to a different role (e.g. `engineer` vs `security-reviewer`) to give the
right context at each stage:

```yaml
name: incident-response
trigger: "PagerDuty alert fires for a production service"
steps:
  - name: triage
    agent: oncall
    prompt: "Assess severity, check recent deploys, identify affected services"
    handoff:
      required: [severity, affected_services]

  - name: route
    if: "severity == critical"
    goto: escalate
    else: investigate

  - name: investigate
    agent: engineer
    prompt: "Find root cause using logs, metrics, and traces"
```

You normally don't write these by hand — the [create-agent](skills/create-agent.md)
skill guides you through it.

## CLI

```bash
# Agents
bobi agent <name> subagents launch -w <workflow> --role <role> --task "context"
bobi agents list
bobi agent <name> subagents show <id>
bobi agent <name> subagents cancel <id>

# Communication
bobi agent <name> ask "question"          # blocks until response
bobi agent <name> message "update"        # fire-and-forget

# Observability
bobi agent <name> status                  # active agents
bobi agent <name> events                  # recent events and decisions
bobi agent <name> transcript show <sess>  # session transcript
bobi agent <name> doctor                  # system health check

# Workflows & monitors
bobi agent <name> workflows list
bobi agent <name> monitors list
bobi agent <name> roles list
```

Full command reference: [skills/bobi.md](skills/bobi.md).

## Configuration

See the setup guides for [Slack](skills/slack-setup.md) and
[Linear](skills/linear-setup.md).

`BOBI_HOME` is the single low-level home root. It defaults to `~/.bobi` and is
configurable only by environment variable. Each named Bobi Agent lives under
`$BOBI_HOME/agents/<name>/`, with editable source in `src/`, installed package
files in `run/package/`, mutable state in `run/state/`, workspace files in
`run/workspace/`, and credentials in `run/.env`.

`agent.yaml` is the package config file (roles, services, monitors, credentials);
secrets use `${ENV_VAR}` references resolved from the environment, with `run/.env`
loaded at startup:

```yaml
# run/package/agent.yaml
services:
  - name: slack
    bot_token: ${SLACK_BOT_TOKEN}
  - name: linear
    api_key: ${LINEAR_API_KEY}
event_server_url: https://bobi-events.example.workers.dev
```

Custom roles, workflows, monitors, and tools are source-package changes. Edit the
source under `src/` (or another explicit source directory), then reinstall so
`run/package/` is regenerated.

## Security

Bobi agents take real actions — they push commits, comment on issues, and message
your team — so treat a running agent like any other automation with access to your
accounts:

- **Run teams you trust.** Installing a team runs its prompts and tool guides
  against your credentials. Review a team before installing it, the same way you'd
  review a dependency.
- **Keep secrets in `run/.env`.** Credentials live in per-agent `.env` files
  resolved via `${ENV_VAR}` references. Never commit them.
- **Local by default.** The local event server runs on your machine; nothing
  leaves it until you connect a remote event server or messaging integration.

## Documentation

| Goal | Read |
|---|---|
| Run and operate Bobi | [skills/bobi.md](skills/bobi.md) — full CLI reference |
| Build your own team | [skills/create-agent.md](skills/create-agent.md) · [docs/BUILDING_AGENT_TEAMS.md](docs/BUILDING_AGENT_TEAMS.md) |
| Understand the model | [docs/EVENT_DRIVEN_AGENTS.md](docs/EVENT_DRIVEN_AGENTS.md) — why event-driven agents |
| Onboard a team | [docs/AGENT_TEAM_ONBOARDING.md](docs/AGENT_TEAM_ONBOARDING.md) |
| Connect Slack / Linear | [skills/slack-setup.md](skills/slack-setup.md) · [skills/linear-setup.md](skills/linear-setup.md) |
| Deploy to production | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) · [docs/CONTAINER.md](docs/CONTAINER.md) |

## Development

```bash
git clone https://github.com/moda-labs/bobi-agent.git
cd bobi-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/
```

## License

MIT
