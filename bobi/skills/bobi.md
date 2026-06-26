# Using Bobi

Guide the user through running, operating, and extending bobi —
the event-driven AI agent framework.

## What bobi is

Bobi is a framework for running persistent AI agent teams. Agents
are symmetric nodes that subscribe to event topics and receive events
via a centralized event server. All domain behavior comes from agent
packs (role prompts, workflows, monitors) — the framework has no
topology opinions.

## Quick start

```bash
# Install
uv tool install bobi

# Start an agent team
bobi start <pack-name>

# Stop
bobi stop

# Restart fresh (wipes manager session)
bobi start <pack-name> --fresh
```

## Core commands

### Agent management

```bash
bobi agents launch -w <workflow> --role <role> --task "context"
bobi agents list
bobi agents show <id>
bobi agents cancel <id>
```

### Communication

```bash
bobi message "text"       # inject into any session
bobi ask "question"       # ask and block for response

# Slack
bobi slack-reply -w <workspace> -c <channel> "message"
bobi slack-reply -w <workspace> -c <channel> -t <thread-ts> "threaded reply"
```

`slack-reply` converts Markdown to Slack formatting (`**bold**` → `*bold*`,
links, headings). Messages over 3000 chars are truncated. Requires
`slack.bot_token` in `~/.bobi/config.yaml`.

### Observability

```bash
bobi status               # active engineer sessions
bobi events               # recent events and decisions
bobi transcript show <s>  # session transcript
bobi transcript search q  # search history
bobi doctor               # system health check
```

### Workflows

```bash
bobi workflows list       # available workflows
bobi workflows status     # active workflow runs
bobi workflows validate f # validate a workflow YAML
```

### Monitors

```bash
bobi monitors list        # list all monitors (merged across tiers)
bobi monitors add <name>  # add a monitor
bobi monitors pause <n>   # disable a monitor
bobi monitors remove <n>  # remove a user-added monitor
```

### Roles and registry

```bash
bobi roles list                    # available roles
bobi agents browse                 # browse remote registry
bobi agents update <name>          # update from remote
bobi agents add-registry <repo>    # add a remote registry
```

### Knowledge base

```bash
bobi kb create <name>                  # create a named KB
bobi kb add <name> --file <path>       # index a file
bobi kb add <name> --text "..."        # add inline text
bobi kb search <name> "query"          # hybrid FTS + semantic search
bobi kb search <name> "q" --mode fts   # keyword-only search
bobi kb list                           # list all KBs
bobi kb info <name>                    # show stats
bobi kb remove <name>                  # delete a KB
```

Each KB is a separate SQLite database at `.bobi/kb/<name>.db` with
FTS5 for keyword search and sqlite-vec for semantic search. An embedding
sidecar (fastembed/ONNX) auto-starts on first use and stays alive
between commands. `bobi stop` tears it down.

Agent teams use KBs for domain-specific retrieval — index Google Docs,
Slack history, emails, or any text source into a named KB, then search
it from agent tools.

### Event server

```bash
bobi event-server start   # start local event server
bobi event-server stop    # stop local event server
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

### Machine-wide (`~/.bobi/config.yaml`)

Service credentials and connection URLs (not checked in):

```yaml
slack:
  bot_token: xoxb-...
event_server:
  url: https://bobi-events.example.workers.dev
linear:
  api_key: lin_api_...
registries:
  - moda-labs/bobi-agents
```

### Per-project overrides (`.bobi/`)

```
.bobi/
├── roles/<role>.md          # Override a role prompt
├── tools/<service>.md       # Override a tool guide
├── workflows/<name>.yaml    # Override a workflow
└── monitors/defaults.yaml   # Override monitors
```

Resolution order (most specific wins):
1. `.bobi/` in project
2. Agent team directory
3. User cache (`~/.bobi/agents/`)

## Common tasks

### Start a new project with bobi

1. Install: `uv tool install bobi`
2. Create or fetch an agent team into `agents/<name>/`
3. Configure `~/.bobi/config.yaml` with credentials
4. Run: `bobi start <name>`

### Debug a running system

```bash
bobi doctor              # check health
bobi status              # who's running
bobi events              # what happened recently
bobi transcript search "error"  # find issues in history
```

### Customize behavior for a project

Create `.bobi/` overrides in the project root. These take precedence
over the pack's built-in files without modifying the pack itself.

### Add a new workflow

Write a YAML file in `.bobi/workflows/` or the pack's `workflows/`
directory. Use `bobi workflows validate <file>` to check syntax.

### Add a monitor

```bash
bobi monitors add stale-prs --interval 2h --description "Check for PRs open > 3 days"
```

Or add to `monitors/defaults.yaml` in the pack.

## Troubleshooting

| Symptom | Check |
|---|---|
| Agent not responding to events | `bobi events` — is the event arriving? |
| Workflow stuck | `bobi workflows status` — which step? |
| Agent crashed | `bobi agents list` — status column |
| Event server down | `bobi event-server start` |
| Credentials failing | `bobi doctor` — checks service connectivity |

## Tips

- Use `--fresh` to wipe state when things get confused
- Monitors are for polling gaps — prefer webhooks when available
- Role prompts are the primary lever for behavior — invest time in them
- Handoff fields are contracts between steps; change them carefully
- The `adhoc` workflow catches anything that doesn't match a specific trigger
