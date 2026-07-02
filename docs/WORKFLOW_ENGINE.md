# Workflow Engine

A workflow is a linear sequence of steps that one agent session walks through
from start to finish. The workflow engine is the deterministic state machine
that drives that session: it injects each step's prompt, validates the agent's
output against a contract, branches on the result, suspends to wait for external
events, and resumes when they arrive.

The engine itself has **no LLM**. It is pure Python. The agent does all the
work using its tools; the engine decides what to ask next and when the run is
done. The code lives in `bobi/workflow/`.

## Mental model

An event arrives, the manager picks a workflow by name, and the engine walks its
steps one at a time until the workflow ends or hits an `await` step:

```
  event ──► manager picks workflow ──► run_workflow()
                                           │
                                           ▼
                                      step 1 ──► step 2 ──► ... ──► done
```

Each step runs to completion before the next begins. Most steps just advance to
the next one; a `route` step can jump elsewhere, and an `await` step pauses the
whole run until an external event resumes it:

```
  ...running... ──► await step ──► save state, stop  (no live process)
                                        ╎
                  external event ───────╎──► resume from the next step
```

Two things hold across the entire run:

- **One agent session.** The same Claude Code session is reused for every step,
  so what the agent learns in `setup` carries into `pickup`, and `pickup`
  insights carry into `implement`. One registry entry, one log file, one
  session ID per run.
- **The engine has no LLM.** It is pure Python deciding which step is next. The
  agent does the actual work, but only during `prompt` steps; `route`, `notify`,
  `action`, and `await` steps run as plain code with no model call.

## Anatomy of a workflow

Workflows are YAML files. They ship inside an agent team package under
`workflows/` and resolve at runtime exclusively from the installed pack image at
`$BOBI_HOME/agents/<name>/run/package/workflows/` (see `triggers.py`). A
workflow has a name, a human-readable `trigger`, an optional `description`, and
an ordered list of `steps`:

```yaml
name: issue-lifecycle
trigger: When an issue is assigned and requires code changes.
description: >
  Full engineering lifecycle for code changes: worktree setup, triage,
  optional spec phase with approval gate, implement, open PR, QA.

steps:
  - name: setup
    agent: engineer
    prompt: |
      Create a git worktree for this issue and set up the workspace.
    handoff:
      required: [worktree]
    timeout: 300
```

The `trigger` and `description` are not just docs. The dispatcher renders every
loaded workflow into a menu (`WorkflowDispatcher.format_workflow_menu`) that the
manager reads to decide, semantically, which workflow fits an incoming event.

## Step types

Every step is one of five kinds, distinguished by which field it sets. Schema
and parsing live in `bobi/workflow/schema.py`.

### Prompt step (the default)

Injects `prompt` into the persistent session, waits for the agent to finish the
turn, then reads a handoff file. This is the only step type that uses the LLM.

```yaml
  - name: pickup
    agent: engineer
    model: sonnet
    prompt: |
      Move the issue to In Progress. Explore the codebase, classify
      complexity, write the handoff.
    handoff:
      required: [complexity, needs_spec]
      optional: [blocked_by, notes, has_frontend]
    timeout: 1800
```

`agent` names the role whose prompt frames the turn. `timeout` (seconds, default
1800) is the declared deadline carried into the registry for the reconciler's
dead-man check.

`model` is optional. When omitted, prompt steps use the team default configured
in `agent.yaml` under `brain.model` or the provider default when no model is
configured:

```yaml
brain:
  kind: codex
  model: gpt-5-codex
```

Set `model` on an individual prompt step when that step should use a different
provider-specific model or alias. For Claude-backed teams, aliases such as
`haiku`, `sonnet`, and `opus` are accepted, as are full Claude model IDs:

```yaml
steps:
  - name: discover
    agent: prospect-targeter
    model: haiku
    prompt: "Find companies matching the wedge..."
```

Model changes are prompt-step boundaries. If a resumed workflow reaches a
prompt step whose model differs from the saved session's model, the engine
starts a fresh session for that step and injects the accumulated workflow
context so the handoff chain remains intact.

### Route step

Deterministic branch, no LLM. Evaluates `if:` and jumps to `goto:` when true or
`else:` when false. Both targets are step names.

```yaml
  - name: route
    if: "needs_spec == true"
    goto: spec
    else: implement
```

### Await step

Suspends the workflow until a named external event arrives. The engine persists
the full run state to disk, emits `agent/workflow.suspended`, disconnects the
session, and returns. The run sits dormant (no process, no cost) until an event
unblocks it.

```yaml
  - name: await_approval
    await: approval
    timeout: 86400
```

### Notify step

Deterministic Slack message, no LLM. Resolves the `message` template and posts
to the requester's channel and thread. Notification failures are non-fatal: they
are logged and the workflow continues.

```yaml
  - name: notify_start
    notify: slack
    message: "Working on #${{input.run_key}}: ${{input.task}}"
```

### Native action step

Runs a registered Python function with no LLM and captures its result dict as
the step's outputs. Actions are registered in `_NATIVE_ACTIONS` in
`orchestrator.py`; today the only one is `cleanup_worktree`.

```yaml
  - name: cleanup
    action: cleanup_worktree
    timeout: 120
```

## Variables and templating

Steps reference data with `${{scope.key}}`. Resolution and condition parsing
live in `bobi/workflow/variables.py`. There is no `eval()`; conditions go
through a small recursive-descent parser.

**Scopes** are named dictionaries on the run's `VariableContext`:

