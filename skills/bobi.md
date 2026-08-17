# Using Bobi

Guide the user through running, operating, and extending Bobi Agents.
Bobi is an event-driven framework for persistent AI agent teams. Domain
behavior comes from Bobi Agent packages: roles, workflows, monitors,
tools, context files, and workspace templates.

## Directory Model

`BOBI_HOME` is the only user-configurable home location. It is set by
environment variable and defaults to `~/.bobi`.

```text
$BOBI_HOME/
├── config.yaml
└── agents/
    └── <name>/
        ├── src/              # editable Bobi Agent source
        └── run/              # selected runtime root
            ├── package/      # installed frozen package
            ├── state/        # sessions, logs, pid files, policy
            ├── workspace/    # user-owned domain files and outputs
            └── .env          # runtime credentials
```

Runtime commands are scoped to one installed Bobi Agent:

```bash
bobi agents list
bobi agents install ./agents/eng-team --name eng
bobi agent eng start
bobi agent eng status
bobi agent eng ask "what's the status?"
```

## Machine Commands

```bash
bobi app start                        # unified web app (dashboard + onboarding
bobi app stop|restart|status          #   + chat), runs in the background
bobi setup <name>                     # design/build/install a Bobi Agent
bobi agents install <source> --name <name>
bobi agents install <source> --name <name> --with-deps  # + install declared deps locally
bobi agents list
bobi agents browse
bobi agents update <name>
bobi agents add-registry <repo>
bobi build <team> --tag <ref> [--push]  # render a team into a ready-to-run
                                        #   image (needs the deploy plugin)
```

`<source>` can be a local source directory, local `.tar.gz`, public
`.tar.gz` URL, or registry name.

## Runtime Commands

```bash
bobi agent <name> start
bobi agent <name> stop
bobi agent <name> restart
bobi agent <name> start --fresh
bobi agent <name> status
bobi agent <name> doctor

# Supervise the manager as the terminal process (containers, pod specs).
# Spawns + probes the manager, publishes heartbeat/lifecycle telemetry, and
# listens on the admin topic so a wedged manager can still be restarted.
# Everything after `--` forwards to the manager's start command.
# This is what a container entrypoint runs as PID 1 - not for interactive use.
bobi agent <name> supervise -- --foreground

bobi agent <name> ask "question"
bobi agent <name> message "text"
bobi agent <name> compact
bobi agent <name> events
bobi agent <name> events publish alert/firing --json '{"title":"x"}'

# Scoped ingest tokens: let an external system (alerting, CI, SaaS webhooks)
# POST plain JSON to one topic via /webhooks/ingest/<topic>. The token is
# shown once at creation; the server stores only a hash.
bobi agent <name> events ingest-token create alert/firing --name oncall
bobi agent <name> events ingest-token list
bobi agent <name> events ingest-token revoke <id>

bobi agent <name> transcript show manager
bobi agent <name> transcript search "query"
bobi agent <name> costs

# Recover token telemetry for sessions that recorded zero (#935). Reads each
# session's retained Claude transcript and fills ONLY missing counters -
# recorded tokens and provider dollars are never overwritten. Dry run by
# default; re-running after a write is a no-op.
bobi agent <name> costs backfill
bobi agent <name> costs backfill --write

# Reply into a chat conversation (channel-agnostic; ref comes from the event)
bobi reply <conversation> "markdown text"
bobi reply <conversation> --edit <ts> "text"     # resolve a placeholder
bobi reply <conversation> --file <path> "comment"
bobi read-conversation <conversation> [-n 50] [--json-output]
```

Use `bobi reply` and `bobi read-conversation` for Slack and any other
chat channel delivered through the channel gateway.

## Upgrading Bobi In Place

A local upgrade replaces bobi's files underneath whatever is already
running, and neither the team reinstall nor `bobi agent <name> restart`
restarts the local event server. Restart both:

```bash
uv tool install --upgrade bobi
bobi agents install ./agents/<team> --name <name>
bobi agent <name> restart
bobi agent <name> event-server restart
```

