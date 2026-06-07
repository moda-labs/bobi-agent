# Event-Driven Agents: The Missing Layer

*June 2026*

Every agent framework today solves the same problem: a human types
something, an agent reasons about it, and something happens. Chat in,
action out. The entire industry — LangGraph, CrewAI, OpenAI Agents SDK,
Anthropic Agent SDK — is built on this request-response loop.

But the most valuable agents aren't the ones that wait for humans to
ask. They're the ones that react when something happens in the real
world: a PR gets opened, a deploy fails, a customer files a ticket, an
alert fires, a deadline passes. Agents that watch, reason, and act
without being prompted.

No framework makes this easy today. modastack is an open-source agent
framework that does. Agents subscribe to real-world events, respond
with autonomous reasoning — and remain interactive at all times. You
can spin them up in any topology, wire them together, and talk to any
of them whenever you want.

---

## The problem

### Knowledge work is already event-driven

Most knowledge work isn't planned in advance — it's reactive. A
ticket gets updated, you respond. A Slack message comes in, you
answer. A support request arrives, you triage. A document changes in
Confluence, you review. An alert fires, you investigate. A PR gets
opened, you review the code.

The tools people use every day — Jira, Linear, Slack, Gmail, Notion,
PagerDuty, Zendesk, Salesforce — are event-driven systems. They
generate a continuous stream of things that need attention, and
knowledge workers spend their days responding to that stream.

Anyone who uses an AI coding agent has felt this: someone asks a
question in Slack or a PR comment, and you think — the agent already
knows the answer to this, it's been working in this codebase all day.
But the agent can't hear the question. So you context-switch, look it
up, type the answer, and spend five minutes getting back to where you
were. The agent is smart enough. It just can't hear.

AI agents should work the same way people do — reacting to events as
they arrive. But they don't.

### Agents are stuck in chat mode

The dominant agent architecture in 2026 looks like this:

```
Human types → Agent reasons → Agent acts → Human reads result
```

Every major framework assumes this lifecycle. LangGraph builds
directed graphs that execute when invoked. CrewAI assembles crews
that run when called. OpenAI's Agents SDK processes a conversation
turn. Even multi-agent systems — where agents delegate to each other
— start with a human request and end with a human-facing response.

This works for copilots and assistants. It doesn't work for agents
that need to operate continuously in response to the world changing
around them.

### The trigger layer and the reasoning layer are separate products

Developers who want event-driven agents today face a two-product
problem:

1. **Workflow automation platforms** (Zapier, Make, n8n) have 8,000+
   triggers but shallow AI — prompt-and-tool-call, no persistent
   reasoning, no multi-step planning.

2. **Agent frameworks** (LangGraph, CrewAI, AG2) have deep reasoning
   but no trigger infrastructure — you wire up your own webhook
   handlers, load state, resume the graph yourself.

Composio tries to bridge the gap by bolting triggers onto existing
frameworks, but it's a third dependency, not a unified system. Mastra
has `afterEvent`/`resumeWithEvent` primitives, but no persistent
daemon or event bus. Cloudflare's Agents SDK has the infrastructure
(Durable Objects, WebSocket, cron) but no agent orchestration layer.

The result: building an agent that reacts to a GitHub webhook today
means stitching together a webhook handler, a state store, a session
manager, and an agent framework — then figuring out how they talk to
each other.

### The event-driven agents that exist are locked to one domain

Event-driven agent systems do exist — but they're all hardcoded to
software development. Factory.ai reacts to GitHub issues and produces
PRs. OpenAI's Symphony polls Linear and dispatches coding agents.
Cursor Automations trigger on PRs and CI failures. These are
event-driven SDLC agents, not event-driven agent frameworks.

You can't take Factory.ai and point it at PagerDuty alerts, or
customer support tickets, or content review queues. The event sources,
the workflows, the agent roles — they're all baked into the product.
If your use case isn't "issue in, PR out," you start from scratch.

The gap isn't event-driven agents. It's a general-purpose,
domain-agnostic framework for building them.

### No framework treats agents as composable event-driven nodes

