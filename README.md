# Modastack

[![CI](https://github.com/moda-labs/modastack/actions/workflows/ci.yml/badge.svg)](https://github.com/moda-labs/modastack/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/modastack)](https://pypi.org/project/modastack/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Modastack is a CLI toolkit that provides the building blocks for creating proactive agent teams: agents that are both human-responsive, and can act autonomously in response to real-world events like GitHub PRs, Slack messages, ticket updates, or incoming emails. You define the roles and functionality of your agent team, and Modastack builds and runs it for you.

Under the hood, every agent is a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) session — which means the entire system runs on a flat-rate Claude Pro or Max plan with no per-token API costs. API key usage is also supported.

## Installation

### Prerequisites

Install and authenticate [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — this is the reasoning engine that powers every agent:

```bash
npm install -g @anthropic-ai/claude-code
claude
```

Follow the prompts to log in with your Anthropic account (Pro, Max, or API key).

### Install Modastack

Once Claude Code is set up, paste this into your Claude Code session:

```plaintext
Install modastack using https://raw.githubusercontent.com/moda-labs/modastack/main/scripts/install.sh
```

Or install manually:

```bash
uv tool install modastack
```

On macOS you can also use Homebrew:

```bash
brew install moda-labs/modastack/modastack
```

See [scripts/install.sh](scripts/install.sh) for what the installer does.

## Quick Start

```bash
# Start a pre-built agent
modastack start eng-team 

# Or launch a single ad-hoc agent
modastack agents launch --role engineer --task "Fix the login bug"

# Talk to running agents
modastack ask "What's the status of issue #42?"
modastack message "Skip the integration tests, just ship it"
```

## Agent Teams

An agent team is everything an agent needs to operate in a domain: role prompts, workflows, monitors, tools, and extra context/content.

```
agents/eng-team/                   # ← browse the reference team at agents/eng-team/
├── defaults.yaml           # entry role, event sources
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

### Creating Your Own Agents

Run the following prompt in your chat assistant of choice (ChatGPT, Claude, etc) to launch a guided process that will help you generate your own agent team:

```plaintext
Read https://raw.githubusercontent.com/moda-labs/modastack/main/skills/create-agent.md and help me build a modastack agent
```

### Agent Team Registry

Modastack maintains an agent team registry at [`agents/`](agents/). Install teams from our registry, or maintain your own private registry and add it to your local installation of Modastack:

```bash
modastack agents browse                     # see available teams from all registries
modastack agents update eng-team             # install or update
modastack agents add-registry myorg/agents  # add a private registry
```

If you think you have a general-purpose agent you'd like to share with the world, we encourage you to open a PR with it and add it to the global registry!

## Architecture

The topology below is just one example — the [`eng-team`](agents/eng-team/) agent team for software teams. The event server and monitor scheduler, and agent messaging system are infrastructure that every deployment gets.

The topology of agents, including their roles, relationships to each other, and events they are subscribed to is completely defined by the agent team.
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
│ Agent Teamage   │                                      │
│                 │          ┌──────────────────────┐    │
│                 │          │      Monitors        │    │
│                 │          │    (runs locally)    │    │
│                 │          │  pr_conflicts  15m   │    │
│                 │          │  stale_prs     2h    │    │
│                 │          │  deploy_drift  1h    │    │
│                 │          └──────────┬───────────┘    │
│                 ▼                     │                │
│       ┌──────────────────────────┐    │                │
│       │        Director     ◄─────────┘                │
│       │       (persistent)       │                     │
│       └─────┬──────────────┬─────┘                     │
│             │              │                           │
│             ▼              ▼                           │
│    ┌──────────────┐ ┌──────────────┐                   │
│    │ Project Lead │ │ Project Lead │                   │
│    │ (persistent) │ │ (persistent) │                   │
│    │   (repo-a)   │ │   (repo-b)   │                   │
│    └──────┬───────┘ └──────┬───────┘                   │
│           │                │                           │
│      ┌────┴────┐      ┌────┴────┐                      │
│      ▼         ▼      ▼         ▼                      │
│   ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────────┐          │
│   │ task │ │ task │ │ task │ │   workflow   │          │
│   │agent │ │agent │ │agent │ │  (YAML DAG)  │          │
│   └──────┘ └──────┘ └──────┘ │ step → step  │          │
│                              └──────────────┘          │
└────────────────────────────────────────────────────────┘
```

## Event Server

Agents receive real-world events (GitHub, Slack, Linear, custom webhooks) through a centralized event server. Three options:

| | Local | Self-hosted Cloudflare | Hosted (coming soon) |
|---|---|---|---|
| Setup | `modastack event-server start` | Deploy the worker yourself | Sign up at [modastack.dev](https://modastack.dev) |
| Hosting | Runs on your machine | Your Cloudflare account, always on | Managed by Moda Labs |
| Webhook routing | Requires [ngrok](https://ngrok.com/) or similar tunnel | Stable URL, no tunnel | Stable URL, no tunnel |
| GitHub/Slack apps | Create your own | Create your own | Install our pre-built apps |
| Storage | In-memory | Durable Objects (persistent) | Durable Objects (persistent) |
| Best for | Local dev, quick experimentation | Self-hosted production | Fastest path to production |

## Monitors

Not every event comes from a webhook. Monitors are scheduled checks that detect conditions the outside world doesn't notify you about — merge conflicts, stale PRs, deploy drift, SLA breaches — and inject them into the same event bus as webhooks.

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

A monitor with a `check:` field runs a deterministic native function — fast, deduplicated, no LLM needed. A monitor without one spawns a short-lived agent that evaluates the description and posts an event only if it finds something. Either way, the resulting event is indistinguishable from a webhook — subscribing agents handle it the same way.

```bash
modastack monitors list              # see all active monitors
modastack monitors add stale-deploys --interval 1h --description "Deploys older than 24h"
modastack monitors pause pr_conflicts
```

## Workflows

Knowledge work often requires multi-step workflows where LLM variation or skipping steps is not acceptable.  For example, in software development, you want to require that all changes go through an automated code review and that CI passes before opening PRs.

Workflows are YAML DAGs that force agents to follow pre-built recipes in order.  Each workflow step can also route to a different role (e.g. `engineer` vs `security-reviewer`) to provide better context for each step.

Here is an example YAML:

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

You normally don't have to build these YAML files by hand — the [create-agent](skills/create-agent.md) skill will guide you through the process.

## CLI

```bash
# Agents
modastack agents launch -w <workflow> --role <role> --task "context"
modastack agents list
modastack agents show <id>
modastack agents cancel <id>

# Communication
modastack ask "question"          # blocks until response
modastack message "update"        # fire-and-forget

# Observability
modastack status                  # active agents
modastack events                  # recent events and decisions
modastack transcript show <sess>  # session transcript
modastack doctor                  # system health check

# Workflows & monitors
modastack workflows list
modastack monitors list
modastack roles list
```

## Configuration

See the setup guides for [Slack](skills/slack-setup.md) and [Linear](skills/linear-setup.md).

Machine-wide credentials in `~/.modastack/config.yaml`:

```yaml
slack:
  bot_token: xoxb-...
event_server:
  url: https://modastack-events.example.workers.dev
linear:
  api_key: lin_api_...
```

Per-project overrides in `.modastack/` — custom roles, workflows, monitors, and tools that take priority over the agent team defaults.

## Development

```bash
git clone https://github.com/moda-labs/modastack.git
cd modastack
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/
```

## License

MIT
