# Agent Pack Onboarding Design

How a pack author creates a new agent and how a user sets it up.

## Pack author experience

Five questions define an agent:

1. **What is this agent going to do?** Describe the domain in a sentence. "Manage the engineering SDLC", "Run sales outreach", "Monitor customer support tickets." This frames everything that follows.

2. **What are the distinct jobs involved?** Break the purpose down into roles. Think about the different hats a human team would wear. A sales pipeline might need a researcher, a copywriter, and a CRM updater. An engineering org might need a director who triages, project leads who coordinate, and engineers who execute. A simple agent might just be one role. Each role gets its own prompt and responsibilities — this is how the agent pack scales from a solo operator to a full team.

3. **How do you want to interact with it?** Choose:
   - **Slack** — chat with the agent in a channel, get updates, give instructions
   - **Telegram** — same, but via Telegram bot
   - **Autonomous** — no chat interface, the agent operates entirely on its own based on events and schedules

4. **What services does the team need?** Pick from: email, github, salesforce, calendar, linear, notion, jira, etc. These are the tools the agents will read from and write to.

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

For any non-native service the user wants to proactively respond to (question 5), the pack builder needs to construct the actual `venn exec` command. This requires exploring the user's Venn account to discover server IDs, tool names, and argument schemas.

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

The pack builder iterates through each service that needs monitoring, discovers the right tool, tests it, and writes the `command:` line for the monitor. The key is finding a tool that returns a list of items with stable IDs — the monitor scheduler diffs by ID across runs, so the tool output needs to be diffable.

This discovery step is why pack creation should be an interactive, agent-guided process rather than a static template. The available tools, server IDs, and argument schemas vary per user's Venn account.

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

## Service resolution

Services fall into two categories at startup:

**Native services** (github, slack, linear) — modastack has built-in webhook integrations for real-time events. These connect directly using their own credentials. Each has a different auth story:

- **Slack**: bot token handles everything (auto-detect workspace, verify webhooks, send replies)
- **Linear**: API key handles everything (auto-detect team, receive events, write back)
- **GitHub**: webhook setup is external (GitHub App install or manual), reads/writes via `gh` CLI with its own auth, no credential in agent.yaml needed

**Non-native services** (email, salesforce, calendar, etc.) — handled by Venn. Venn holds OAuth tokens for 50+ services behind a single API key. Agents interact via the `venn` CLI (like they use `gh` for GitHub). For inbound events, monitors poll via `venn exec` on a schedule.

The framework doesn't maintain a hardcoded list of every service. The logic is: "Do I know this natively? If not, it's a Venn service."

## User onboarding flow

When a user runs `modastack start <agent>`, the framework validates everything is connected:

```
$ modastack start sales-team

Services:
  ✓ github       (native, detected from git remote)
  ✓ linear       (native, API key configured)
  ✓ email        (venn, work-gmail connected)
  ✓ calendar     (venn, personal-google-calendar connected)
  ✗ salesforce   (venn — not connected)
    → Connect at venn.ai, then restart

Missing 1 required service.
```

### Setup steps for the user

1. **Native services**: provide credentials in agent.yaml (or env vars)
   - Slack: create a bot, paste bot token
   - Linear: generate API key, paste it
   - GitHub: install the modastack GitHub App on the repo (or manually configure webhooks pointing at the event server)

2. **Non-native services**: go to venn.ai, create an account, connect the services the agent needs (Gmail, Salesforce, Google Calendar, etc.), then paste the Venn API key into agent.yaml

That's it. One API key covers all non-native services.

## Config file design

`agent.yaml` is the single source of truth. It replaces the previous split between `defaults.yaml` (pack manifest) and `.modastack/config.yaml` (project credentials).

**Pack ships** `agents/<name>/agent.yaml` with defaults — services, entry point, monitors. No secrets.

**User overrides** in `.modastack/agent.yaml` — merged on top, secrets via `${ENV_VAR}` references.

Secrets are never hardcoded. The `${VAR}` references serve as documentation — glance at the config and know exactly what accounts and tokens are needed.

## Inbound events architecture

Two paths for getting events into the agent:

**Real-time webhooks** (native services only): GitHub, Slack, and Linear push webhooks to the event server. The event server normalizes payloads and routes them to subscribed agents via WebSocket. Sub-second latency.

**Polling monitors** (any service, including Venn): the monitor scheduler runs a shell command on an interval, parses JSON output, diffs against the previous run, and fires events for new items. Uses the existing monitor infrastructure with the `command:` field:

```yaml
monitors:
  - name: new-emails
    command: venn exec work-gmail list_messages '{"maxResults": 10, "q": "is:unread"}'
    interval: 5m
    event: email/received
```

The `command:` monitor is generic — it works with `venn exec`, `gh pr list`, `curl`, or any command that returns JSON. No LLM or agent is spawned; it's pure subprocess + JSON diff.

Monitor events route through the event server's generic topic endpoint (`POST /events/{topic}`), which routes based on `event.type` as a subscription key.

Venn does not provide webhooks, so polling via monitors is the only inbound event path for non-native services.

## Agent operations architecture

Agents interact with services through CLI tools:

- **Native**: `gh` for GitHub, Slack API via `modastack slack-reply`, Linear API via tool guides
- **Non-native**: `venn` CLI wraps the Venn REST API

The `venn` CLI mirrors how `gh` works:

```bash
venn help list_servers              # what's connected
venn tools search "send an email"   # find the right tool
venn tools describe -s gmail -t send_email  # get the schema
venn tools execute -s gmail -t send_email -a '{"to": "...", "body": "..."}'
```

A tool guide (`tools/venn.md`) in the agent pack teaches the agent how to use the CLI — same pattern as `tools/github.md`.

No MCP wiring is needed. Venn is just another CLI tool in the agent's environment.
