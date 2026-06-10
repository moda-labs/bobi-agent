# Agent Team Onboarding Design

How a team author creates a new agent and how a user sets it up.

## Team author experience

Five questions define an agent:

1. **What is this agent going to do?** Describe the domain in a sentence. "Manage the engineering SDLC", "Run sales outreach", "Monitor customer support tickets." This frames everything that follows.

2. **What are the distinct jobs involved?** Break the purpose down into roles. Think about the different hats a human team would wear. A sales pipeline might need a researcher, a copywriter, and a CRM updater. An engineering org might need a director who triages, project leads who coordinate, and engineers who execute. A simple agent might just be one role. Each role gets its own prompt and responsibilities — this is how an agent team scales from a solo operator to a full organization.

3. **How do you want to interact with it?** Choose:
   - **Slack** — chat with the agent in a channel, get updates, give instructions
   - **Telegram** — same, but via Telegram bot
   - **Autonomous** — no chat interface, the agent operates entirely on its own based on events and schedules

4. **What services does the team need?** Pick from: email, github, salesforce, calendar, linear, notion, jira, etc. These are the tools the agents will read from and write to. Internal services and custom tooling count too — anything with an MCP server can be declared in `mcp_servers` and its tools land in the agents' sessions automatically.

5. **Which sources should the agent proactively respond to?** For each service from question 4, decide: should the agent watch for changes and react on its own (new email arrives, PR opens, deal updates), or only interact with it when asked? This is the difference between an agent that monitors your inbox vs one that only sends email when told to.

### Example: engineering SDLC agent

> 1. **Purpose?** Manages the software development lifecycle — triages issues, coordinates project work, reviews PRs, monitors deploys.
> 2. **Jobs?** Three roles: a director (triages incoming work, assigns to projects), project leads (coordinate within a project), and engineers (execute tasks). Director is the entry point.
> 3. **Interaction?** Slack — the team talks to the agent in a channel.
> 4. **Services?** GitHub (code + PRs), Linear (issue tracking).
> 5. **Events?** GitHub (react to PR opens, issue assignments), Linear (react to status changes), Slack (react to mentions and DMs).

### Example: sales outreach agent

> 1. **Purpose?** Monitors inbound leads, drafts personalized outreach, updates CRM.
> 2. **Jobs?** Three roles: a researcher (enriches lead data), a copywriter (drafts outreach), and a CRM updater (logs activity). Or just one role if you want it simple.
> 3. **Interaction?** Slack — sales team reviews drafts in a channel before they go out.
> 4. **Services?** Salesforce (CRM), email (outreach), calendar (meetings).
> 5. **Events?** Salesforce (new lead created), email (reply received).

### Example: deploy monitor

> 1. **Purpose?** Watches production deploys, runs smoke tests, alerts on failures.
> 2. **Jobs?** Single role — one agent handles it all.
> 3. **Interaction?** Autonomous — no human chat, just monitors and alerts to a Slack channel.
> 4. **Services?** GitHub (deploy events), Slack (alert channel).
> 5. **Events?** GitHub (deploy status changes).

### Monitor discovery: building the `command:` lines

For any non-native service the user wants to proactively respond to (question 5), the team builder needs to construct the actual `venn exec` command. This requires exploring the user's Venn account to discover server IDs, tool names, and argument schemas.

The discovery flow:

```bash
# 1. What servers are connected?
venn help list_servers
# → work-gmail (gmail), salesforce (salesforce), personal-google-calendar (googlecalendar), ...

# 2. What tools does this server have for listing/polling?
venn tools search "list recent emails"
# → work-gmail / list_messages (rank 1)
# → work-gmail / search_messages (rank 2)

# 3. What arguments does it take?
venn tools describe -s work-gmail -t list_messages
# → maxResults (int), q (string), labelIds (array), ...

# 4. Test the command
venn tools execute -s work-gmail -t list_messages -a '{"maxResults": 5, "q": "is:unread"}'
# → [{"id": "msg1", "subject": "...", ...}, ...]
```

The team builder iterates through each service that needs monitoring, discovers the right tool, tests it, and writes the `command:` line for the monitor. The key is finding a tool that returns a list of items with stable IDs — the monitor scheduler diffs by ID across runs, so the tool output needs to be diffable.

This discovery step is why team creation should be an interactive, agent-guided process rather than a static template. The available tools, server IDs, and argument schemas vary per user's Venn account.

