# Bobi

[![CI](https://github.com/moda-labs/bobi-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/moda-labs/bobi-agent/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bobi)](https://pypi.org/project/bobi/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Bobi is a lightweight library for building and deploying proactive agents** -
agents that don't just do work when you talk to them, but respond to real-world
events like ticket updates, incoming emails, GitHub PRs, Slack messages, or any
webhook, acting on their own when something changes. Agents coordinate, delegate,
and spin up new sub-agents on their own whenever the work calls for it.

They also get more useful the more you use them: a closed-loop memory system distills each
session into durable facts and preferences that carry into future runs, so an
agent learns how you like to work and adapts its behavior over time. You extend an
agent just by telling it what you want - hand it new tasks or responsibilities in
plain language, or add new tools, roles, and workflows as your needs grow.

Every agent runs on [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
or [OpenAI Codex](https://developers.openai.com/codex/cli/), so it all runs on the
flat-rate subscription you already pay for - no per-token API bills (API keys work
too).

## What you can build

You define what an agent is for; the framework has no opinion. Here are a few examples of 
agents you can build:

- **Agentic Engineering Team** - triage issues, open PRs through a required review-and-CI
  workflow, and watch for merge conflicts and stale PRs across repos. Ships as the
  ready-to-use [`eng-team`](agents/eng-team/) agent: import it and customize it with
  your own engineering methodologies and practices.
- **Personal assistant** - watch your inbox and calendar, draft replies, and
  surface only what needs a decision. Ships as the ready-to-use
  [`personal-assistant`](agents/personal-assistant/) agent, connecting to Gmail,
  Google Calendar, and Google Tasks via Venn: customize it for your own routines.
- **Customer support** - triage tickets, answer from a knowledge base, and
  escalate what it can't close.
- **Sales automation** - enrich inbound leads, keep the CRM current, and follow up
  on schedule.

## Installation

### What you need

- An agent runtime - [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
  or [OpenAI Codex](https://developers.openai.com/codex/cli/), installed from
  Homebrew or npm. Bobi runs each agent on one of these (steps below).
- The `bobi` CLI - installed from [Homebrew](https://brew.sh/) or any Python
  package manager such as [`uv`](https://astral.sh/uv/) or
  [`pipx`](https://pipx.pypa.io/).
- For cloud deployment (optional): a [Fly.io](https://fly.io) account and a Fly
  API token. Only needed if you run `bobi deploy` - see [Cloud Deployment](#cloud-deployment).

You don't clone this repo to run Bobi - it's a published package. Install the CLI
and go.

### 1) Set up an agent runtime

Bobi runs each agent on **Claude Code** (default) or **OpenAI Codex**. You need
at least one installed and authenticated - skip this if you already have one set
up.

**Claude Code** - if you don't have it yet:

```bash
brew install --cask claude-code   # or: npm install -g @anthropic-ai/claude-code
claude
```

Log in with your Anthropic account (Pro, Max, or API key). See the
[Claude Code docs](https://docs.anthropic.com/en/docs/claude-code) for details.

**OpenAI Codex** - if you don't have it yet:

```bash
brew install --cask codex          # or: npm install -g @openai/codex
codex
```

Log in with your ChatGPT plan or an OpenAI API key. See the
[Codex CLI docs](https://developers.openai.com/codex/cli/) for details. You'll
select Codex per agent with `brain: {kind: codex}` once you create one (below).

### 2) Install Bobi

With [Homebrew](https://brew.sh/):

```bash
brew install moda-labs/bobi-agent/bobi
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install bobi
```

Or, from a Claude Code session:

```plaintext
Install bobi using https://raw.githubusercontent.com/moda-labs/bobi-agent/main/scripts/install.sh
```

See [scripts/install.sh](scripts/install.sh) for what the installer does.

## Quick Start

`eng-team` is the ready-to-use engineering agent that ships with Bobi - install it
to get a working team you can customize, or build your own for any domain (below).

```bash
# Install the ready-to-use engineering agent and start it
bobi agents install eng-team --name eng-team
bobi agent eng-team start

# Ask the manager a question (blocks until it responds)
bobi agent eng-team ask "What can I help with right now?"

# Hand it a one-off task
bobi agent eng-team subagents launch --role engineer --task "Fix the login bug"
```

Prefer to design your own agent from scratch? Run the interactive wizard:

```bash
bobi setup            # go from an idea to a runnable agent, interactively
```

Or use your coding assistant to help you build one with the `create-agent` skill -
paste this into your Claude Code or Codex session:

```plaintext
Read https://raw.githubusercontent.com/moda-labs/bobi-agent/main/skills/create-agent.md and help me build a bobi agent
```

### Choose the runtime (optional)

Every agent runs on **Claude Code by default** - you don't need to configure
anything. To run an agent on **OpenAI Codex** instead, add a `brain` block near
the top of its `agent.yaml`:

```yaml
brain:
  kind: codex          # omit the block entirely for Claude Code (the default)
  model: gpt-5-codex   # optional: provider-specific model or alias
```

Make sure the matching CLI is installed and authenticated (see
[Set up an agent runtime](#1-set-up-an-agent-runtime) above).
For Claude-backed teams, `model` can be an alias such as `haiku`, `sonnet`, or
`opus`, or a full Claude model ID.

Workflow steps can override the team default for that step:

```yaml
steps:
  - name: discover
    agent: prospect-targeter
    model: haiku
    prompt: "Find companies matching the wedge..."
```

Don't want to edit YAML by hand? Paste this into your Claude Code or Codex
session:

```plaintext
In my bobi agent's agent.yaml, set the brain to Codex by adding:
  brain:
    kind: codex
(To switch back to Claude Code, remove the brain block - Claude Code is the default.)
```

### Credentials (optional)

Out of the box an agent runs locally and handles whatever you hand it. To let it
act on outside services - opening a GitHub PR, updating a Linear ticket, posting
to Slack - it needs credentials for them. `bobi agents install` prompts for any
secrets the agent's `agent.yaml` references and writes them to `run/.env` (never
commit this file); you can also supply them as environment variables.

You don't run the event server yourself - `bobi agent <name> start` launches a
local one automatically. To receive webhooks from the public internet (Slack,
GitHub, Linear), point the agent at a deployed event server: the shared Bobi
cloud Worker by default, or your own Cloudflare Worker.

### Talk to your agent from Slack (optional)

By default you talk to an agent from the terminal (`bobi agent <name> ask`). To
message it from Slack instead - and have it reply and react there - generate a
Slack app and point it at your agent:

```bash
bobi create-slack-bot --app-name "Bobi"
```

Full walkthrough: **[Slack setup](skills/slack-setup.md)**.

## Under the hood

- **It's a CLI all the way down.** `bobi` launches agents from your terminal - and
  each agent launches *its own* sub-agents through the same CLI. A director can
  spawn async workers, and those workers can launch bounded helpers when a
  workflow calls for it. That recursion is the execution model.
- **No topology opinions.** Bobi ships no org chart. Roles, relationships, and
  who-subscribes-to-what are defined entirely by the agent.
- **Built-in event server.** A topic-based pub/sub bus (run locally or on your own
  Cloudflare account) ingests webhooks from GitHub, Slack, Linear, and anything
  else, then fans them out to the agents subscribed to each topic.
- **Runtime-agnostic brains.** Each agent is a Claude Code or OpenAI Codex
  session; choose per agent with `brain: {kind: claude|codex}`.
- **Deterministic workflows.** YAML DAGs force multi-step work through a fixed
  recipe with role routing - code review before merge, CI before PRs - instead of
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

- **Agents are installable packages.** An agent is a portable bundle of roles,
  workflows, monitors, and tool guides. You install one by name, path, URL, or
  from a registry - like installing a dependency - and get a working agent for a
  domain.
- **Source is editable; the runtime image is frozen.** You edit an agent's source
  files, then reinstall to regenerate the frozen package image the runtime
  actually reads. Reinstalling never clobbers your runtime state or workspace.
- **Everything lives under one home directory.** `$BOBI_HOME` (default `~/.bobi`)
  holds every named agent: editable source in `src/`, the installed package in
  `run/package/`, mutable state in `run/state/`, your files in `run/workspace/`,
  and credentials in `run/.env`.

## Cloud Deployment

A proactive agent is only as available as the machine it runs on. Locally, your
agent works when your laptop is open; in the cloud, it works **24/7** - reacting to
a PR at 2am or a support ticket on the weekend without you in the loop. That
always-on shift is the real productivity unlock, and Bobi makes it one command.

`bobi deploy` packages your agent into an immutable container image and runs it as
an always-on instance on a cloud VM - no Dockerfile to write, no server to
configure.

**Prerequisites.** Cloud deployment targets [Fly](https://fly.io) Machines, so you
need a Fly.io account and a Fly API token (`flyctl` authenticated via
`fly auth login`). First time on Fly? `bobi deploy` preflights your setup and
prints exactly what to do - install `flyctl`, sign up or log in, and clear the
one-time new-org unlock.

```bash
bobi deploy eng-team
```

The command provisions the machine, ships the image, and starts the agent; run it
again and it updates the instance in place. Behind it:

- **Immutable image.** The framework and pinned agent runtimes are baked into one
  image - the image is the unit of update. The embedding model downloads on first
  KB use into the durable volume cache.
- **Durable state.** Credentials and session transcripts live on a mounted volume,
  so they survive image updates and the agent resumes where it left off.
- **Self-managing.** A machine restart policy plus a supervision watchdog keep the
  agent alive without babysitting.
- **GitOps for fleets.** `bobi deploy-init` scaffolds a GitHub Action that
  reconciles `deployments/*.yaml` against running instances on every release - git
  is the desired state, `bobi deploy` closes the gap, one instance at a time.
- **Bring your own image.** Want to fully pre-bake the agent runtime? Point a
  deployment at a prebuilt container with `image: <ref>` in
  `deployments/<name>.yaml` and `bobi deploy` ships it by reference, skipping the
  build entirely - useful for custom runtimes, enterprise registries, or fast
  CI-built images.

Bobi runs agents on Fly Machines for their fast VM wake-up and scale-to-zero
model, which pair naturally with the external event server. Today each agent runs
as a single always-on Machine with no
public ingress: it holds an outbound WebSocket to the event server and acts on
events as they arrive, kept alive by Fly's restart policy plus Bobi's supervision
watchdog. Because Fly Machines are Firecracker microVMs that suspend and resume in
well under a second, near-term work will let an idle agent scale to zero and wake
on the next event - making it very affordable to run a fleet of always-available
agents without paying for idle VMs. It works from the installed CLI alone - no
framework
checkout. Full runbook (image, Fly, and GitOps):
[docs/CONTAINERIZED_DEPLOYMENT.md](docs/CONTAINERIZED_DEPLOYMENT.md).

## Security

Every event the bus delivers becomes input an agent acts on, and a team is code
that runs with your credentials, so Bobi gates both ends. A signed **trust-bubble**
(HMAC) controls who can publish or subscribe; **proof-of-access** grants control
which external webhook topics you can receive, verified against an upstream
credential that is never stored. The local event server is loopback-only - nothing
leaves your machine until you connect a remote one - and installing a team runs its
code against your credentials, so review one before installing it, like a
dependency.

Full model: **[docs/SECURITY.md](docs/SECURITY.md)** - trust boundary, credentials,
the prompt-injection surface, and trusted team code. Event-bus internals:
**[docs/EVENT_SERVER.md](docs/EVENT_SERVER.md)**.

## Documentation

| Goal | Read |
|---|---|
| Run and operate Bobi | [skills/bobi.md](skills/bobi.md) - full CLI reference |
| Build your own agent | [skills/create-agent.md](skills/create-agent.md) · [docs/BUILDING_AGENT_TEAMS.md](docs/BUILDING_AGENT_TEAMS.md) |
| Understand the event bus | [docs/EVENT_SERVER.md](docs/EVENT_SERVER.md) — architecture, topics, security |
| Understand the security model | [docs/SECURITY.md](docs/SECURITY.md) — trust, credentials, prompt-injection |
| Connect Slack / Linear | [skills/slack-setup.md](skills/slack-setup.md) · [skills/linear-setup.md](skills/linear-setup.md) |
| Deploy to production | [docs/CONTAINERIZED_DEPLOYMENT.md](docs/CONTAINERIZED_DEPLOYMENT.md) |

## Development

```bash
git clone https://github.com/moda-labs/bobi-agent.git
cd bobi-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/
```

## License

[Apache License 2.0](LICENSE).