In distributed systems, event-driven architectures decouple producers
from consumers. Services emit events, other services subscribe, and
the system composes without tight coupling. This is a solved pattern
— pub/sub, event sourcing, choreography — that has powered
production systems for decades.

Agent frameworks haven't adopted it. Multi-agent communication in
CrewAI, AG2, and LangGraph is internal: agents pass messages within a
single process or session. The topology is hardcoded —
supervisor/worker, sequential chain, round-robin. There's no way to
spin up agents across machines that communicate through an event bus,
or to add a new agent later that subscribes to the same events
without modifying existing code.

Google's A2A protocol and MCP are steps toward interoperability, but
neither provides an event bus. A2A defines task lifecycle (submitted →
working → completed) and agent discovery, not pub/sub. MCP defines
tools, resources, and sampling — it's request-response by design.

---

## What's out there

### Where event-driven agents are showing up

The demand is real. Vendors across categories are adding event-driven
agent capabilities — but each is constrained in a different way:

**Incident response.** Datadog's Bits AI investigates alerts the
moment they fire. AWS shipped a DevOps Agent (GA April 2026) triggered
by CloudWatch alarms and PagerDuty alerts. PagerDuty embedded AI
agents directly in Slack. These are powerful — but closed, vendor-
specific, and limited to their own alert streams.

**Code review and CI.** CodeRabbit ships "Triggers" that activate an
agent when a matching event lands. Cursor launched "Automations" —
always-on agents triggered by PRs and Slack messages. Domain-locked
to SDLC.

**Workflow automation.** n8n 2.0 added 70+ AI nodes. Zapier launched
Agents that plan across 8,000+ integrations. Great triggers, shallow
reasoning — prompt-and-tool-call, not persistent multi-step agents.

### Survey of existing approaches

| Category | Event-driven? | Domain-agnostic? | Interactive? | Agent networks? | Open source? |
|---|---|---|---|---|---|
| **SDLC agents** (Factory, Symphony, Devin) | Yes — GitHub/Linear | No — coding only | Limited | No | Partial |
| **Agent harnesses** (OpenClaw, Hermes) | Cron + webhooks | Yes | Yes | No — point-to-point | Yes |
| **Agent frameworks** (LangGraph, CrewAI, AG2) | No — request-response | Yes | Per-request | Internal only | Yes |
| **Workflow platforms** (Zapier, Make, n8n) | Yes — 1000s of triggers | Yes | No | No | Partial |
| **Agent infrastructure** (Inngest, Temporal) | Yes | Yes | No | No — BYO agent | Partial |
| **Composio** | Bolt-on triggers | Yes | No | No | Elastic |
| **Mastra** | Event primitives | Yes | Per-request | No | Yes |
| **modastack** | **Native event bus** | **Yes** | **Always** | **Any topology** | **Yes — MIT** |

OpenClaw and Hermes are the closest to modastack in spirit — they're
agent harnesses with cron scheduling, inbound webhooks, and Slack
integration. Agents can monitor external systems on intervals and
react to incoming webhooks. But they lack a centralized event bus:
each agent wires its own cron jobs and webhook endpoints point-to-
point. There's no topic-based pub/sub, no shared event stream across
agents, and no way to compose agents into networks that communicate
through events. Adding a new agent that cares about the same events
means duplicating webhook and cron configuration, not adding a
subscription.

Mastra is the closest competitor in design philosophy. It has
`afterEvent`/`resumeWithEvent` and webhook triggers. But it lacks a
persistent event bus, catch-up replay, long-running agent daemons,
agent-to-agent messaging, and interactive access to running agents.
It's event-aware, not event-native.

The interactive + autonomous combination is what makes modastack's
event-driven architecture practical. Autonomous agents that you can't
talk to are too risky for production. Interactive agents that can't
react to events are too limited for real work. modastack agents are
both — persistent processes that act on events and accept human input
through the same session.

---

## How modastack solves it

### Create agents, wire them together

The foundation is the CLI. One command creates an agent:

```bash
modastack agents launch \
  --role oncall \
  --task "Monitor production and respond to incidents" \
  --subscribe pagerduty,sentry,github:org/infra
```