**Fallback: agent-based monitors.** Not every Venn tool returns clean, diffable JSON with stable IDs. Some return nested structures, paginated results, or data that needs interpretation to decide if it's actionable. In those cases, use a description-only monitor instead — the scheduler spawns a short-lived agent that calls the Venn CLI, interprets the results, and decides whether to fire an event. More expensive (uses an LLM call per interval), but handles any complexity:

```yaml
monitors:
  - name: important-emails
    description: >
      Check for emails from VIP customers (domain: @bigcorp.com)
      in the last 30 minutes. Only fire if the email looks urgent
      or mentions a production issue.
    interval: 10m
    event: email/vip_urgent
```

### Output

These answers produce a single `agent.yaml`:

```yaml
version: "1.0.0"
entry_point: director
chat: slack

roles:
  - director
  - project_lead
  - engineer

services:
  - name: github
    events: true
  - name: linear
    events: true
  - name: email
    events: true
  - name: salesforce
  - name: calendar

monitors:
  - name: new-emails
    command: venn exec work-gmail list_messages '{"maxResults": 10, "q": "is:unread"}'
    interval: 5m
    event: email/received

slack:
  bot_token: ${SLACK_BOT_TOKEN}
linear:
  api_key: ${LINEAR_API_KEY}
venn_api_key: ${VENN_API_KEY}
```

## Service connection: three mechanisms

Every service the agent uses connects through one of three mechanisms:

### API key services (native)

GitHub, Slack, Linear — modastack has built-in webhook integrations. Each has its own auth:

- **GitHub**: auto-detected from git remote. Reads/writes via `gh` CLI (user runs `gh auth login`). Webhooks via GitHub App install or manual setup.
- **Slack**: bot token referenced as `${SLACK_BOT_TOKEN}` in agent.yaml, value in `.modastack/.env`. Handles workspace detection, webhooks, and replies.
- **Linear**: API key referenced as `${LINEAR_API_KEY}` in agent.yaml, value in `.modastack/.env`. Handles team detection, webhooks, and writes.

Setup: paste the API key or token when `modastack install` prompts. Done.

### OAuth services (via Venn)

Gmail, Salesforce, Google Calendar, Notion, Jira, HubSpot, Dropbox, etc. — anything that requires OAuth.

OAuth is hard in headless environments. The token acquisition flow requires a browser redirect, but on EC2 there's no browser. Worse, each provider requires a registered OAuth application with approved scopes — Google's security review alone takes weeks. The MCP ecosystem hasn't solved this: users are creating their own Google Cloud projects just to read email.

Venn solves both problems: it holds pre-registered OAuth apps for 50+ services and manages all tokens behind a single API key. The user connects services on venn.ai (browser-based, one-time), then the agent uses the `venn` CLI for reads/writes and `venn exec` in monitors for polling.

Setup: create a Venn account, connect services, paste the API key. One key covers everything.

### Custom MCP servers

For internal services, custom tools, or anything not covered by native integrations or Venn. Declared in agent.yaml:

```yaml
mcp_servers:
  internal-crm:
    type: http
    url: https://crm.internal/mcp
    headers:
      Authorization: Bearer ${CRM_TOKEN}
  local-tools:
    type: stdio
    command: node
    args: ["tools/server.js"]
```

MCP servers are wired directly into the Claude Code session via the SDK. The agent gets their tools automatically. Supports HTTP, SSE, and stdio transports.

Setup: provide the URL/command and credentials. Preflight validation probes each MCP server to verify it connects and lists tools.

## Installing a team: `modastack install`

Installing is distinct from authoring. A user with an existing team — from
the repo, a teammate, or a registry — runs:

```
$ modastack install agents/eng-team

Installed 'eng-team' into .modastack/
  roles: director, engineer, project_lead
  tools: github.md, linear.md, slack.md, venn.md
  workflows: adhoc.yaml, issue-lifecycle.yaml, pr-feedback.yaml, ...

This agent needs credentials:
  SLACK_BOT_TOKEN: xoxb-...
Credentials saved to .modastack/.env

Run `modastack start` to launch.
```

Resolution order: local path first, then remote registries. Install:

1. Copies the team's `roles/`, `tools/`, `workflows/`, `monitors/`,
   `context/`, and `agent.md` into `.modastack/` — the only runtime
   location.