- `input` — `task`, `repo`, `run_key`, plus any `input_fields` from the trigger
  (for example `input.pr_number`, `input.head_branch`).
- `requested_by` — who triggered the run (channel, thread) for notify routing.
- `worktree` — `worktree.path` when the run uses an isolated git worktree.
- `event` — the payload of the event that resumed a suspended run.
- One scope per completed step, named after the step. After the `pr` step
  finishes, `${{pr.pr_url}}` holds its handoff `pr_url` field.

**Filters**: `${{scope.key | lower}}` and `${{scope.key | upper}}`. A reference
to a missing scope or key resolves to an empty string and logs a warning rather
than failing the run.

**Conditions** in route steps use bare names (resolved from a flat namespace of
all step outputs) and support `==`, `!=`, `in`, `not in`, `and`, `or`, `not`,
quoted string literals, list literals, and `true` / `false`:

```yaml
    if: "complexity == 'large' and needs_spec != false"
```

## The handoff contract

A prompt step's `handoff` block is the contract between the engine and the
agent. The engine appends instructions to the prompt telling the agent to write
a YAML file at `<session>/handoff-<step>.yaml` with the named fields:

```yaml
complexity: <value>
needs_spec: <value>
blocked_by: <value>  # optional
```

After the turn, the engine reads that file and checks every `required` field is
present (`_validate_handoff`). If fields are missing, it re-prompts the agent to
fill them in, up to `MAX_HANDOFF_RETRIES` (2). If they are still missing, the
step fails and the workflow fails. Present fields (required and optional) become
the step's output scope and feed downstream routing and templating.

## Execution model

`run_workflow()` (`orchestrator.py`) is the entry point. End to end:

1. **Register.** Compute a deterministic session name
   (`wf-<workflow>-<repo>-<run_key>`), create a git worktree if any step
   declares `worktree: true`, and register one `SessionEntry` in the registry
   with status `running`. Emit `agent/workflow.started`.
2. **Seed context.** Build the `VariableContext` with the `input`,
   `requested_by`, and (if used) `worktree` scopes.
3. **Run the step loop** in `_run_workflow_async`. Walk steps by index.
   Route/action/notify steps execute inline and advance. Prompt steps inject,
   drain the response, validate the handoff, and capture outputs. Await steps
   persist and return.
4. **Terminate honestly.** A `finally` block emits the truthful terminal event,
   `agent/session.completed` on success or `agent/session.failed` (carrying the
   error) on any failure path, and durably records the matching terminal status
   in the registry. A suspended run is *not* terminal: it skips this entirely
   and stays `waiting`.

The session is created once and reused across all prompt steps, so the agent
keeps full context. The engine drains exactly one turn per prompt
(`_drain_response`) and saves the returned session ID so a resumed run can pick
the same conversation back up.

## Suspend and resume

Await steps make workflows durable across long waits (a human approval, a CI
build, a downstream PR event) without holding a live process.

**On suspend**, the engine writes a `WorkflowRun` record
(`bobi/workflow/state.py`) to `$BOBI_HOME/state/workflow/runs/<run_id>.json`. It
captures everything needed to continue: `workflow_name`, `suspended_at_step`
(the index of the *next* step), `await_event`, `session_name`, the full
`variable_scopes`, `repo`, `cwd`, and `run_key`. Writes are atomic (temp file
then rename) so a process killed mid-write cannot leave a truncated record.

**On resume**, the manager calls `try_resume_for_event(event_type, run_key,
event, repo)` when an event arrives. It looks up a waiting run matching the event
type, run key, and repo (`WorkflowRun.find_waiting`). To avoid two processes
resuming the same run, the caller must first `claim()` it: an atomic rename of
`<run_id>.json` to `<run_id>.resuming.json`. Exactly one caller wins; the others
get `FileNotFoundError` and back off. The winner restores the variable context,
injects the triggering event under the `event` scope, and re-enters
`_run_workflow_async` at `suspended_at_step`. Execution continues as if the await
never paused.

## Lifecycle events

The engine emits structured events throughout a run (via `_emit_lifecycle_event`)
so monitors, the manager, and the launcher can track progress and route replies:

| Event | When |
| --- | --- |
| `agent/workflow.started` | Run begins |
| `agent/step.started` | A prompt step begins |
| `agent/step.completed` | Any step finishes, with its outputs |
| `agent/step.failed` | A step fails |
| `agent/workflow.suspended` | An await step suspends the run |
| `agent/workflow.resumed` | A suspended run resumes |
| `agent/workflow.completed` / `agent/workflow.failed` | Run reaches a terminal outcome |
| `agent/session.completed` / `agent/session.failed` | Honest terminal session event for the launcher |

These flow over the same bus as every other event. See `docs/EVENT_SERVER.md`
for the bus and `docs/BUILDING_AGENT_TEAMS.md` for authoring workflows inside a
team package, including `bobi agent <name> workflows validate`.

## Where to look

| Concern | File |
| --- | --- |
| YAML schema and parsing | `bobi/workflow/schema.py` |
| State machine, session lifecycle, step execution | `bobi/workflow/orchestrator.py` |
| Suspend/resume persistence and claim | `bobi/workflow/state.py` |
| Variable resolution and condition parsing | `bobi/workflow/variables.py` |
| Workflow discovery and the manager menu | `bobi/workflow/triggers.py` |
| Native actions (`cleanup_worktree`) | `bobi/workflow/cleanup.py` |