That agent is now a persistent, interactive process. It subscribes to
events, reasons about them, and takes action autonomously. But it's
also always reachable:

```bash
# Ask it a question (blocks until response)
modastack ask "What's the status of the current incident?"

# Inject context (fire-and-forget)
modastack message "The deploy was rolled back manually, stand down"
```

The CLI is built primarily for agent consumption, not just humans.
An agent can spawn other agents, ask them questions, check their
status, read their transcripts, and diagnose problems — using the
same CLI commands a human would. This means agents can build and
manage their own networks: a director agent can launch project leads,
monitor their progress with `modastack agents list` and
`modastack transcript show`, and intervene when something stalls.

Agents message each other the same way — synchronously (block until
response) or asynchronously (fire and forget). The framework doesn't
impose a topology. You build whatever shape the problem needs:

```bash
# Star: manager delegates to specialists
modastack agents launch --role manager --subscribe github:org/repo
modastack agents launch --role frontend --task "Handle UI changes"
modastack agents launch --role backend --task "Handle API changes"

# Chain: pipeline stages
modastack agents launch --role triage --subscribe linear:ENG
modastack agents launch --role spec --subscribe triage/handoff
modastack agents launch --role implement --subscribe spec/handoff

# Mesh: peers share findings
modastack agents launch --role researcher --subscribe topic/security
modastack agents launch --role researcher --subscribe topic/security
modastack agents launch --role synthesizer --subscribe topic/security
```

There's no structural difference between a "manager" and a "worker"
in the framework — every agent is a symmetric node that produces and
consumes events. Hierarchy, if you want it, comes from role prompts
and subscriptions. Not from code.

### Always interactive, always autonomous

This is the key property other frameworks lack. A modastack agent
processes events from the outside world **and** human input through
the same session. You can:

- Watch an agent handle a PagerDuty alert, then message it: "Don't
  roll back, the fix is already in flight"
- Ask an agent mid-workflow: "What did you find in the triage step?"
- Redirect an agent's approach: "Skip the integration tests, just
  run unit tests for now"

The agent doesn't lose context between interactions. It's a
persistent session that accumulates knowledge from events, human
messages, and its own actions over time. This is what makes
autonomous agents trustworthy — you're not handing off control,
you're collaborating with an agent that also happens to be watching
the world.

### Three paths for inbound events

modastack supports three complementary mechanisms for getting
real-world events into the system:

**1. Push (webhooks)**

External services send webhooks to the event server. The server
extracts a routing key (which repo? which workspace? which team?),
wraps the raw payload in a minimal envelope, and routes it to
subscribing agents.

```
Service → POST /webhooks/{source} → event server → WebSocket → agent
```

The event server doesn't reshape payloads. It extracts routing
information and passes the raw content through. Agents are LLMs —
they can interpret any JSON payload without normalization.

A generic webhook endpoint accepts events from any service. Per-source
connector configs handle routing key extraction and optional signature
verification:

```typescript
// Adding a new webhook source = ~5 lines of config
jira: {
  routingKey: (body) => `jira:${body.issue?.fields?.project?.key}`,
  type: (body) => `jira.${body.webhookEvent}`,
}
```

**2. Poll (monitors with native checks)**

Background monitors run on intervals, detect conditions that no
webhook fires for, and inject synthetic events into the same event
bus. Monitors handle deduplication — a condition that persists across
intervals fires once, not repeatedly.

```yaml
monitors:
  - name: pr_conflicts
    check: pr_conflicts        # native Python check function
    interval: 15m
    event: monitor/pr.conflict_detected
```

**3. Poll (monitors with MCP gateway)**

Monitors can poll external services through MCP (Model Context
Protocol) servers. An MCP gateway like Venn handles authentication
(OAuth tokens, API keys, refresh flows) for dozens of services. The
monitor scheduler calls MCP tools on each interval, diffs results
against last-seen state, and fires events for new items.