Each long-lived process records the bobi it launched from, so anything
still on the replaced code is named - by the install itself, and by the
`Running code` check in `bobi agent <name> doctor`, with the restart
command to clear it. Containers cannot drift this way: the image is the
unit of update, so a new version is a new process.

## Sub-Agents

Sub-agents are child executions launched by a Bobi Agent runtime. Use
them for delegated work and workflow steps.

```bash
bobi agent <name> subagents launch -w adhoc --role engineer --task "Fix CI"
bobi agent <name> subagents launch -w adhoc --role engineer --wait --task "Fix CI"
bobi agent <name> subagents launch -w adhoc --role monitor --as-check --task "Check prod"
bobi agent <name> subagents list
bobi agent <name> subagents show <id>
bobi agent <name> subagents cancel <id>
```

`--wait` blocks until the launched adhoc agent completes. It requires
`-w adhoc`: waiting works by running the task as one prompt, while a multi-step
workflow returns as soon as it is dispatched, so there is no run to join.
`--as-check` is the explicit short-lived monitoring-check harness; it prints
verdict JSON and is the only `subagents launch` mode that accepts
`--post-event`.

To fan out and join without burning a turn per check, start the units in the
background and block on all of them in a **single** shell command:

```bash
bobi agent <name> subagents launch -w adhoc --role engineer --wait \
  --task "Review bobi/workflow/" > /tmp/r1.log 2>&1 &
bobi agent <name> subagents launch -w adhoc --role engineer --wait \
  --task "Review bobi/brain/" > /tmp/r2.log 2>&1 &
wait; tail -20 /tmp/r1.log /tmp/r2.log
```

Polling a log in a loop instead is the pattern this replaces: one real engineer
session spent 79 of its 201 turns doing exactly that (#845).

`--model` and `--effort` override the launched agent's model and reasoning
effort for the whole run (provider-native values; they win over workflow step
and role config), so an agent can pick both dials per delegation:

```bash
bobi agent <name> subagents launch -w adhoc --role engineer \
  --model gpt-5.6 --effort xhigh --task "Design the migration"
```

## Telemetry

Record agent-authored metrics and logs to an OTLP endpoint. The agent chooses
what is worth recording; bobi stamps the fleet identity labels.

```bash
bobi agent <name> otel check
bobi agent <name> otel check --send
bobi agent <name> otel metric tickets.processed 42
bobi agent <name> otel metric queue.depth 7 --kind gauge --attr queue=inbox
bobi agent <name> otel log "reconciled the backlog" --severity info
```

The destination comes from `OTEL_EXPORTER_OTLP_ENDPOINT` (see `docs/OTEL.md`);
`check` reports an unconfigured box as such and makes no network call without
`--send`. `--attr` values are always sent as strings and must stay
low-cardinality - they become time-series labels. Opt in per team with
`tool_library: [otel]`; it is deliberately not in every agent's default prompt.

## Package Surfaces

Installed package files live under `run/package/`:

```text
package/
├── agent.yaml
├── agent.md
├── roles/<role>/ROLE.md
├── tools/*.md
├── workflows/*.yaml
├── monitors/defaults.yaml
└── context/*.md
```

Edit the source under `$BOBI_HOME/agents/<name>/src/` or the
user-chosen source directory, then reinstall. Runtime state and
credentials live under `run/` and should not be edited into package
source.

## Common Tasks

```bash
# Create a new Bobi Agent interactively
bobi setup support

# Install a checked-out team source
bobi agents install ~/agent-teams/support --name support

# Run and talk to it
bobi agent support start
bobi agent support ask "summarize the current queue"

# Inspect operation
bobi agent support status
bobi agent support events
echo '{"title":"x"}' | bobi agent support events publish alert/firing
bobi agent support transcript show manager
```

## Rules of Thumb

- Use the `agents` command group for machine-wide Bobi Agent management.
- Use the named `agent` command group for runtime operations.
- Use `subagents` for child agent executions.
- Put source-controlled team definitions in `src/` or another explicit
  source directory.
- Treat `run/package/` as generated install output and `run/state/` as
  mutable runtime state.