2. Writes the team's `agent.yaml` verbatim into `.modastack/agent.yaml`
   (plus the team name). The installed copy is a frozen image —
   regenerated wholesale on every install, never merged with prior
   state, never hand-edited. Per-machine variance enters only through
   `${VAR}` references resolved from `.env`.
3. Seeds `<project>/workspace/` from the team's `workspace/` templates —
   user-owned domain files, copied only if absent. Unlike the frozen
   image, reinstall never overwrites them and they are not
   manifest-tracked.
4. Scans the installed config for `${VAR}` references, prompts for any
   missing values, and writes them to `.modastack/.env` (gitignored
   automatically).
5. Records a hash of every installed file in `install-manifest.json` —
   `modastack doctor` flags hand-edits to the frozen image before a
   reinstall would silently destroy them.

`modastack start` takes no arguments — it reads `.modastack/agent.yaml`,
loads `.env`, runs preflight, and launches. If no agent is installed it
says so and lists available teams.

### Two paths: downloaded vs. local source

The source of truth is wherever the team came from, and install adjusts
what gets checked in accordingly:

**Downloaded team** — installed from a registry. The copy in `.modastack/`
is the only copy, so it is the source of truth. Edit roles, workflows, and
monitors in place; check the contents in to share customizations with the
team. Only `.env`, `sessions/`, and `state/` are gitignored.

**Local source of truth** — the team lives at `agents/<name>/` in the repo,
checked in. Install materializes it into `.modastack/` as a build artifact:
the installed copies are gitignored and never hand-edited. To customize,
edit `agents/<name>/` and reinstall. This avoids two diverging copies of
the same team in git.

Install writes `.modastack/.gitignore` to match the path it took. Either
way, `.modastack/agent.yaml` (the manifest — which team, its config,
`${VAR}` refs) and the rest of the runtime contract stay identical.

## Interactive onboarding: `modastack setup`

The `modastack setup` command starts by offering existing teams from the
registry — most users should start from a working team rather than a blank
page. Three branches:

- **Use as-is** — drops straight into the install flow: credentials, then
  start.
- **Customize** — loads the existing team's shape as the starting answers
  to the five questions, walks through each one for review (roles to add or
  drop, services to change, event sources to toggle), then continues to
  service connection. Customizing materializes the team into
  `agents/<name>/` in the project (the eject step) and installs from there
  — the user now owns the source, and `.modastack/` stays a frozen build
  artifact. This is the only sanctioned way to modify a team you didn't
  author.
- **Build your own** — walks through the five questions from scratch.

Every branch starts the same way:

```
$ modastack setup

Use an existing agent team, customize one, or build your own?

  Available teams:
    eng-team          Engineering team — a director triages issues and
                      coordinates project leads and engineers across repos.
                      GitHub + Linear + Slack.
    content-review    Content pipeline — researchers, editors, and fact
                      checkers produce and maintain documentation from
                      GitHub issues and email requests.
```

### Customizing an existing team

Setup shows the team's current shape and walks through each of the five
questions with the team's answers pre-filled — keep or change each one:

```
> customize content-review

content-review currently:
  Purpose:     Produce and maintain documentation from issues and email
  Roles:       manager (entry), researcher, editor, fact_checker
  Interaction: Slack
  Services:    github (events), email (events)

Roles — keep all four?
> drop fact_checker, keep the rest

Interaction — keep Slack?
> yes

Services — keep github and email?
> add linear, with events

...continues to service connection, same as below.
```

### Building from scratch

```
> build my own

What is this agent going to do?
> Manage sales outreach — monitor leads, draft emails, update CRM

What are the distinct jobs?
> A researcher that enriches leads, a copywriter that drafts outreach

How do you want to interact with it?
> Slack

What services does the team need?
> salesforce, email, calendar, slack

Which should it proactively respond to?
> salesforce (new leads), email (replies)

Connecting services...

  slack — needs a bot token.
  Paste your Slack bot token: xoxb-...
  ✓ slack                          native

  github — auto-detected from git remote.
  ✓ github                         native

  salesforce, email, calendar — these require OAuth.
  Go to venn.ai, connect: Salesforce, Gmail, Google Calendar
  Paste your Venn API key: venn_...
  ✓ email                          venn
  ✓ calendar                       venn
  ✓ salesforce                     venn

  Credentials saved to .modastack/.env

Building monitors for event sources...
  Exploring Venn tools for salesforce polling...
  ✓ salesforce/updated — venn exec salesforce query_records '{"object": "Lead", "limit": 20}'
  Exploring Venn tools for email polling...
  ✓ email/received — venn exec work-gmail list_messages '{"maxResults": 10, "q": "is:unread"}'

Writing agents/sales-outreach/ (roles, monitors, agent.yaml)...
Installing into .modastack/...
Done. Run `modastack start` to launch.
```

