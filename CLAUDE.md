# modastack

Event-driven AI engineering team. A persistent Claude Code manager monitors Linear, GitHub, Slack, and engineer sessions — assigning work, routing phases, answering questions, and communicating with humans.

## Install

```bash
brew tap moda-labs/modastack
brew install modastack
```

Works on macOS and Linux. For development, clone and install in editable mode:

```bash
cd ~/dev/modastack
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
modastack init --non-interactive
```

## First-time setup (agent guidance)

When setting up dispatch for a user, you MUST ask them for information.
Do NOT guess or skip these steps.

### Step 1: Install

```bash
brew tap moda-labs/modastack
brew install modastack
modastack init --non-interactive
```

### Step 2: Setup the repo

Ask the user TWO things:

1. "What's your Linear API key? You can create one at
   https://linear.app/settings/api → click 'Create key'."

2. "What's your Linear project key? This is the prefix on your issue
   IDs (e.g., if issues look like ENG-42, the key is ENG)."

Then run:
```bash
modastack setup --linear-key <API_KEY> --linear-project <PROJECT_KEY>
```

This stores the API key per-project (in ~/.modastack/credentials.yaml,
not in the repo) and registers the repo in ~/.modastack/config.yaml.

### Step 3: Verify

Show the user the registered repo entry and ask if the detected
Linear project and skills look correct.

### Important

- NEVER guess the Linear project key — always ask
- NEVER guess the Linear API key — always ask
- Credentials are per-project, stored in ~/.modastack/credentials.yaml
- All repo config lives in ~/.modastack/config.yaml (nothing in the target repo)

## Commands

```bash
modastack start                # start event loop
modastack agent --repo R --task T   # launch an ad-hoc engineer agent
modastack agent --workflow W --repo R --issue I  # run a workflow
modastack workflow list        # list available workflows
modastack monitor list         # list background monitors (merged across tiers)
modastack monitor add <name>   # add a monitor (--interval, --description, --repo)
modastack monitor pause <name> # disable a monitor
modastack monitor remove <name>  # remove a user-added monitor
modastack status               # show active engineer sessions
modastack events               # show recent events from the bus
modastack message "text"       # inject a message into the manager session
modastack consult "question"   # ask the manager a question, block until response
modastack init                 # initialize global config
modastack setup [path]         # set up a repo — generate config, store credentials, register
modastack register <target>    # register a repo (local path or org/repo)
modastack repos                # list registered repos
modastack doctor               # health-check /browse (Playwright, Chromium sandbox, daemon)
```

## Architecture

Modastack is a generic event-driven agent framework. Events from
GitHub, Linear, Slack, and other sources flow through a Cloudflare
event server to a persistent Claude Code manager session. The manager
receives ALL events and decides what to do — spawn an agent, run a
structured workflow, or handle it directly.

The framework has no domain opinions. All domain-specific behavior
(what kind of work to do, how to do it, what workflows to run) comes
from per-repo `.modastack/` configuration.

```
modastack/                        # Framework (Python package)
├── cli.py                        # Click CLI entrypoint
├── config.py                     # Global + per-repo config loading
├── session.py                    # Legacy tmux session management
├── setup.py                      # Repo setup — auto-detection
├── board_setup.py                # Bootstrap Linear board with workflow states
├── prompts/                      # Framework base prompts (no domain logic)
│   ├── manager_base.md           # Event format, Slack threading, agent spawning
│   ├── agent_base.md             # Handoff mechanics, manager communication
│   └── resolver.py               # Agent prompt resolution from repo config
├── manager/                      # Persistent manager + event system
│   ├── session.py                # Manager SDK session (start, resume, inject)
│   └── events/
│       ├── bus.py                # Thread-safe in-process event queue
│       ├── consumer.py           # Drain bus → write events file → trigger manager
│       ├── pollers.py            # Background threads: workers (5s), Linear (30s), Slack (10s)
│       ├── webhook_server.py     # HTTP endpoints: /webhooks/github, /linear, /slack
│       └── slack_socket.py       # Slack Socket Mode WebSocket client
├── workflow/
│   ├── orchestrator.py           # DAG executor with deterministic routing
│   ├── triggers.py               # Workflow discovery, three-tier resolution
│   └── schema.py                 # WorkflowDef, StepDef (with agent: field), YAML parsing
└── monitors/                     # Background polling to fill webhook gaps
    ├── schema.py                 # Monitor record + interval parsing
    ├── registry.py               # Three-tier load/merge + writes
    ├── checks.py                 # Native check runners (pr_conflicts, stale_prs)
    └── scheduler.py              # Interval scheduler, dedup, synthetic event injection

workflows/adhoc.yaml              # Only built-in workflow (generic pass-through)
monitors/defaults.yaml            # Empty — domain monitors go in repo config

.modastack/                       # This repo's own config (dogfooding)
├── config.yaml                   # Task tracking, test command, credentials
├── manager.md                    # Engineering manager role prompt
├── agents/
│   └── engineer.md               # Engineer agent: standards, conventions, phases
├── workflows/                    # Engineering lifecycle workflows
│   ├── issue-lifecycle.yaml      # triage → spec → implement → PR
│   ├── pr-feedback.yaml          # address review comments
│   ├── build-failure.yaml        # fix CI failures
│   ├── pr-merged.yaml            # post-merge cleanup
│   └── stall-recovery.yaml       # recover stuck agents
└── monitors.yaml                 # PR conflict + stale PR checks
```

