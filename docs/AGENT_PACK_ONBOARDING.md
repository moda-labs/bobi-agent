# Agent Pack Onboarding Design

How a pack author creates a new agent and how a user sets it up.

## Pack author experience

Five questions define an agent:

1. **What is this agent going to do?** Describe the domain. "Manage the engineering SDLC", "Run sales outreach", "Monitor customer support tickets." This frames everything that follows.

2. **How many specialists do you need?** An agent pack is a team. A simple pack might be a single role. A complex one has a hierarchy — a director that triages, project leads that coordinate, and engineers that execute. Each role gets its own prompt, responsibilities, and scope. Define the roles and who the entry point is.

3. **What services does the team need?** Pick from: email, github, salesforce, calendar, linear, slack, telegram, notion, jira, etc. These are the tools the agents will read from and write to.

4. **How do users talk to the agent?** Choose: slack, telegram, or cli (none). This is the interactive channel — where humans give instructions and get updates.

5. **Which services should push events to the agent?** For each service from question 3, opt in to inbound events. This determines whether the agent reacts to changes in that service (new email, PR opened, deal updated) or only reads/writes on demand.

### Example: engineering SDLC agent

> 1. **What does it do?** Manages the software development lifecycle — triages issues, coordinates project work, reviews PRs, monitors deploys.
> 2. **Specialists?** Three roles: a director (triages incoming work, assigns to projects), project leads (coordinate within a project), and engineers (execute tasks). Director is the entry point.
> 3. **Services?** GitHub (code + PRs), Linear (issue tracking), Slack (team comms).
> 4. **Chat?** Slack — the team talks to the agent in a channel.
> 5. **Events?** GitHub (react to PR opens, issue assignments), Linear (react to status changes), Slack (react to mentions and DMs).

### Example: sales outreach agent

> 1. **What does it do?** Monitors inbound leads, drafts personalized outreach, updates CRM.
> 2. **Specialists?** Single role — an outreach agent. No hierarchy needed.
> 3. **Services?** Salesforce (CRM), email (outreach), calendar (meetings).
> 4. **Chat?** Slack — sales team reviews drafts in a channel.
> 5. **Events?** Salesforce (new lead created), email (reply received).

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
