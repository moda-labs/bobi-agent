# agent-dispatch

Cron-based agent dispatch loop. Scans Linear + Slack for work, spawns Claude Code (or Codex) to implement, reports results back.

Built from scratch to understand the agentic product engineering loop. Takes design decisions from OpenClaw (persistent heartbeat, state machine per issue, CAS dispatch) and Hermes (tiered complexity, prompt templates, cross-repo portability).

## Architecture

```
Every N minutes (cron):

  SCAN          →  Linear API (issues labeled for automation)
                   Slack API (DMs with actionable intent)
                          │
  CLASSIFY      →  Complexity: trivial / medium / heavy
                   (from .dispatch.yaml rules)
                          │
  DISPATCH      →  Spawn `claude -p` with assembled prompt
                   Track PID + branch in state.json
                          │
  CHECK         →  In-flight items: still running? finished? stuck?
                          │
  REPORT        →  Linear comment + status update
                   Slack message in configured channel
```

## Setup

### One-liner (paste into any coding agent)

From inside any repo you want to wire up:

> Set up agent-dispatch for this repo: run `bash <(curl -sL https://raw.githubusercontent.com/underminedsk/agent-dispatch/main/bootstrap.sh)` — this clones, installs, and runs `dispatch setup` in the current directory.

Or if already cloned locally:

> Set up agent-dispatch: run `~/dev/agent-dispatch/bootstrap.sh`

### Manual

```bash
git clone https://github.com/underminedsk/agent-dispatch.git ~/dev/agent-dispatch
cd ~/dev/agent-dispatch
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
dispatch init
```

### Quick reference

```bash
dispatch init              # configure Linear API key + Slack bot token
dispatch register <path>   # register a repo for automated dispatch
dispatch repos             # list registered repos
dispatch cycle             # run one scan/dispatch cycle
dispatch status            # show in-flight work
```

## Per-repo config

Drop `.dispatch.yaml` in any repo to wire it up:

```yaml
linear:
  project: "PROJ"
  trigger_labels: ["agent"]

complexity:
  trivial: "label:typo OR label:docs"
  heavy: "label:feature OR estimate>3"

agent:
  tool: "claude"
  skills: ["review", "ship"]
  max_parallel: 2

verify:
  test_command: "pytest"
  review_required: true

notify:
  slack_channel: "#eng-agents"
```

## Cron setup

```bash
# Run every 5 minutes
* * * * * cd ~/dev/agent-dispatch && python -m dispatch.cli cycle >> ~/.dispatch/dispatch.log 2>&1
```

## State machine

Each work item progresses through:

```
TODO → DISPATCHED → WORKING → AUDITING → DONE
                                  ↓
                               FAILED / STUCK (escalates to human)
```

Key invariant: the worker agent cannot mark its own work as done. `review_required: true` means a separate verification step before closing.

## Design decisions

| Decision | Rationale | Source |
|----------|-----------|--------|
| Cron, not daemon | Stateless between runs, no crash recovery needed | Simplicity |
| State file with CAS | Prevents double-dispatch across overlapping cron runs | OpenClaw |
| Per-repo manifest | Engine is global, config is local. Repo opts in. | gstack model |
| Tiered prompts | Trivial work gets minimal context, heavy gets full methodology | OpenClaw dispatch routing |
| Worker can't self-close | Independent verification prevents "it works on my machine" | OpenClaw linear-plugin |
| Complexity from labels + estimates | Uses data already in Linear, no separate classification step | Hermes |