```yaml
connectors:
  gateway: venn               # MCP gateway handles all auth

monitors:
  - name: jira_new_issues
    tool: jira_search_issues   # MCP tool via gateway
    args:
      jql: "project = ENG AND created >= -5m"
    key_field: id
    interval: 5m
    event: jira/issue.created

  - name: sentry_errors
    tool: sentry_list_issues
    args:
      project: my-app
      query: "is:unresolved firstSeen:-5m"
    key_field: id
    interval: 5m
    event: sentry/issue.detected
```

This turns every MCP server into an event source with zero custom
code. The MCP ecosystem already has servers for GitHub, Jira,
Confluence, PagerDuty, Datadog, Sentry, Notion, Google Workspace,
Salesforce, and hundreds more. Each one becomes a modastack connector
through YAML configuration.

For checks that require judgment — "is this dashboard healthy?" vs.
"list items matching a query" — the monitor spawns a short-lived
agent with MCP access. The agent interprets the results, decides
whether the condition is actionable, and posts an event only if it
finds something.

### All paths converge

Regardless of how an event enters the system — push webhook, native
monitor, MCP poll, or agent-emitted — it lands on the same event bus
and routes to subscribing agents identically.

```
Webhook ─────────┐
Native monitor ──┤→ Event bus → Subscription routing → Agent WebSocket
MCP poll ────────┤
Agent event ─────┘
```

This is the key architectural property. An agent that handles Sentry
alerts doesn't know or care whether the alert came from a Sentry
webhook (push) or a monitor polling the Sentry MCP server (pull). The
agent's prompt and workflow logic are the same either way.

### Persistent agents with catch-up

modastack agents are long-running processes, not request-response
functions. They maintain a WebSocket connection to the event server,
hold conversational context across events, and resume after downtime
with cursor-based replay — every event missed during an outage is
delivered on reconnect.

This matters because real-world event handling requires continuity. An
agent investigating a CI failure needs to remember the PR it's working
on when the reviewer's comments arrive. An agent monitoring deploys
needs to correlate a rollback with the alert that triggered it. Chat-
mode agents lose this context between invocations.

### Declarative workflows: what to do with the event

Getting events to agents is half the problem. The other half is: what
should the agent actually do?

LLM agents are not deterministic. Given the same event twice, an agent
might take different approaches, skip steps, or get distracted by
tangential issues. This is fine for open-ended exploration. It's not
fine for knowledge work that requires discrete, repeatable steps —
triage before investigation, investigation before remediation, spec
before implementation.

Declarative workflows solve this. They define the steps, the order,
and which agent handles each step — while leaving the reasoning within
each step to the LLM. The workflow is deterministic; the work inside
each step is not.

```yaml
# .modastack/workflows/incident-response.yaml
name: incident-response
trigger: "PagerDuty alert fires for a production service"
steps:
  - name: triage
    agent: oncall
    task: "Assess severity, check recent deploys, identify affected services"
  - name: investigate
    agent: engineer
    task: "Find root cause using logs, metrics, and traces"
    depends_on: [triage]
  - name: remediate
    agent: engineer
    task: "Apply fix or rollback based on investigation"
    depends_on: [investigate]
```

This matters because most knowledge work has process. Support tickets
get triaged, then investigated, then resolved. Code changes get
specified, then implemented, then reviewed. Incidents get assessed,
then diagnosed, then fixed. The steps are known — what varies is the
reasoning within each step. Workflows encode the process; agents
bring the judgment.

Workflows are loaded from three tiers — built-in defaults, user-level
overrides, and per-project definitions — so teams customize behavior
without forking the framework.

### Agent packs: creating your own agents

Creating a purpose-built agent is a single command:

```bash
modastack agents create customer-support
```

This launches an interactive session with a builder agent that walks
you through designing your agent — what role it plays, what events it
subscribes to, what workflows it runs, what monitors it needs, when
it should notify you, and when it should ask for your input versus
acting autonomously. The builder generates a complete agent pack
written to disk, ready to launch.

You can also skip the interactive flow if you already know what you
want:

```bash
modastack agents create incident-responder \
  --task "Build an oncall agent that triages PagerDuty alerts, \
          investigates using Datadog, and posts findings to Slack"
```

