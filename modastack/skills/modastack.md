# Using Modastack

Guide the user through running, operating, and extending modastack —
the event-driven AI agent framework.

## What modastack is

Modastack is a framework for running persistent AI agent teams. Agents
are symmetric nodes that subscribe to event topics and receive events
via a centralized event server. All domain behavior comes from agent
packs (role prompts, workflows, monitors) — the framework has no
topology opinions.

## Quick start

```bash
# Install
uv tool install modastack

# Start an agent team
modastack start <pack-name>

# Stop
modastack stop

# Restart fresh (wipes manager session)
modastack start <pack-name> --fresh
```

## Core commands

### Agent management

```bash
modastack agents launch -w <workflow> --role <role> --task "context"
modastack agents list
modastack agents show <id>
modastack agents cancel <id>
```

### Communication

```bash
modastack message "text"       # inject into any session
modastack ask "question"       # ask and block for response

# Slack
modastack slack-reply -w <workspace> -c <channel> "message"
modastack slack-reply -w <workspace> -c <channel> -t <thread-ts> "threaded reply"
```

`slack-reply` converts Markdown to Slack formatting (`**bold**` → `*bold*`,
links, headings). Messages over 3000 chars are truncated. Requires
`slack.bot_token` in `~/.modastack/config.yaml`.

### Observability

```bash
modastack status               # active engineer sessions
modastack events               # recent events and decisions
modastack transcript show <s>  # session transcript
modastack transcript search q  # search history
modastack doctor               # system health check
```

### Workflows

```bash
modastack workflows list       # available workflows
modastack workflows status     # active workflow runs
modastack workflows validate f # validate a workflow YAML
```

### Monitors

```bash
modastack monitors list        # list all monitors (merged across tiers)
modastack monitors add <name>  # add a monitor
modastack monitors pause <n>   # disable a monitor
modastack monitors remove <n>  # remove a user-added monitor
```

### Roles and registry

```bash
modastack roles list                    # available roles
modastack agents browse                 # browse remote registry
modastack agents update <name>          # update from remote
modastack agents add-registry <repo>    # add a remote registry
```

### Knowledge base

```bash
modastack kb create <name>                  # create a named KB
modastack kb add <name> --file <path>       # index a file
modastack kb add <name> --text "..."        # add inline text
modastack kb search <name> "query"          # hybrid FTS + semantic search
modastack kb search <name> "q" --mode fts   # keyword-only search
modastack kb list                           # list all KBs
modastack kb info <name>                    # show stats
modastack kb remove <name>                  # delete a KB
```

Each KB is a separate SQLite database at `.modastack/kb/<name>.db` with
FTS5 for keyword search and sqlite-vec for semantic search. An embedding
sidecar (sentence-transformers) auto-starts on first use and stays alive
between commands. `modastack stop` tears it down.

Agent teams use KBs for domain-specific retrieval — index Google Docs,
Slack history, emails, or any text source into a named KB, then search
it from agent tools.

### Event server

```bash
modastack event-server start   # start local event server
modastack event-server stop    # stop local event server
```

## Architecture overview

```
Event sources (GitHub, Slack, Linear, etc.)
    │
    ▼
Event Server (Cloudflare Worker or local Node.js)
    │ WebSocket pub/sub
    ▼
Subscribing Agents (symmetric nodes)
    │
    ▼
Outputs (PRs, messages, reports, deployments)
```

Key concepts:
- **Agent team**: portable bundle of roles, workflows, monitors, tools
- **Symmetric node**: any agent can subscribe to any topic — no hierarchy in framework
- **Event server**: centralized pub/sub (topic-based)
- **Workflow**: YAML DAG defining multi-step processes
- **Monitor**: scheduled polling for conditions no webhook covers
- **Handoff**: YAML contract between workflow steps

## Configuration

### Machine-wide (`~/.modastack/config.yaml`)

Service credentials and connection URLs (not checked in):

```yaml
slack:
  bot_token: xoxb-...
event_server:
  url: https://modastack-events.example.workers.dev
linear:
  api_key: lin_api_...
registries:
  - moda-labs/modastack-agents
```

### Per-project overrides (`.modastack/`)

```
.modastack/
├── roles/<role>.md          # Override a role prompt
├── tools/<service>.md       # Override a tool guide
├── workflows/<name>.yaml    # Override a workflow
└── monitors/defaults.yaml   # Override monitors
```

Resolution order (most specific wins):
1. `.modastack/` in project
2. Agent team directory
3. User cache (`~/.modastack/agents/`)

## Common tasks

### Start a new project with modastack

1. Install: `uv tool install modastack`
2. Create or fetch an agent team into `agents/<name>/`
3. Configure `~/.modastack/config.yaml` with credentials
4. Run: `modastack start <name>`

### Debug a running system

```bash
modastack doctor              # check health
modastack status              # who's running
modastack events              # what happened recently
modastack transcript search "error"  # find issues in history
```

### Customize behavior for a project

Create `.modastack/` overrides in the project root. These take precedence
over the pack's built-in files without modifying the pack itself.

### Add a new workflow

Write a YAML file in `.modastack/workflows/` or the pack's `workflows/`
directory. Use `modastack workflows validate <file>` to check syntax.

### Add a monitor

```bash
modastack monitors add stale-prs --interval 2h --description "Check for PRs open > 3 days"
```

Or add to `monitors/defaults.yaml` in the pack.

## Troubleshooting

| Symptom | Check |
|---|---|
| Agent not responding to events | `modastack events` — is the event arriving? |
| Workflow stuck | `modastack workflows status` — which step? |
| Agent crashed | `modastack agents list` — status column |
| Event server down | `modastack event-server start` |
| Credentials failing | `modastack doctor` — checks service connectivity |

## Tips

- Use `--fresh` to wipe state when things get confused
- Monitors are for polling gaps — prefer webhooks when available
- Role prompts are the primary lever for behavior — invest time in them
- Handoff fields are contracts between steps; change them carefully
- The `adhoc` workflow catches anything that doesn't match a specific trigger
