# How Bobi Works

Bobi is a framework for building **proactive agents**: agents that don't just
answer when you talk to them, but watch for real-world events and act on their
own. This page explains the six core ideas in about five minutes, each
illustrated with `eng-team`, the ready-made engineering agent that ships with
Bobi.

The one-line mental model:

```
  events (chat, webhooks, monitors)
        │
        ▼
  director agent ──picks──► YAML workflow ──runs in──► worker agent(s)
        │                                                   │
        └── shared memory + knowledge base ◄────────────────┘
```

Something happens, a coordinating agent decides what to do about it, a
deterministic workflow drives the work through defined roles, and everything
the team learns accumulates in shared memory. Each piece below.

## 1. Agent teams, not a single agent

A Bobi agent is really an **agent team**: a package of named roles, each with
its own prompt, scope, and tools. One role is the entry point (usually a
director or manager) that receives every event; it delegates real work by
launching **sub-agents** in other roles. Each sub-agent is its own Claude Code
(or Codex) session, so a team can run many pieces of work in parallel without
one context window turning into soup.

**eng-team example:** the team has two roles. The `director` triages incoming
events, picks workflows, and reports status to you; the `engineer` does the
actual coding. When three issues get assigned at once, the director launches
three engineer sub-agents, each in its own git worktree, and tracks them all
(up to a configured concurrency cap - eng-team allows 8).

Teams are installable packages, like dependencies: `bobi agents install
eng-team --name eng-team` gives you the whole org chart in one command.

## 2. Tools: CLIs, MCP servers, and skills

Agents act on the world through tools, and Bobi treats every kind of tool the
same way - a **dependency with a verifiable success condition**, declared in
the team's `agent.yaml` under `tool_library:`. Three common shapes:

- **CLIs.** Most tools are just command-line programs the agent runs, paired
  with a short markdown guide (`tools/<name>.md`) teaching the agent how to
  use them well.
- **MCP servers.** A dependency can carry an `mcp:` connection spec; Bobi
  renders it into the agent runtime's config and verifies it with a live
  handshake, so the agent gets the server's tools natively.
- **Skills.** A dependency can point at a skill library (a repo of reusable
  agent procedures); the install guide becomes a tool doc and the skill's
  commands become part of the agent's repertoire.

Every dependency declares a `success:` check, so `bobi agent <name> doctor`
can verify the toolchain and the framework can refuse to dispatch work that
would fail for a missing tool.

**eng-team example:** eng-team's primary GitHub interface is the `gh` CLI. Its
`tools/github.md` guide shows the agent exactly how to check PR status
(`gh pr view <number> --json state,mergeable,...`), open PRs, and read CI
results, and its dependency check verifies `gh --version` works before any
engineer is dispatched.

## 3. Deterministic workflows in YAML

For multi-step work you don't want to trust the model to remember the process.
A **workflow** is a YAML file: an ordered list of steps that a pure-Python
state machine walks through, one at a time. The engine has no LLM - the agent
does the work, the engine decides what's next. Each step names the **role**
that performs it, each step can pin its own **model**, and steps hand
structured data to later steps through a required **handoff** contract the
engine validates. Steps can also branch (`route`), post to Slack (`notify`),
or pause the whole run (`await`) until an external event - like a human
approval - resumes it, at zero cost while suspended.

**eng-team example:** the `issue-lifecycle` workflow drives every assigned
issue through the same recipe (abridged):

```yaml
steps:
  - name: setup      # engineer: create a git worktree
  - name: pickup     # engineer: triage, classify complexity
  - name: route      # no LLM: does this need a spec?
    if: "needs_spec == true"
    goto: spec
    else: implement
  - name: await_approval   # pause until a human approves the spec
    await: approval
  - name: implement  # engineer: write code and tests
  - name: pr         # engineer: open the pull request
  - name: qa         # engineer: verify frontend changes
```

Tests get written before the implementation, specs get approved before large
changes, and QA runs on every frontend change - because the YAML says so, not
because the model remembered. The
director routes events to workflows (some deterministically, via
`auto_dispatch` rules that fire before the LLM even sees the event), and
multiple workflow runs execute in parallel across worker agents.