Setup ends with the agent installed. The answers produce a team source at
`agents/<name>/` and setup runs install internally — so the result is a
normal local-source team, and `.modastack/` stays a regenerable artifact.

Because install is idempotent (frozen image, no merge), setup is safe to
re-run at any time: to revisit the five questions, add a service, or reset
a hand-edited image back to its source. If the user has edited
`.modastack/` directly, `modastack doctor` flags the drift against the
install manifest, and setup offers to migrate those edits into the team
source before regenerating.

### Preflight validation

On every `modastack start`, the framework runs preflight checks before launching:

```
Preflight checks:
  ✓ github                         native
  ✓ slack                          native
  ✓ email                          venn
  ✓ calendar                       venn
  ✗ salesforce                     venn — not connected
    → Connect at venn.ai, then restart
  ✓ internal-crm                   mcp, 12 tools
```

Checks: entry point role exists, native credentials present, Venn services connected (REST API call), MCP servers connect and list tools (Claude SDK probe).

## Config file design

`agent.yaml` is the single source of truth for an agent team.

**Team ships** `agents/<name>/agent.yaml` with defaults — entry point, services, monitors, and `${VAR}` references for any credentials it needs. No secrets.

**Install copies** it verbatim into `.modastack/agent.yaml`. The installed copy mimics a runtime installation — frozen, regenerated on every install, never edited in place. Customizing the team means editing the source `agents/<name>/agent.yaml` and reinstalling. Whether `.modastack/` is checked in depends on which install path was taken (see "Two paths" above) — downloaded teams live there as the source of truth; local-source teams treat the whole image, `agent.yaml` included, as a gitignored build artifact.

**Secrets live in `.modastack/.env`** — gitignored, created by `modastack install`, which scans the installed config for `${VAR}` references and prompts for each missing value. `Config.load()` reads `.env` into the environment before resolving the config, so every command (start, doctor, monitors) sees resolved values through a single path. `.env` is also where per-machine, non-secret variance goes — e.g. `event_server: ${MODASTACK_EVENT_SERVER}` resolves to the cloud Worker in production and to nothing (auto-started local server) in CI and local dev.

The `${VAR}` references serve as documentation — glance at the config and know exactly what accounts and tokens are needed. Preflight validation resolves them and fails with a pointed hint if any are missing.

## Inbound events architecture

Two paths for getting events into the agent:

**Real-time webhooks** (native services only): GitHub, Slack, and Linear push webhooks to the event server. The event server normalizes payloads and routes them to subscribed agents via WebSocket. Sub-second latency.

**Polling monitors** (any service): the monitor scheduler runs a shell command on an interval, parses JSON output, diffs against the previous run, and fires events for new items:

```yaml
monitors:
  - name: new-emails
    command: venn exec work-gmail list_messages '{"maxResults": 10, "q": "is:unread"}'
    interval: 5m
    event: email/received
```

The `command:` monitor is generic — works with `venn exec`, `gh pr list`, `curl`, or any command that returns JSON. No LLM or agent spawned; pure subprocess + JSON diff.

Monitor events route through the event server's generic topic endpoint (`POST /events/{topic}`), which uses `event.type` as a fallback subscription key when no source-specific routing fields exist.

## Agent operations architecture

Agents interact with services through CLI tools and MCP:

- **Native**: `gh` for GitHub, Slack API via `modastack slack-reply`, Linear API via tool guides
- **Venn services**: `venn` CLI wraps the Venn REST API
- **Custom MCP**: tools appear in the Claude session automatically

The `venn` CLI mirrors how `gh` works:

```bash
venn help list_servers              # what's connected
venn tools search "send an email"   # find the right tool
venn tools describe -s gmail -t send_email  # get the schema
venn tools execute -s gmail -t send_email -a '{"to": "...", "body": "..."}'
```

Tool guides (`tools/venn.md`, `tools/github.md`) in the agent team teach the agent how to use CLI tools. MCP server tools are discovered automatically by the Claude SDK — no guide needed.
