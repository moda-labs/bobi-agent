# Bobi

[![CI](https://github.com/moda-labs/bobi-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/moda-labs/bobi-agent/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bobi)](https://pypi.org/project/bobi/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Bobi is a lightweight library for building and deploying proactive agents** -
agents that don't just do work when you talk to them, but respond to real-world
events like ticket updates, incoming emails, GitHub PRs, Slack messages, or any
webhook, acting on their own when something changes. Agents coordinate, delegate,
and spin up new sub-agents on their own whenever the work calls for it.

They also get more useful the more you use them: a built-in sleep cycle distills each
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
- Node.js 20 or newer for the embedded local event server.
  For current releases, install Node separately and ensure `node` is on `PATH`, including when installing Bobi with Homebrew.
  A companion Homebrew formula update will automate this prerequisite once it ships.
- For cloud deployment (optional): somewhere to run a container - a VM with
  Docker, a Kubernetes cluster, or any container host. See
  [Cloud Deployment](#cloud-deployment).

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

Until the companion Homebrew formula update ships, Homebrew users must install Node.js 20 or newer separately.
For every install method, verify that the supported runtime is available:

```bash
node --version
```

Then install with uv:

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
It is preconfigured for GitHub and Slack, so `bobi agents install` will ask for
the required `GH_TOKEN`, `SLACK_BOT_TOKEN`, and `SLACK_SIGNING_SECRET` secrets
(`SLACK_CHANNELS` is optional). Authenticate the GitHub CLI or provide a token
with repo access, and see the [Slack setup guide](skills/slack-setup.md) for the
bot token and signing secret.

```bash
# Install the ready-to-use engineering agent and start it
bobi agents install eng-team --name eng-team
bobi agent eng-team start

# Ask the manager a question (blocks until it responds)
bobi agent eng-team ask "What can I help with right now?"

# Hand it a one-off task
bobi agent eng-team subagents launch -w adhoc --role engineer --task "Fix the login bug"
```

Prefer a visual home for all of this? The unified web app opens as a
dashboard of every agent on your machine - create a team in a guided
conversation, launch it, and chat with its agents, all in the browser:

```bash
bobi app start        # runs in the background; stop/restart/status manage it
```

Prefer to design your own agent from scratch in a standalone wizard? Run:

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

To run a team on **local or self-hosted models**, add `base_url` - the engine
you picked with `kind` then dials that gateway instead of its native vendor
endpoint. The claude engine works with any Anthropic-compatible endpoint
(LiteLLM, Ollama's Anthropic-compat API):

```yaml
brain:
  kind: claude
  base_url: ${LLM_GATEWAY_URL}   # the /v1/messages-compatible endpoint
  model: qwen3:14b               # gateway-native model id
```

If the gateway needs auth, put `ANTHROPIC_AUTH_TOKEN` in the team's runtime
`.env`. See `skills/create-agent.md` for the details.

For an **OpenAI-compatible** gateway, use the codex engine instead:

```yaml
brain:
  kind: codex
  base_url: ${LLM_GATEWAY_URL}   # OpenAI-compatible /v1 endpoint
  model: gpt-5.5                # gateway-native model id
  wire_api: responses           # optional: responses (default)
```

If the gateway needs auth, put `BOBI_GATEWAY_API_KEY` in the team's runtime
`.env`. Bobi references that dedicated key from Codex and never sends an ambient
real `OPENAI_API_KEY` to the gateway.

Current Codex builds use the Responses API for custom providers.
Bobi keeps `wire_api: chat` as a pass-through for deliberately pinned older
Codex builds, but `bobi agent <name> doctor` warns on it.
For a chat-completions-only OpenAI-compatible gateway, front it with LiteLLM so
Codex can call `/v1/responses`, or use `kind: claude` + `base_url` when the
backend exposes an Anthropic-compatible endpoint.

(The pre-0.46 spellings `kind: gateway` and `kind: gateway-openai` remain
accepted aliases for exactly these two configurations.)

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
GitHub, Linear), use the **Webhook ingress** row in `bobi setup`'s Connections
card. It can keep the team local-only, verify a quick tunnel such as
cloudflared/ngrok to `localhost:8080`, or save the URL of a durable HTTPS
event server you host yourself (`BOBI_EVENT_SERVER`; see
[docs/SELF_HOSTED_EVENT_SERVER.md](docs/SELF_HOSTED_EVENT_SERVER.md)).
Setup-authored `agent.yaml` files reference that optional
environment variable so verified public ingress is used when present, while an
unset value leaves the automatic local event server path unchanged.

If you already run the local event server behind a Cloudflare tunnel, use the
quick tunnel option; the wizard does not replace that topology, it validates and
persists the tunnel URL for webhook-backed services.

To run that ingress yourself - a tunnel in front of the embedded server, a
standalone event server on a box you manage, or the Cloudflare Worker variant
when you need registrations and replay to survive a restart - follow
[docs/SELF_HOSTED_EVENT_SERVER.md](docs/SELF_HOSTED_EVENT_SERVER.md).

### Talk to your agent from Slack (optional)

By default you talk to an agent from the terminal (`bobi agent <name> ask`). To
message it from Slack instead - and have it reply and react there - generate a
Slack app and point it at your agent:

```bash
bobi create-slack-bot --app-name "Bobi"
```

If you built the team with `bobi setup` and picked Slack as its chat, the
setup completion screen walks you through this - scopes on-screen, a dedicated
channel saved for the team, and a test message to prove the wiring.

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
  session; choose per agent with `brain: {kind: claude|codex}`, or point a
  team at local models by adding a gateway endpoint with
  `brain: {kind: claude, base_url: ...}`.
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
bobi agent <name> events publish alert/firing --json '{"title":"x"}'
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

Bobi publishes a reference container image so you can run an always-on agent on
whatever hosts containers already - a VM, Docker on a box, Kubernetes, or your
own orchestrator. Nothing about the runtime is tied to a particular cloud.

**The image.** `ghcr.io/moda-labs/bobi:<version>` is public and multi-arch. It
carries the framework, the pinned agent runtimes, and the supervisor. Run it
with `--init` (the container expects a real PID 1) and point it at your event
server:

```bash
docker run --init -v bobi-eng:/data \
  -e BOBI_INSTANCE=eng \
  -e BOBI_TEAM=eng-team \
  -e BOBI_AUTH=api_key \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e BOBI_EVENT_SERVER=https://events.example.com \
  ghcr.io/moda-labs/bobi:<version>
```

The volume at `/data` is the agent's identity and its only durable state -
reuse it and the agent resumes, discard it and you get a fresh agent.

`docs/REFERENCE_IMAGE.md` documents what the image contains, the runtime env
contract, and the `TEAM_DEPS` hook for baking your team's own dependencies into
a derived image. `docs/SELF_HOSTED_EVENT_SERVER.md` covers standing up the
event server the instance connects out to.

**Managed fleets.** Provisioning, GitOps reconciliation of a whole fleet, and
the hosted admin control plane are part of Moda's managed/enterprise offering
rather than this package - reach out via an issue or zach@modalabs.ai. The
mechanics below describe how a deployed instance behaves either way:

- **Immutable image.** The framework and pinned agent runtimes are baked into one
  image - the image is the unit of update. The embedding model downloads on first
  KB use into the durable volume cache.
- **Durable state.** Credentials and session transcripts live on a mounted volume,
  so they survive image updates and the agent resumes where it left off.
- **Self-managing.** Your orchestrator's restart policy plus the built-in
  supervisor sidecar (`bobi agent <name> supervise`) keep the agent alive
  without babysitting. The supervisor's wire contract is documented in
  `docs/ADMIN_PROTOCOL.md`, so an external control plane can monitor and drive
  instances over the event bus.
- **Bake your own dependencies.** The image exposes a `TEAM_DEPS` hook: derive
  from the published base, layer in whatever your team's tools need, and the
  hook verifies its own `requires` gate at build time - useful for custom
  runtimes, enterprise registries, or fast CI-built images.

An instance needs **no public ingress**. It holds an outbound WebSocket to the
event server and acts on events as they arrive, which is what makes it
deployable behind a firewall, in a private subnet, or on a cluster with no
inbound path. It works from the published image and the installed CLIs alone -
no framework checkout.

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
| Understand how Bobi works | [docs/OVERVIEW.md](docs/OVERVIEW.md) - the core concepts in 5 minutes |
| Go from zero to a deployed agent | [docs/QUICKSTART.md](docs/QUICKSTART.md) - step-by-step quickstart |
| Run and operate Bobi | [skills/bobi.md](skills/bobi.md) - full CLI reference |
| Build your own agent | [skills/create-agent.md](skills/create-agent.md) · [docs/BUILDING_AGENT_TEAMS.md](docs/BUILDING_AGENT_TEAMS.md) |
| Understand the event bus | [docs/EVENT_SERVER.md](docs/EVENT_SERVER.md) — architecture, topics, security |
| Receive webhooks on your own infrastructure | [docs/SELF_HOSTED_EVENT_SERVER.md](docs/SELF_HOSTED_EVENT_SERVER.md) — tunnel or standalone server |
| Understand the security model | [docs/SECURITY.md](docs/SECURITY.md) — trust, credentials, prompt-injection |
| Connect Slack / Linear | [skills/slack-setup.md](skills/slack-setup.md) · [skills/linear-setup.md](skills/linear-setup.md) |
| Run Bobi in a container | [docs/REFERENCE_IMAGE.md](docs/REFERENCE_IMAGE.md) — `ghcr.io/moda-labs/bobi`, the published image |
| Control a deployment remotely | [docs/ADMIN_PROTOCOL.md](docs/ADMIN_PROTOCOL.md) — the supervisor's wire contract |

## Development

```bash
git clone https://github.com/moda-labs/bobi-agent.git
cd bobi-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,kb]"
pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q
```

That install is intended to match the CI `Unit tests` job in
`.github/workflows/ci.yml`. It includes the normal test tools plus
knowledge-base dependencies such as `fastembed`, `sqlite-vec`, and their numeric
stack, including `numpy`, because full unit-test collection imports KB modules.
Use the narrower `.[dev]` install for e2e-only work where the KB test surface is
not being collected.

## License

[Apache License 2.0](LICENSE).