## 4. How an agent hears about work

A Bobi agent has four ears, and all of them feed one event bus:

- **Chat.** Talk to it directly - from the terminal
  (`bobi agent eng-team ask "what's the status?"`) or from Slack, where it
  replies and reacts in-thread like a teammate.
- **Webhooks.** A built-in event server ingests webhooks from GitHub, Slack,
  Linear, or anything else, and fans each event out to the agents subscribed
  to its topic. Runs loopback-only on your machine by default, or as a
  Cloudflare Worker for public traffic.
- **Monitors (polling).** Webhooks tell you when something *happens*; monitors
  catch state that *drifts* with no event behind it. A monitor runs on a
  schedule, detects a condition, and publishes it as an event like any other.
- **Cron schedules.** A monitor's schedule can be an interval (`15m`) or
  wall-clock times with a timezone (`at: ["06:00", "18:00"]`), so recurring
  jobs are just monitors too.

**eng-team example:** a reviewer requests changes on a PR; the GitHub webhook
lands on the bus and triggers the `pr-feedback` workflow. Meanwhile `pr-conflict-check` polls every
15 minutes for merge conflicts nobody was notified about, `stale-pr-check`
flags PRs quiet for 48 hours, and a `team-status-roundup` cron fires at 06:00
and 18:00 to post an org-wide status report to Slack. You can also just DM it.

## 5. Memory that closes the loop

Agents forget when sessions end; Bobi's memory system makes sure the team
doesn't. A built-in curator (itself a monitor, shipped with every team)
periodically distills what happened across sessions into a single, bounded
team policy file - a curated list of durable **facts** ("staging deploys are
frozen on Fridays") and **decisions** ("we use squash merges") - and injects
it read-only into every agent's prompt. The more you work with an agent, the
more it behaves like someone who's been on the team a while. Full session
transcripts are also kept and searchable
(`bobi agent eng-team transcript search "deploy failure"`).

**eng-team example:** you tell the director once, in Slack, "always branch
from develop, not main." The curator distills that into team policy, and every
future engineer sub-agent starts its session already knowing it.

## 6. A knowledge base with semantic search, no setup

Any agent (or you) can create named knowledge bases and search them
semantically:

```bash
bobi agent eng-team kb create docs
bobi agent eng-team kb add docs --file architecture.md
bobi agent eng-team kb search docs "how does auth work"
```

Under the hood each KB is a local SQLite database combining full-text and
vector search (hybrid mode is the default). The embedding model downloads
itself on first use and a small embedding service starts automatically - there
is nothing to install, configure, or pay a vector-database vendor for.

**eng-team example:** feed the team your internal runbooks and architecture
docs once; when an engineer sub-agent picks up an issue touching the auth
system, it searches the KB and finds the relevant design doc by meaning, not
keyword.

## Putting it together

One event, end to end: a teammate assigns issue #42 on GitHub. The webhook
lands on the event bus and reaches eng-team's director. Routing rules match it
to `issue-lifecycle`, so the director launches an engineer sub-agent to run
it. The engineer sets up a worktree, triages the issue, decides it needs a
spec, writes one, and the workflow suspends awaiting your approval - costing
nothing while it waits. You approve in Slack; the run resumes, the engineer
implements with tests (already knowing your branch conventions from team
policy, and consulting the KB for the design doc), opens a PR with `gh`, and
the workflow posts the link back to your Slack thread. Fifteen minutes later
the conflict monitor is already watching that PR for drift.

## Where to go next

- **Try it in 15 minutes:** [QUICKSTART.md](QUICKSTART.md)
- **Build your own team:** [BUILDING_AGENT_TEAMS.md](BUILDING_AGENT_TEAMS.md)
  and the [create-agent skill](../skills/create-agent.md)
- **Deep dives:** [WORKFLOW_ENGINE.md](WORKFLOW_ENGINE.md) ·
  [MONITORS.md](MONITORS.md) · [TOOL_LIBRARY.md](TOOL_LIBRARY.md) ·
  [EVENT_SERVER.md](EVENT_SERVER.md) · [SECURITY.md](SECURITY.md)
