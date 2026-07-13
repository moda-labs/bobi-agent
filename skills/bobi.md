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
                                        #   image (needs the bobi-deploy plugin)
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

# Reply into a chat conversation (channel-agnostic; ref comes from the event)
bobi reply <conversation> "markdown text"
bobi reply <conversation> --edit <ts> "text"     # resolve a placeholder
bobi reply <conversation> --file <path> "comment"
bobi read-conversation <conversation> [-n 50] [--json-output]
```

Use `bobi reply` and `bobi read-conversation` for Slack and any other
chat channel delivered through the channel gateway.

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

`--wait` blocks until the launched adhoc agent completes. `--as-check` is the
explicit short-lived monitoring-check harness; it prints verdict JSON and is the
only `subagents launch` mode that accepts `--post-event`.

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