The result is an agent pack — a portable bundle of everything an
agent needs to operate in a domain: role prompts, workflows, monitors,
and check functions. Packs are the distribution unit for agents. You
can install a pack someone else built, point it at your project, and
have a working agent for a domain you've never configured — the same
way you'd install a library instead of writing one from scratch.

```
agents/engineering_org/
├── defaults.yaml              # event_sources: [github, linear, slack]
├── agent.md                   # shared base prompt for all roles
├── roles/
│   ├── director.md            # engineering director role prompt
│   ├── project_lead.md        # project lead role prompt
│   └── engineer.md            # staff engineer role prompt
├── workflows/
│   ├── issue-lifecycle.yaml   # triage → spec → implement → PR
│   ├── pr-feedback.yaml       # address review comments
│   └── build-failure.yaml     # fix CI failures
└── monitors/
    └── github_checks.py       # PR conflict detection, stale PR checks
```

The framework is domain-agnostic. The engineering org pack is the
reference implementation. The same architecture supports DevOps
runbooks, customer support triage, content review pipelines, research
workflows, or any domain where agents should react to events.

---

## Why this matters

### Interactive + autonomous = trust

The biggest barrier to autonomous agents in production is trust. Teams
won't let an agent roll back a deploy or merge a PR if they can't see
what it's thinking and intervene when it's wrong.

modastack solves this by keeping agents interactive. They act
autonomously on events, but you can always ask what they're doing,
why they made a decision, or tell them to stop. This is the
difference between "autonomous agent" and "autonomous agent I
trust."

### Any topology, no framework lock-in

Agent networks aren't one-size-fits-all. Some problems need a
hierarchy (manager delegates to workers). Some need a pipeline
(stages process sequentially). Some need a mesh (peers collaborate).

With modastack, topology is configuration. Launch agents, give them
subscriptions, let them message each other. Change the shape by
changing the config, not by switching frameworks.

### Composability

Adding a new agent to the system means adding a subscription. The
existing agents don't change. A security-scanning agent can subscribe
to `github:org/repo` alongside the code-review agent — neither knows
the other exists. This is the pub/sub composability that made
microservices work, applied to agents.

### Resilience

Events persist independently of agent availability. An agent that goes
down at 2am doesn't miss the deploy failure — it catches up on
reconnect via cursor-based replay. This is table stakes for production
systems but impossible in request-response frameworks where missed
invocations are lost.

### Observability

The event log is an audit trail. Every event, every agent decision,
every workflow step is recorded. When something goes wrong, you can
replay the exact sequence of events that led to an agent's action.

### From reactive to proactive

Request-response agents are reactive: they do what you ask. Event-
driven agents are proactive: they notice things and act. A monitor
detects a stale PR, fires an event, and an agent follows up —
without anyone asking. This is the shift from "AI assistant" to "AI
team member."

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 External World                  │
│  GitHub · Slack · Linear · Jira · PagerDuty · … │
└──────────┬──────────────────┬───────────────────┘
           │ webhooks         │ MCP tools
           ▼                  ▼
┌─────────────────┐  ┌──────────────────┐
│  Event Server   │  │ Monitor Scheduler│
│  (CF Worker /   │  │ (polling loop)   │
│   local Node)   │  │                  │
│                 │  │  ┌────────────┐  │
│ POST /webhooks/ │  │  │ MCP Gateway│  │
│    {source}     │  │  │  (Venn /   │  │
│                 │  │  │  self-host)│  │
│ Subscription    │  │  └────────────┘  │
│ routing via KV  │  │                  │
└────────┬────────┘  └────────┬─────────┘
         │                    │
         │  events            │ synthetic events
         ▼                    ▼
┌─────────────────────────────────────────────────┐
│              Event Bus (unified queue)           │
│         topic-based pub/sub + cursor replay      │
└──────────┬──────────────┬───────────────┬───────┘
           │              │               │
           ▼              ▼               ▼
     ┌──────────┐  ┌──────────┐    ┌──────────┐
     │ Agent A  │  │ Agent B  │    │ Agent C  │
     │ (oncall) │  │ (eng mgr)│    │ (review) │
     │          │  │          │    │          │
     │ role     │  │ role     │    │ role     │
     │ prompt + │  │ prompt + │    │ prompt + │
     │ workflows│  │ workflows│    │ workflows│
     └──────────┘  └──────────┘    └──────────┘