### Per-repo configuration

Repos bring their own `.modastack/` directory:

```
<repo>/.modastack/
├── config.yaml                   # connections, tracker type/auth, test command
├── manager.md                    # domain-specific manager role prompt
├── agents/
│   └── <role>.md                 # agent role prompt(s)
├── workflows/
│   └── <workflow>.yaml           # domain-specific workflow definitions
└── monitors.yaml                 # domain-specific monitor definitions
```

The framework loads `manager_base.md` + repo `manager.md` for the manager,
and `agent_base.md` + repo `agents/<role>.md` for each agent. Workflow
steps specify `agent: <role>` to select which agent prompt to use.

## Issue lifecycle

The manager matches incoming events against workflow trigger descriptions
(natural language conditions) to decide what to do:

| Condition | Action |
|---|---|
| Issue assigned that needs code changes | run `issue-lifecycle` workflow |
| Engineer session state changes | read handoff, run next workflow step |
| Pull request merged | run `pr-merged` workflow |
| Reviewer requests changes | run `pr-feedback` workflow |
| CI check fails | run `build-failure` workflow |
| Engineer session stalls | run `stall-recovery` workflow |
| Human replied | inject answer into engineer session |

Internal phases (triage, spec, implement) happen within "In Progress".
Per-step handoff files in the session directory track sub-phase state.
Linear doesn't need to know.

## Handoff contract

Each workflow step writes a handoff file at
`~/.modastack/sessions/<session-name>/handoff-<step>.yaml`:

```yaml
complexity: medium
needs_spec: true
notes: "Requires API changes"
```

Each agent reads the handoff, does its work, then goes idle. The
manager detects state changes via the worker poller and routes to
the next skill.

## Custom workflows

Workflows are YAML DAGs loaded from three tiers (most specific wins):
1. `<repo>/.modastack/workflows/` — repo-specific overrides
2. `~/.modastack/workflows/` — user-level overrides
3. `<modastack>/workflows/` — built-in defaults

Per-repo context from `.modastack/config.yaml`'s `context:` section is
available as `${{repo.key}}` in workflow templates. See `docs/CUSTOM_WORKFLOWS.md`
for the full reference and `workflows/examples/` for non-dev examples
(content review, research).

## Background monitors

Monitors are scheduled polling tasks that fill webhook gaps — conditions
no webhook fires for (merge conflicts, stale PRs, deploy health). A monitor
is a small human-readable YAML record (`name`, `description`, `interval`,
`event`) loaded from three tiers, later tiers overriding earlier by `name`:

1. `monitors/defaults.yaml` — built-in, shipped, read-only (empty by default)
2. `~/.modastack/monitors.yaml` — user globals (apply to all repos)
3. `<repo>/.modastack/monitors.yaml` — repo-specific

Set `enabled: false` on a repo-specific entry to opt that repo out of an
inherited monitor. The scheduler (a thread in the manager process) runs each
monitor on its interval, deduplicates detected conditions against
`~/.modastack/monitor_state.json`, and injects a synthetic event onto the
same queue webhooks use — so the manager routes it like any other event.

A monitor with a `check:` field uses a native runner in
`modastack/monitors/checks.py` (deterministic, deduplicated). Without one,
the scheduler launches a short-lived, non-interactive check agent out-of-band
(`modastack agent --wait --task "..." --post-event <event>`): it performs the
check from the `description`, captures the result, and posts an event back to
the bus *only* if it finds something. The manager never sees the check
process — only the resulting finding — so its context stays clean and
responsive. Engineering-specific monitors (PR conflicts, stale PRs) are
configured in `.modastack/monitors.yaml` — see this repo's own config.

## Releasing

1. Bump `version` in `pyproject.toml`
2. `git tag v<version> && git push --tags`
3. GitHub Actions publishes to PyPI and auto-updates the Homebrew formula

The publish workflow (`.github/workflows/publish-pypi.yml`) triggers on `v*` tags.
The Homebrew tap (`moda-labs/homebrew-modastack`) updates automatically via repository dispatch.

## Tests

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ --ignore=tests/integration/  # unit tests (~30s)
pytest tests/                              # all tests including integration (~5min)
```

Integration tests drive real Claude Code sessions. Run them before
pushing to main or opening a PR — not on every edit.

**Production bug = integration test gap.** Any time an issue is found
in production, write or update an integration test that covers that
scenario before fixing the code. The test must fail without the fix
and pass with it. No exceptions — if it broke in prod, it means our
tests didn't cover that path.
