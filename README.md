# Bobi

[![CI](https://github.com/moda-labs/bobi-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/moda-labs/bobi-agent/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bobi)](https://pypi.org/project/bobi/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Bobi is a lightweight library for building and running purposeful multi-agent
teams** — proactive teams that don't just answer when you message them, but
subscribe to the outside world (GitHub PRs, Slack messages, ticket updates,
incoming email, any webhook) and act on their own when something changes.

Every agent runs on [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
or [OpenAI Codex](https://developers.openai.com/codex/cli/), so a whole team runs
on the flat-rate subscription you already pay for — no per-token API bills (API
keys work too).

## What you can build

You define what a team is for; the framework has no opinion. A few shapes it
takes:

- **Engineering org** — triage issues, open PRs through a required review-and-CI
  workflow, and watch for merge conflicts and stale PRs across repos. Bundled as
  the [`eng-team`](agents/eng-team/) reference team.
- **Personal assistant** — watch your inbox and calendar, draft replies, and
  surface only what needs a decision.
- **Customer support** — triage tickets, answer from a knowledge base, and
  escalate what it can't close.
- **Sales automation** — enrich inbound leads, keep the CRM current, and follow up
  on schedule.

## Installation

### What you need

- Python 3.11+, Git, Node.js + npm
- [`uv`](https://astral.sh/uv/) (or `pipx`)

You don't clone this repo to run Bobi — it's a published package. Install the CLI
and go.

### 1) Set up an agent runtime

Bobi runs each agent on **Claude Code** (default) or **OpenAI Codex**. For Claude
Code:

```bash
npm install -g @anthropic-ai/claude-code
claude
```

Log in with your Anthropic account (Pro, Max, or API key). To use Codex, install
and authenticate the `codex` CLI and set `brain: {kind: codex}` in your team's
`agent.yaml`.

### 2) Install Bobi

```bash
uv tool install bobi          # or: brew install moda-labs/bobi-agent/bobi
```

Or, from a Claude Code session:

```plaintext
Install bobi using https://raw.githubusercontent.com/moda-labs/bobi-agent/main/scripts/install.sh
```

See [scripts/install.sh](scripts/install.sh) for what the installer does.

## Quick Start

`eng-team` is the reference team that ships with Bobi — install it to see a full
team, or build your own for any domain (below).

```bash
# Install the reference engineering team and start it
bobi agents install eng-team --name eng-team
bobi agent eng-team start

# Ask the manager a question (blocks until it responds)
bobi agent eng-team ask "What can I help with right now?"

# Hand it a one-off task
bobi agent eng-team subagents launch --role engineer --task "Fix the login bug"
```

Prefer to design your own team from scratch? Run the interactive wizard, or have
your chat assistant walk you through it:

```bash
bobi setup            # go from an idea to a runnable team, interactively
```

```plaintext
Read https://raw.githubusercontent.com/moda-labs/bobi-agent/main/skills/create-agent.md and help me build a bobi agent
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

## Under the hood

- **It's a CLI all the way down.** `bobi` launches agents from your terminal — and
  each agent launches *its own* sub-agents through the same CLI. A manager spawns
  project leads, a project lead spawns task agents, every level using the same
  commands. That recursion is the entire execution model.
- **No topology opinions.** Bobi ships no org chart. Roles, relationships, and
  who-subscribes-to-what are defined entirely by the agent team.
- **Built-in event server.** A topic-based pub/sub bus (run locally or on your own
  Cloudflare account) ingests webhooks from GitHub, Slack, Linear, and anything
  else, then fans them out to the agents subscribed to each topic.
- **Runtime-agnostic brains.** Each agent is a Claude Code or OpenAI Codex
  session; choose per team with `brain: {kind: claude|codex}`.
- **Deterministic workflows.** YAML DAGs force multi-step work through a fixed
  recipe with role routing — code review before merge, CI before PRs — instead of
  trusting the model to remember.
- **Monitors.** Scheduled checks detect conditions no webhook fires for (merge
  conflicts, stale PRs, deploy drift) and inject them onto the same bus.
- **Observability.** Full session transcripts, an event-and-decision log, cost
  accounting, and a `doctor` health check.

```bash
# Launch and operate agents
bobi agent <name> start
bobi agent <name> subagents launch --role <role> --task "context"

# Talk to running agents
bobi agent <name> ask "question"          # blocks until response
bobi agent <name> message "update"        # fire-and-forget

# Observe
bobi agent <name> status                  # active agents
bobi agent <name> events                  # recent events and decisions
bobi agent <name> transcript show <sess>  # session transcript
bobi agent <name> doctor                  # system health check
```

Full command reference: [skills/bobi.md](skills/bobi.md).

## Mental model

Bobi has a small surface area to learn:

- **Agent teams are installable packages.** A team is a portable bundle of roles,
  workflows, monitors, and tool guides. You install one by name, path, URL, or
  from a registry — like installing a dependency — and get a working agent for a
  domain.
- **Source is editable; the runtime image is frozen.** You edit a team's source
  files, then reinstall to regenerate the frozen package image the runtime
  actually reads. Reinstalling never clobbers your runtime state or workspace.
- **Everything lives under one home directory.** `$BOBI_HOME` (default `~/.bobi`)
  holds every named agent: editable source in `src/`, the installed package in
  `run/package/`, mutable state in `run/state/`, your files in `run/workspace/`,
  and credentials in `run/.env`.

## Deployment

A proactive agent is only as available as the machine it runs on. Locally, your
team works when your laptop is open; in the cloud, it works **24/7** — reacting to
a PR at 2am or a support ticket on the weekend without you in the loop. That
always-on shift is the real productivity unlock, and Bobi makes it one command.

`bobi deploy` packages your agent into an immutable container image and runs it as
an always-on instance on a cloud VM — no Dockerfile to write, no server to
configure.

```bash
bobi deploy eng-team
```

The command provisions the machine, ships the image, and starts the agent; run it
again and it updates the instance in place. Behind it:

- **Immutable image.** The framework, a pinned agent runtime, and the embedding
  model are baked into one image — the image is the unit of update.
- **Durable state.** Credentials and session transcripts live on a mounted volume,
  so they survive image updates and the agent resumes where it left off.
- **Self-managing.** A machine restart policy plus a supervision watchdog keep the
  agent alive without babysitting.
- **GitOps for fleets.** `bobi deploy-init` scaffolds a GitHub Action that
  reconciles `deployments/*.yaml` against running instances on every release — git
  is the desired state, `bobi deploy` closes the gap, one instance at a time.

Currently targets [Fly](https://fly.io) Machines, and works from the installed CLI
alone — no framework checkout. Full runbook: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md);
image details: [docs/CONTAINER.md](docs/CONTAINER.md).

## Security

Bobi's event server is a direct front door to a model's prompt: every event it
delivers becomes input an agent acts on. So neither subscribing to a topic nor
publishing onto one can be open. Bobi gates both with a signed **trust-bubble**
model:

- **Bubble membership (HMAC).** Each named agent mints a *trust bubble* on first
  start; every deployment of that agent joins it with the bubble's key. Each
  publish and join is signed with HMAC-SHA256 over a canonical
  `(timestamp, nonce, method, path, body)` string and verified server-side within
  a ±5-minute replay window. Only bubble members can put events on the bus.
- **Proof of access for external topics.** Before a bubble may subscribe to a
  global webhook topic (`github:owner/repo`, `linear:team`, `slack:workspace`),
  the event server verifies an upstream credential *once* — a GitHub repo read, a
  Linear team read, a Slack workspace registration — and stores only the resulting
  grant, never the credential. The grant is the source of truth at delivery, so a
  bubble can never receive another org's events without proving it controls that
  resource.
- **Protect the bubble key.** The bubble's private key lives on the machine
  running the agent and grants the ability to act as that agent on the bus. Treat
  it like any other credential — it sits under `run/`, and you should never commit
  it or copy it off the host.
- **Local by default.** The local event server runs on your machine; nothing
  leaves it until you connect a remote event server or messaging integration.
  Installing a team runs its prompts against your credentials, so review a team
  before installing it — the same way you'd review a dependency.

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
