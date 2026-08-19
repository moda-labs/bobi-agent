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
workflow has a name, a human-readable `trigger`, an optional `description`, an
optional `period` (`hourly` / `daily` / `weekly` / `monthly` - one run per
period across every dispatch path, see **Execution model**), and an ordered
list of `steps`:

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
1800) is the step's wall-clock budget, and it has exactly one enforcement point:
it gates whether a turn-cap restart is allowed to **start** (see [Turn
budget](#turn-budget) for the resulting bound). Nothing interrupts a drain already in
flight on it. It is **not** the value the reconciler's dead-man check uses —
that check reads the **run-level** timeout (`run_workflow`'s own parameter,
default 3600), which covers the whole run, not any single step.

`model` is optional. When omitted, a prompt step uses the acting role's
configured model (`roles.<role>.model` in `agent.yaml`), then the team default
(`brain.model`), then the provider default. An explicit `--model` on
`subagents launch` wins over all of them for the whole run:

```yaml
brain:
  kind: codex
  model: gpt-5-codex
roles:
  prospect-targeter: {model: gpt-5-mini}
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

`effort` is the model's sibling dial (#778): the reasoning effort for the
step, with the identical precedence chain - `--effort` launch flag > step
`effort:` > `roles.<role>.effort` > `brain.effort` > provider default. Values
are provider-native and pass through untranslated: codex accepts `none`,
`minimal`, `low`, `medium`, `high`, `xhigh`; claude accepts `low`, `medium`,
`high`, `xhigh`, `max` (so `low`-`xhigh` is the portable subset). A value the
brain doesn't know is NOT translated or caught by bobi at session start:
codex fails the first turn with a 400, and the claude CLI warns and silently
runs on its default effort - config validation (`bobi agent <name> doctor`,
and the check at agent start) warns about config and step values the
configured brain does not accept, to catch typos early.

```yaml
steps:
  - name: implement
    agent: engineer
    effort: xhigh
    prompt: "Implement the change with tests..."
```

Model changes are prompt-step boundaries. When a workflow reaches a prompt
step whose model differs from the session's current model, the engine
continues the same session natively on the new model when the brain supports
it (Claude does), keeping the full conversation. On brains without that
capability, or when the step switches back to the provider default, it falls
back to a fresh session seeded with the accumulated workflow context, so the
handoff chain remains intact either way. Note that
native continuation carries the full transcript into the new model's context,
so a step that switches a long conversation onto a pricier model pays for
that history in input tokens.

Effort changes are cheaper boundaries: effort never affects
continue-vs-fresh. A step that changes only the effort reconnects the same
session natively under the new dial on every brain, keeping the transcript
whenever a resumable session id exists (the rare fallbacks that clear it - a
stale resume, a session that never reported an id - re-seed a fresh session
from the workflow context, exactly as a model switch would).

An `agent:` change is **not** on its own a session boundary. The engine only
rebuilds the session when a step changes `model`, `effort`, or `max_turns`; a
step that switches `agent:` while all three of those match continues in the live
session and inherits the previous agent's transcript under its own system
prompt. That is a known gap, not an intended behavior - a reviewer step
following a builder step at identical dials sees the builder's reasoning. When a
step must start clean, give it an explicit dial change (a different `model`,
`effort`, or `max_turns`): that enters the rebuild branch, and an agent change
inside it always starts fresh rather than resuming natively.

### Turn budget

`max_turns` caps how many turns one session may take. Precedence mirrors the
other dials, minus a launch flag - it is a safety backstop an operator
configures, not a per-invocation choice: step `max_turns:` >
`roles.<role>.max_turns` > `brain.max_turns` > the framework default (1000).

The cap is a construction-time CLI flag, so a step that changes it rebuilds the
session - and because the cap never changes the model, that rebuild resumes the
**same transcript** natively, exactly as an effort-only change does. No
conversational context is lost, so a per-step cap costs nothing but a
reconnect.

```yaml
brain:
  kind: claude
  max_turns: 1500          # team default
roles:
  monitor: {max_turns: 8}  # a one-verdict agent that needs hundreds is broken
steps:
  - name: implement
    agent: engineer
    max_turns: 2000        # this step only
```

The real budget for a long session is its wall-clock `timeout`; the turn cap
exists to bound a runaway loop. Size it well clear of honest work - a
Bash-heavy build step spends turns on ordinary tool use, and the framework
default was raised from 200 to 1000 after two real engineer sessions were
killed on turn 201, hours inside their 6h timeouts (#845).

Hitting the cap is **not** terminal for a prompt step. The transcript is
intact and the session id is valid, so the engine restarts the step on that
id - a fresh CLI process with a fresh turn budget - up to
`MAX_TURN_BUDGET_RESUMES` times and only while the step's own `timeout` has
time left. Each restart is logged to the session log as a
`turn_budget_resume` record, and the final restart tells the agent to write
its handoff immediately. When the restarts are exhausted the step fails with
the brain's own diagnosis (`max_turns_reached (max=…, turns=…)`) in
`state.json` and the `agent/session.failed` event.

Know the resulting bound before raising either number. `step.timeout` gates
whether a new resume **starts**; nothing enforces it against a drain already
running, so a resume begun just under the deadline still gets a full fresh
budget. Worst case for one prompt step is therefore
`max_turns × (MAX_TURN_BUDGET_RESUMES + 1)` turns, ended in-process only by the
agent finishing. The dead-man reconciler is the outer net.

### Route step

Deterministic branch, no LLM. Evaluates `if:` and jumps to `goto:` when true or
`else:` when false. Both targets are step names.

```yaml
  - name: route
    if: "needs_spec == true"
    goto: spec
    else: implement
```

Route steps that jump to themselves or an earlier step are capped at 3 visits by
default. Use `max_iterations` or its alias `max_visits` to set a different
positive integer cap. When the cap is exhausted, the workflow fails unless
`on_exhausted` names a later step to continue with.

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
to the requester's channel and thread. Notification failures are normally
non-fatal: they are logged and the workflow continues.

The one exception is a notify step immediately followed by an await step. If
that notification is undeliverable (no token, no channel, or a failed Slack
post), the run **fails** with `workflow.notify_undeliverable` rather than
arming the await - the engine refuses to suspend a run waiting on an approval
nobody was actually asked for.

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

A route step's `if:` cannot guard an action step: the engine checks `condition`
before `action`, so a step carrying both is treated as a route and its action
never runs. A destructive action therefore carries its own guard rather than
relying on where it sits in the step list. `cleanup_worktree` is the worked
example (#1004): it re-reads the PR's merge state from GitHub and deletes
nothing unless that live read says merged - never the `input.merged` the run
was launched with, which is the webhook's snapshot and stale by arrival. It
publishes the verdict it acted on as `merged_live`, and downstream routes key
off that, so the branch taken cannot disagree with what happened on disk.

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

That flat namespace holds **step outputs only** - handoff fields and native
action results. `input.*` is NOT in it, so a bare `merged` does not reach
`input.merged`; reference it as `${{input.merged}}` or route on a step output
instead. An unresolved bare name is not an error: the parser treats it as a
string literal, so `if: "merged == true"` quietly compares `"merged"` to
`"true"` and takes the `else` branch forever. This is what left `pr-closed`'s
`close-issue` step dead until #1004. When a route never seems to fire, check
that its names are step outputs.

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

   The run key is the launch's `--id`. Without one, `launch_agent` derives it
   from the launch's own dials: workflow, project, role, model, effort and task
   text (`subagent.derive_run_key`). An identical launch therefore lands on the
   same session name and is refused by the admission check while the first is
   active, so duplicate suppression is the default rather than something each
   caller opts into (#850). A derived key also starts a fresh transcript, and
   refuses to take over a suspended (`waiting`) run; `--id-random` opts out of
   the derivation entirely for deliberate parallel fan-out.

   A workflow that declares `period:` (`hourly` / `daily` / `weekly` /
   `monthly`) owns its run key outright (#1048). Admission derives the key
   from the workflow name and the current period bucket -
   `daily-standup-2026-08-10` - and **overrides** any caller `--id` or
   `--id-random`, so a scheduled tick, a manual catch-up, and an event
   reaction all land on ONE run identity per period. Buckets use the host's
   local time, deliberately (monitor `at:` schedules are local); every
   dispatcher is assumed to share the host clock. The period is scoped per
   repo, like the session name: two repos served by one installation each
   get their own run per period.

   The run ledger (below) is then consulted, under a cross-process file
   lock: a completed entry for the period refuses a relaunch (`--fresh` is
   the deliberate operator escape hatch to run a period again); a `running`
   or torn-`resuming` entry blocks only while a live process actually holds
   the session - a run that died without a terminal status is closed and
   its entry flipped to `failed`, so a dead run can never block the period.
   A `waiting` entry DOES hold the period: it is a healthy parked gate, and
   the refusal says so - it resumes on its event, or the operator closes
   the run from the console runs view to release the period.
2. **Seed context.** Build the `VariableContext` with the `input`,
   `requested_by`, and (if used) `worktree` scopes.
3. **Open the run's ledger entry.** Every run gets a `WorkflowRun` record
   (`bobi/workflow/state.py`) at start, not only the ones that later suspend
   (#1048). If the previous run under this key **failed** at a checkpoint, the
   entry is adopted instead: the persisted scopes are restored and the step
   loop starts at `checkpoint_step`, so a retry resumes where the failure
   happened rather than replaying completed steps (`fresh` opts out, exactly
   as it does for the transcript).
4. **Run the step loop** in `_run_workflow_async`. Walk steps by index.
   Route/action/notify steps execute inline and advance. Prompt steps inject,
   drain the response, validate the handoff, and capture outputs. After each
   completed prompt/action/notify step the ledger entry is checkpointed - the
   next step index plus the full variable scopes. Await steps flip the same
   entry to `waiting` and return.
5. **Terminate honestly.** A `finally` block emits the truthful terminal event,
   `agent/session.completed` on success or `agent/session.failed` (carrying the
   error) on any failure path, and durably records the matching terminal status
   in the registry. The ledger entry is closed the same way - `completed`, or
   `failed` with its checkpoint kept for the next retry. A suspended run is
   *not* terminal: it skips this entirely and stays `waiting`.

The brain session opens **lazily, at the first prompt step that executes**, and
is then reused across all prompt steps, so the agent keeps full context. The
engine drains exactly one turn per prompt (`_drain_response`, a thin adapter
over the shared `bobi/brain/turns.py` drain primitive, #1048) and saves the
returned session ID so a resumed run can pick the same conversation back up. A
workflow whose reachable steps are all deterministic opens no brain session at
all.

**`connect()` is never a turn (#1016).** Opening a session delivers no text, so
no execution point exists before step 0. The launch task is not an instruction
the agent acts on directly: it reaches the agent as a labelled YAML context
block (`input.task`, alongside the other scopes) prepended to the **first
prompt step's** prompt — on a fresh transcript, and on a resumed transcript
when a new dispatch arrives. The same fold delivers the persisted scopes when a
mid-run model/agent switch starts a fresh session; no turn is spent on context
injection anywhere. Before this invariant, the raw task text drained as a full
tool-enabled agent turn *before* step 0, which is how one catch-up dispatch of
`daily-standup` published two standups. If a run completes without any prompt
step executing, the engine says so — it emits
`agent/workflow.brief_undelivered` rather than letting the launch brief vanish
silently.

A launch is admitted before any of this runs. `max_launch_depth` bounds how
deep a chain of runs launching runs can go, and a run launching a named
workflow already in its own chain is refused outright - see **Launch caps** in
`BUILDING_AGENT_TEAMS.md`. The cap is not workflow-specific: it sits on the
launch pipeline, alongside `max_concurrent_agents` and `spend_cap`.

## Suspend and resume

Await steps make workflows durable across long waits (a human approval, a CI
build, a downstream PR event) without holding a live process.

**On suspend**, the engine flips the run's own ledger entry - the
`WorkflowRun` record opened at launch (#1048) - to `waiting` at
`$BOBI_HOME/state/workflow/runs/<run_id>.json`. Suspension is a state of the
run, not a second record. The entry captures everything needed to continue:
`workflow_name`, `suspended_at_step` (the index of the *next* step),
`await_event`, `session_name`, the full `variable_scopes`, `repo`, `cwd`, and
`run_key`. Writes are atomic (temp file then rename) so a process killed
mid-write cannot leave a truncated record.

**On resume**, a suspended run is picked up by
`bobi agent <name> workflows resume <run_id>`. Resume is **manual today**: the
event-driven entry point `try_resume_for_event(event_type, run_key, event, repo)`
exists in `bobi/workflow/orchestrator.py` and is covered by tests, but nothing in
the runtime calls it, so a run that suspends on `await:` stays `waiting` even
once its awaited event arrives. Wiring it up is tracked as part of the
[checklist-execution model](../plans/2026-07-26-checklist-execution-model.md),
which proposes removing the await/resume feature rather than repairing it.

The resume path itself works as follows. It looks up a waiting run matching the
event type, run key, and repo (`WorkflowRun.find_waiting`). To avoid two processes
resuming the same run, the caller must first `claim()` it: an atomic rename of
`<run_id>.json` to `<run_id>.resuming.json`. Exactly one caller wins; the others
get `FileNotFoundError` and back off, having written nothing at all. The winner
then writes its updated `resuming` status over the claimed file. That second
write is a separate step, and a crash between the two leaves
`<run_id>.resuming.json` still reading `waiting`: `find_waiting` keeps matching
it while `claim()` can never succeed again, so the run is unresumable until the
file is removed by hand. Closing that window needs a recovery protocol for a
torn claim; it is recorded rather than built because nothing in the runtime
calls this path today (see above). The winner re-stamps the run's registry
entry with its own pid and a fresh `started_at`/`timeout` (the resume
`--timeout`), so the dead-man reconciler judges the resumed process on its own
budget rather than the long-dead launch process's. It then restores the variable
context, injects the triggering event under the `event` scope, and re-enters
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