```

### Event envelope

Events use a minimal envelope for routing. The payload passes through
untouched — agents interpret raw service payloads directly.

```json
{
  "id": "evt_abc123",
  "source": "github",
  "type": "github.pull_request",
  "timestamp": "2026-06-07T14:30:00Z",
  "repo": "org/repo",
  "payload": { "...raw webhook body..." }
}
```

The routing fields (`repo`, `team_key`, `workspace`, `channel`)
determine which subscribers receive the event. The `payload` is
opaque to the event server — only the subscribing agent interprets it.

### Subscription model

Agents declare subscriptions as topic keys:

```yaml
# agents/<pack>/defaults.yaml
event_sources:
  - github
  - slack
  - linear

# Or subscribe via CLI:
# modastack start eng-org --subscribe github:moda-labs/modastack
```

The event server maintains a subscription index in KV. When an event
arrives, it resolves matching subscription keys, finds the registered
agents, and delivers via WebSocket. Agents that were offline receive
missed events on reconnect via cursor-based replay.

---

## Getting started

```bash
# Install
uv tool install modastack

# Browse and install an agent pack
modastack agents browse
modastack agents update eng-org

# Start the agent
cd my-project
modastack start eng-org

# The agent is now:
# - Subscribed to GitHub, Slack, and Linear events
# - Running monitors for PR conflicts and stale PRs
# - Ready to execute workflows when events arrive
```

Adding a new event source with an MCP gateway:

```bash
# Connect an MCP gateway for additional services
modastack connect gateway venn --token $VENN_TOKEN

# Add a monitor that polls via MCP
modastack monitors add sentry_errors \
  --tool sentry_list_issues \
  --args '{"project": "my-app", "query": "is:unresolved firstSeen:-5m"}' \
  --key-field id \
  --interval 5m \
  --event sentry/issue.detected
```

---

## What modastack is not

**Not an integration platform.** modastack doesn't manage OAuth tokens
or maintain API connectors. It delegates service connectivity to MCP
servers and gateways. It owns the event loop, not the service mesh.

**Not an agent runtime.** modastack uses Claude Code sessions (or any
compatible agent runtime) as its execution engine. It doesn't compete
with LangGraph or CrewAI on reasoning architecture — it orchestrates
on top of them.

**Not a workflow automation tool.** Zapier and n8n connect apps with
deterministic if-then logic. modastack connects events to agents with
autonomous reasoning. The agent decides what to do, not a predefined
rule.

**Not a chat interface.** modastack agents can be messaged (via Slack,
CLI, or API), but that's one input channel among many. The primary
interaction model is event subscription, not conversation.

---

## The open-source case

modastack is MIT-licensed. This matters for three reasons:

**Trust.** Autonomous agents that react to production events need to be
auditable. Teams need to read the code that decides whether to roll
back a deploy or merge a PR. Open source isn't just a distribution
model here — it's a trust requirement.

**Extensibility.** Agent packs, workflow definitions, monitor checks,
and connector configs are all user-authored. The framework provides
the event bus and orchestration; the community provides the domain
expertise. This only works if the framework is open.

**No vendor lock-in.** The event server runs on Cloudflare Workers or
as a local Node.js process. Agents run wherever Claude Code (or
another runtime) runs. MCP gateways are swappable. There's no
managed service to depend on — you own the entire stack.

---

## Roadmap

### Now
- Generic webhook endpoint (`POST /webhooks/{source}`) with pluggable
  connector configs for routing key extraction
- MCP gateway integration in the monitor scheduler
- Agent pack registry for community-contributed domain packs

### Next
- CloudEvents envelope format for interop with non-agent infrastructure
- A2A protocol support for cross-organization agent discovery
- Event filtering and transformation rules (subscribe to
  `github:org/repo` but only `pull_request` events)
- Webhook subscription management (agents register their own webhook
  URLs with services on startup)

### Later
- Hosted event server with managed webhook endpoints
- Visual event flow editor for workflow and subscription design
- Event replay and simulation for testing agent behavior
