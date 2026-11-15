# Spec: one enforceable token boundary per workflow run, native descendants included

> Review copy: the description of issue #1158 (Gate 1). This file mirrors it and is updated with any Gate-1 changes before implementation starts.

Status: **draft for Gate 1** (Luke or Zach approves; no implementation before approval).
Base: `main` at `5ed76cf4` (0.62.1). Not plan-born.
The original report is preserved verbatim at the end of this issue.

## Decisions needed at Gate 1

Each has a recommendation. Approving the spec approves the recommendation unless you say otherwise.

| # | Question | Recommendation |
|---|---|---|
| D1 | Budget unit | Raw tokens. The ledger records uncached input, cache-read input, cache-write input and output separately, and the allowance caps their plain sum. This means no weights in v1, and no dollar or quota equivalence. At weight 1, cache reads dominate: about 97% of the incident's volume. The docs say so, with a sizing recipe. |
| D2 | Teams that configure nothing | **Observe only.** Every run gets a ledger, plus per-call metering shown in the runs view, so operators can size budgets. Nothing is enforced and no capability changes. |
| D3 | Native descendants and concurrency | Where `max_concurrent_agents` or `budget:` is **explicitly set**, live native descendants count toward `max_concurrent_agents`. That is the cap the incident team believed covered them: 9 overlapping children at a cap of 8. A spawn over the cap is denied, never blocked. The manager and monitors stay outside the shared count, as today. Per-team kill switch: `native_delegation: admit \| refuse`. |
| D4 | Identity across continuations | An allowance belongs to the run's session name, so everything that resumes that name shares it. A run allowance **requires the team window (D5)**, the backstop that a new name cannot escape. Inferring "the same work" across different workflow names is not mechanical (see AC6 below), so the reporter should sign off on this. |
| D5 | Team-wide rolling window | `budget.window.tokens` over `budget.window.hours`, charged by every metered call from every Bob session. It is **required whenever `run_tokens` is set** and optional on its own. |
| D6 | Codex | Codex CLI 0.144.5 has native multi-agent, which Bob cannot observe. Where caps are explicitly configured, refuse it mechanically (`features.multi_agent=false`, to be verified), since the adapter cannot enforce caps there. |
| D7 | Delivery | **Two PRs**, each shipping its own docs. PR 1: metering, the observe-only ledger and native admission. PR 2: allowance, window, stops and surfaces. The draft PR opened now carries this spec as `docs/specs/1158-run-containment.md`. |
| D8 | #1131 engine freeze | The engine change is real but bounded. Stops are recorded through the existing failure path (D9), and the turn-cap loop, control reroute and handoff-repair branches charge the same allowance. Metering and admission live in the adapter, drain and launch admission, which #1131 keeps. Zach decides whether this fits the freeze. |
| D9 | How a stop is recorded | As `failed` with `stop_reason`: `budget_run`, `budget_window`, `context_cap`, `unmetered` or `hook_error`. There is no new status. The runs view already treats any unknown status as done (`bobi/webapp/runs.py:119`), while relaunch and the failed tab already handle `failed` (`bobi/workflow/orchestrator.py:399`). |

## What the investigation found

All probes ran on `main` 5ed76cf4 with the real `drain_turn`. The real-Claude probes used the real Claude adapter, built exactly as `orchestrator._make_session` builds a step session, with claude CLI 2.1.311 on haiku at low effort. They ran in a sandboxed env with an access-only credential copy, deleted after each run. Total probe spend was about $0.03.

**F1. The workflow drain never checks context (reproduced).**
A fake client yielded 12 assistant messages, each reporting a 600,000-token per-call context against the persistent driver's 275,000 cap, then a successful result.
`drain_turn` consumed all 12 and returned success, with no stop and no checkpoint.

**F2. Native children bypass every Bob admission check (reproduced through the real adapter).**
The model called the CLI's built-in `Agent` tool (alias `Task`), and a child ran.
`check_concurrency`, `wait_for_slot`, `count_active_agents`, `check_spend_cap` and `record_invocation` were each wrapped with a counter: zero calls.
The child has no registry entry, so `max_concurrent_agents` cannot see it.

**F3. By default a child can outlive the turn that spawned it (new).**
The CLI ran the child as a background task: `TaskStartedMessage`, `TaskUpdatedMessage`, `TaskNotificationMessage`.
The parent's turn returned its result while its own text said "The subagent is still running".
With no prompt sent, the CLI then started a second turn on its own: a new model call, then a second result saying `PARENT-SAW CHILD-OK`.
`drain_turn` returns at the first result, so it never sees that turn or its usage.
Inferred, not yet run through the orchestrator: a session that stays connected across steps would hand that leftover turn to the next step's drain, putting the turns out of step.

**F4. What the stream reports, and where double counting hides.**

- Per call:
  - Each `AssistantMessage` carries one call's `usage` and a `message_id`.
  - The SDK repeats it once per content block (seen: identical usage twice).
  - The adapter already de-duplicates `BrainInvocation` by `message_id` (`bobi/brain/claude.py:355-372`), but yields `AssistantText.usage` raw (`:411-414`).
- End of turn:
  - `ResultMessage.usage` is the main thread only.
  - `model_usage` includes the child: cache writes were parent 10,588 plus child 11,236 = 21,824.
  - Both are cumulative per Claude session. Bob diffs them against `state/usage_baselines/claude-<sid>.json`.
  - A missing baseline makes the first resumed turn report zero, by design (`bobi/brain/usage_baseline.py:10-14`).
- Descendants:
  - Without forwarding, a child's text-only calls and all grandchild calls are missing from the stream.
  - `TaskNotificationMessage.usage` holds only `total_tokens`.
  - Streamed per-call output counts are partial; the end-of-turn totals are authoritative.

**F5. Enforcement points that work today (real adapter).**

| Mechanism | Observed |
|---|---|
| `disallowed_tools: ["Agent", "Task"]` | The tool was absent and no child ran. |
| `PreToolUse` hook on `Agent\|Task` returning `deny` | It fired under `bypassPermissions` and blocked the spawn, and the model saw the reason. |
| The same session-level hook, nested spawn | It fired for the parent's spawn (`agent_id` null) **and** the child's spawn of a grandchild (`agent_id` = child). |
| `SubagentStart` / `SubagentStop` | They fired at both depths with `agent_id` and `agent_type`, but no `tool_use_id`. |
| `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` | The child finished inside the turn, and nothing arrived in 20 s after the result. It also disables background Bash (reviewer, from the CLI binary), so the spec does not use it. |
| `CLAUDE_CODE_FORWARD_SUBAGENT_TEXT=1` | Every child and grandchild call reached the stream, each with `parent_tool_use_id`, `message_id` and per-call usage. |
| Process layout | Children run inside the CLI process (no extra pids at result time). |

**F6. Code facts that shape the design** (`main` 5ed76cf4):

- **No usage record for workflow runs.**
  - Workflow, adhoc and supervised runs write no usage to the registry. Only `Session._drain_turn` calls `record_cost` (`bobi/session.py:985-1005`).
  - `metrics.db` is off unless `BOBI_METRICS_MODE=enabled`.
  - `WorkflowRun` has no usage fields (`bobi/workflow/state.py:28-57`).
- **No tool restriction for agent sessions.** `ClaudeBrain.make_session` sets only `permission_mode: bypassPermissions` (`bobi/brain/claude.py:913-941`). `skills: all` loads the user and project settings, custom `.claude/agents/*` included.
- **Admission sees only Bob launches** (`bobi/subagent.py:1251-1741`). In-run session rebuilds, `workflows resume` and native children bypass it.
- **The concurrency check races.** It (`bobi/subagent.py:1588`) sits outside the cross-process ledger lock (`:1594`).
- **Teardown and reaping:**
  - `_ClaudeSession.abort()` is an immediate SIGKILL of the CLI (`bobi/brain/claude.py:202-223`).
  - `disconnect()` takes the SDK's close path, which ends with SIGKILL.
  - Tool subprocesses run in their own sessions (`bobi/proctree.py:3-7`).
  - `cancel_agent` refuses inside its own tree (`bobi/subagent.py:3274-3277`).
- **Turn caps do not bound a step.** Turn-cap resumes start a fresh CLI with a fresh turn budget, up to `MAX_TURN_BUDGET_RESUMES = 3` (`bobi/workflow/orchestrator.py:83-97`).
- **Launch lineage cannot anchor identity.** It is env-only and never persisted (`docs/BUILDING_AGENT_TEAMS.md:176-183`).

## Solution

1. **Meter every call (PR 1, every team).**
   - Bob Claude sessions pass `--forward-subagent-text` (through `extra_args`, not env, so settings cannot override it).
   - The adapter emits one de-duplicated usage record per `message_id`, from the parent and every descendant.
   - Every run gets a ledger. Teams that configure nothing get observe-only figures.
2. **Admit every child (PR 1, where caps are explicit).**
   - A fail-closed `PreToolUse` gate on `Agent|Task` reserves a slot by `tool_use_id` under the team-wide admission lock, or denies.
   - `PostToolUse` / `PostToolUseFailure` release the slot.
   - Spawners Bob cannot gate are disallowed. Codex multi-agent is refused.
3. **Charge one allowance (PR 2, opt-in).** A durable per-run ledger plus the team window. Charges are persisted per call, so processes sharing a budget see each other within one call. Renewal is an explicit operator action.
4. **Stop safely and say so (PR 2).**
   - The soft threshold works inside the current turn.
   - The hard limit and the context cap close the CLI and reap its tools within a documented bound.
   - The run is recorded as `failed` with a `stop_reason` on every surface.

### What an operator sees

```yaml
# agent.yaml - every key unset = off (no enforcement). Units: raw tokens (D1).
max_concurrent_agents: 8        # explicitly set: native descendants now count (D3)
native_delegation: admit        # admit (gated) | refuse (Agent/Task disallowed)
budget:
  run_tokens: 60000000          # per logical run; requires window
  window: {tokens: 400000000, hours: 5}   # team-wide rolling backstop
  context_tokens: 400000        # per-call context cap (workflow and supervised sessions)
  soft_ratio: 0.9               # at 90%: no new spawns, handoff-only tools
```

```text
$ bobi agent eng-team budget show 1158
run       wf-issue-lifecycle-eng-team-1158   failed (stop_reason budget_run) at step implement
spent     60,412,903 of 60,000,000 tokens  (overshoot 412,903)
by kind   uncached_input 9,120 · cache_read 57,804,113 · cache_write 1,981,300 · output 618,370
calls     1,204 metered · 0 unmetered       native: 7 admitted · 2 denied · 0 live
window    341,000,000 of 400,000,000 tokens in the last 5h (refills from 09:40Z)
next      bobi agent eng-team budget renew 1158 --add 20000000   (nothing restarts)
          bobi agent eng-team subagents launch -w issue-lifecycle --id 1158
```

`budget show` and `budget renew` accept a run key, run id or session name. `budget show --team` shows the window.
`budget renew` is the only way to add allowance. It refuses from inside a Bob session, which is best-effort, since `setsid` can evade it. Every renewal is appended to the ledger and alerts the operator channel.

## Scope

In:
- Forwarding plus per-call metering, with an observe-only ledger for every run.
- The fail-closed native gate with reservations, live counts and the `native_delegation` switch.
- Disallowing spawners Bob cannot gate.
- Codex refusal under explicit caps.
- Fixing the concurrency race.
- Quiescent drain (below).
- The run allowance with the required window, and the context cap.
- Soft and hard stops, close and reap, `stop_reason` recording.
- `budget show` and `budget renew`.
- Engine charge points, and admission on `workflows resume` and relaunches.
- Runs view, CLI and alert surfaces.
- Docs in each PR.

Out:
- Dollar or quota units, and weights. The ledger keeps reported dollars for display only.
- Automatic context-cap rotation inside a step. The stop records a fresh-relaunch path instead.
- Director allowances beyond the window. The manager is never killed for budget: at window exhaustion it starts no new turns and alerts.
- #1154's restart loop and alerting.
- Any deployment's numbers or policy.

## Technical approach

### Accounting identity (AC1, AC2)

- **Per-call records.** The adapter emits one record per distinct `message_id`, from the parent and from forwarded descendants. Reasoning output is a subset of output, never added.
- **Provisional, then authoritative.** Per-call records are provisional and are persisted to the ledger as they arrive.
  - For each Claude session, the ledger holds the last authoritative cumulative snapshot plus all outstanding provisional charges.
  - The next authoritative result from any process for that session id (a turn result or a resumed process's first result) replaces both with `cumulative - last_snapshot`.
  - So a turn killed mid-stream and then resumed is charged once.
  - If the cumulative goes down (the CLI restored less), the ledger re-baselines to it and keeps the provisional charges.
- **Never charged:** parent summaries of child usage (`TaskNotificationMessage.usage`). They are a cross-check only.
- **Unavailable usage.**
  - A turn with no computable delta keeps its provisional charges and is marked `estimated`. Provisional charges are a lower bound, because streamed output is partial, and the surfaces say so.
  - A call with no usage at all counts as `unmetered`.
  - With a budget set, three consecutive unmetered turns stop the run (`stop_reason: unmetered`).
- **Codex** accrues per `turn.completed` delta, so its enforcement granularity is one turn. That is documented.

### Ledger and identity (AC2, AC6)

- **Storage.** `state/budgets/<budget_id>.json`, written with `fsutil.atomic_write_json` under `fsutil.file_lock`, one update per call.
  - It holds the allowance snapshot, taken at run creation, plus totals, per-session snapshots and spawn counts.
  - It also holds the state (`active`, `soft`, `stopped` or `closed`), the stop reason and the renewal log.
- **Identity.** `budget_id` is the session name (`wf-<workflow>-<repo>-<run_key>`), recorded on `WorkflowRun`.
- **What shares one allowance:**
  - turn-cap restarts, handoff repairs, control reroutes and model switches, through the engine charge points;
  - `workflows resume`, await resume and manager restarts;
  - `--fresh` relaunches under the same key.
- **What gets a new allowance.** A run that completes successfully closes its ledger, so a later run under that key is new work with a new allowance.
- **Inheritance.**
  - A launch from inside a budgeted run inherits its `budget_id`. It is carried in env and recorded server-side on the child's registry entry, so clearing the env var does not detach it.
  - `subagents launch --budget <key>` inherits explicitly.
- **Fail closed:** a `WorkflowRun` that names a missing or unreadable ledger is treated as stopped (`stop_reason: budget_run`, detail `ledger_missing`), never as fresh.
- **AC6 honestly stated.**
  - The director sends one issue through several workflows under different names: lifecycle, then PR feedback, build failure or merge conflict.
  - Different names are different runs, so each gets its own allowance. The required window bounds their sum.
  - Making "the same work" one allowance across workflow names needs an identity key that Bob does not have today, such as the issue or PR. That is the Gate-1 sign-off asked in D4.

### Native descendants (AC3)

- **Hooks.** Bob Claude sessions register, alongside the existing defer hook:
  - `PreToolUse` on `Agent|Task`: admission reserves a slot keyed by `tool_use_id`, scoped to the CLI pid and its start token, under the team-wide admission lock, or denies.
  - `PostToolUse` / `PostToolUseFailure` on `Agent|Task`: release by `tool_use_id`.
  - `SubagentStart` with no matching reservation, for example a forked skill: reserve now if capacity allows, otherwise set the stop flag.
- **Fail closed.**
  - Every hook body is wrapped: any exception, or a lock not acquired within 2 s (taken in a worker thread, with an explicit `HookMatcher` timeout), denies the spawn and records `hook_error`.
  - Hooks never stop the session themselves; they set a flag that the drain acts on.
  - Reservations held by a dead CLI are dropped from the count.
- **The concurrency count.** Live registry sessions counted today, plus live reservations of counted sessions. `launch_admission` uses the same counter.
  - The race fix re-checks this count inside the second `ledger_lock` pass (`bobi/subagent.py:1594`), keeping the wait outside the lock.
- **Deny reasons the model sees:**
  - "Concurrency cap reached (8 of 8 in use). Do not retry; continue this work inline or wait for a running agent to finish."
  - "Run token budget nearly exhausted. Do not start new agents; write your handoff to <path> and stop."
- **Spawners Bob cannot gate.** `RemoteTrigger`, `CronCreate` and `ScheduleWakeup` go into `disallowed_tools`, and `CLAUDE_CODE_DISABLE_CRON=1` is set.
  - The init message's tool list is checked against a reviewed allowlist of spawning tools.
  - A session exposing an unknown spawner is refused at start (`stop_reason: hook_error`), and the operator is told.
- **Refuse modes.** `native_delegation: refuse`, or a CLI on which the hooks cannot be registered, falls back to `disallowed_tools: ["Agent", "Task"]`.
- **Leaking child text.** Forwarded descendant text is filtered out of `final_text` and the `response` activity log by `parent_tool_use_id`.
- **Codex** under explicit caps gets `-c features.multi_agent=false` (to be verified in PR 1).

### Quiescent drain (F3)

- `drain_turn` tracks background agent tasks it saw start: a `TaskStartedMessage` with an agent `task_type` and no matching `TaskNotificationMessage`.
- While any are open, it keeps reading past a result, charging the follow-up turn the CLI starts on its own, until none are open and the latest result has arrived. This is bounded by the step timeout and the budget.
- Background Bash tasks are not waited on.
- PR 1 first reproduces F3's out-of-step turns through the orchestrator, as a failing test.

### Per-call context check (AC4)

- The per-call context of a call is `uncached + cache_read + cache_write` input for that one `message_id`. It is the same measure `session._context_fill_tokens` uses (#454), never a turn total.
- On a breach by the parent or any descendant, the drain lets the in-flight call finish, then closes the CLI and records `context_cap`.
- Resuming that transcript would breach again. So `budget show` prints the recovery: relaunch the stopped step in a **fresh** session seeded from the workflow context and `budget-stop.yaml`, the same seeding as an `agent:` boundary.
- `context_tokens` is read live, not snapshotted, so raising it applies to the relaunch.

### Stop, reap, record (AC5, AC7)

- **Soft threshold (`soft_ratio`).**
  - Admission denies spawns and nested launches.
  - A `PreToolUse` hook on `*` denies every tool except writes under the session's handoff path, and the deny reason tells the agent to write its handoff and stop.
  - This happens inside the current turn: no CLI rebuild, no new turn.
- **Hard limit, or context cap.**
  1. The drain stops reading new work.
  2. It snapshots the CLI's process tree (`proctree.kill_set`).
  3. It calls `disconnect()`. The SDK close ladder (stdin EOF, SIGTERM after 5 s, SIGKILL after 5 s) flushes the transcript, and in-process children die with the CLI.
  4. It terminates the snapshot's tool processes with `proctree.terminate` (10 s + 5 s), in a worker thread.
- **Documented bound: everything the run owns is gone within 30 s of the stop decision.**
- **Recording.**
  - The run is recorded as `failed` with `stop_reason` and `error_kind: budget_exhausted` (D9). A soft stop whose handoff completes is still `failed`, because budget stops never read as done.
  - `budget-stop.yaml` in the session dir holds the last assistant text, the handoff path, the spent figures and the exact renew and relaunch commands.
  - Events: `agent/workflow.budget_stopped`, plus `system/budget.window.exhausted`, posted to the operator channel the same way `system/brain.*` is.
- **Maximum in-flight overshoot (documented).**
  - Checks run after each call, against the shared ledger. When the stop lands, every live thread in every process sharing the budget can have one call in flight.
  - Overshoot is therefore at most that number of threads × (per-call context + max output). A context cap bounds the first term.
  - Codex overshoot is one turn per session.

### Surfaces (AC7)

- **`GET .../runs`:**
  - adds `budget: {allowance, spent, remaining, state, stop_reason, estimated, usage_incomplete}`, where `allowance: null` means observe-only;
  - stopped runs sit in the `failed` tab, with a brick "budget stop" chip (the failed family, not violet);
  - the cell reads `41.2M / 60M tok`, with `≥` and an `estimated` badge when incomplete;
  - an example payload goes into `docs/RUNS_VIEW.md`.
- **CLI:** `subagents show` gets `Budget:` lines; `budget show` and `budget renew` are new.
- **Docs.** New `docs/RUN_BUDGETS.md` covers:
  - units;
  - a sizing recipe from observe-only ledgers, with the incident as a worked example;
  - overshoot and reap bounds;
  - identity rules;
  - the unconfigured defaults.

  Also updated: `docs/WORKFLOW_ENGINE.md`, `docs/BUILDING_AGENT_TEAMS.md`, `docs/EVENT_SERVER.md` and `skills/bobi.md`.

### Teams with no budget configured (AC8)

They get:
- per-call metering and an observe-only ledger, shown in the runs view;
- forwarded child text, filtered out of results;
- the quiescent drain.

They do not get native admission, Codex refusal, allowances or stops, unless they set `max_concurrent_agents`, `budget:` or `native_delegation: refuse` explicitly.
This is documented in `docs/RUN_BUDGETS.md`.

## Alternative considered: refuse native delegation (Approach B)

Refusing `Agent`/`Task` in all Bob sessions meets AC3 with no hooks, forwarding or live counts.
It gives up:
- children that start from a small context. These are cheaper than parent calls at the incident's 512K median context, and all 22 incident children were native.
- skills that use the Agent tool and `.claude/agents/*`.

It also pushes fan-out onto heavier Bob launches.
The spec keeps it available per team as `native_delegation: refuse`, which is also the rollback switch for PR 1.

## Edge cases

- **Parallel spawns** each reserve separately under one team-wide lock, so two spawns cannot take the last slot.
- **A child running at the soft threshold** finishes and is charged. The hard limit kills it.
- **The model retries a denied spawn.** It is denied again. The reason says not to retry, and each retry is metered.
- **Custom `.claude/agents/*` subagents** use `Agent` and pass the same gate. Forked skills are caught at `SubagentStart`.
- **A hostile user `~/.claude/settings.json`** cannot turn forwarding off (it is a CLI flag) and cannot add cron tools (they are disallowed). An unknown spawner fails the start check.
- **`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`** is forced to `0`. Out-of-process agent teams are unsupported under containment.
- **A torn or edited ledger** fails closed for a budgeted run. Agents run as the same OS user, so this is a rail, not a security boundary; the docs say so.

## Verification plan

Failing tests first. This extends `tests/test_brain_turns.py`, `tests/test_concurrency_semaphore.py`, `tests/test_spend_governor.py` and the workflow suites.

- **Drain:**
  - F1's replay becomes a test: a configured context cap stops on the first over-cap call, parent or descendant;
  - F3 is reproduced through the orchestrator, then made to pass by the quiescent drain;
  - forwarded child text never reaches `final_text`.
- **Accounting:**
  - duplicate `message_id` fragments count once;
  - resumed cumulatives charge only the delta;
  - kill-then-resume charges once;
  - a decreasing cumulative re-baselines;
  - a missing baseline is `estimated`, not zero;
  - `TaskNotification` totals are never charged.
- **Shared budgets:** two processes on one `budget_id` lose no updates, and a stop in one is seen by the other within one call.
- **Admission:**
  - reservations by `tool_use_id` for parallel and nested spawns;
  - release on post-hooks, and dead-CLI reservations dropped;
  - a hook exception or timeout denies;
  - the race closed: two processes at cap minus one admit exactly one;
  - an unknown spawner is refused at start;
  - unconfigured teams are not gated.
- **Identity:**
  - turn-cap restarts, retries, `workflows resume`, `--fresh` and manager restarts cannot reset spent allowance;
  - a missing ledger fails closed;
  - `run_tokens` without a window fails config validation;
  - renew inside a session is refused and every renewal alerts.
- **Stop and reap:**
  - an orphaned `setsid` tool is reaped;
  - everything is gone within 30 s;
  - the run is `failed` with `stop_reason` in `/runs`, `subagents show` and the event.
- **Negative guarantees:** mutation runs prove the suites fail when the gate, the reservation, the fail-closed wrapper, the persisted spent check or the dedupe is removed.
- **Real-Claude legs**, gated on the CLI and auth, haiku at low effort, with matching stub legs fed injected fake usage:
  1. Reconcile: a generous allowance, a direct and a nested child; the ledger equals the result's `model_usage` delta.
  2. Stop: an allowance of about 50K stops and reaps the run mid-turn.
  3. Deny: a full cap denies the spawn, and the model sees the reason.
  4. Kill then resume charges once.
- **Web UI:** `tests/e2e/test_webapp_ui.py` covers the chip and the cell, with a GIF captured from the real app in the PR.

## Implementation plan

1. **PR 1, meter and native containment.** This meets AC3 for teams with explicit caps.
   - forwarding flag and per-call records;
   - observe-only ledger and runs-view figures;
   - gate, reservations and post-hook release;
   - spawner allowlist and the cron disallow;
   - `native_delegation`;
   - Codex refusal;
   - race fix;
   - quiescent drain;
   - legs 1 and 3;
   - docs.
2. **PR 2, allowance and stops.** It carries `Closes #1158`.
   - config validation (window required);
   - allowance, window and context checks;
   - soft and hard stops;
   - close and reap;
   - `stop_reason` recording;
   - engine charge points;
   - admission on resume and relaunch;
   - `budget show` and `budget renew`;
   - events and alert;
   - surfaces;
   - legs 2 and 4;
   - docs.

## Cost of the root-cause fix

The root cause is structural. The workflow path has no shared, durable token boundary, and native delegation is invisible to Bob's admission.
The parts exist but are not connected:
- per-call usage with `message_id` de-duplication;
- cumulative baselines;
- locked durable state;
- launch admission;
- process-tree reaping.

The fix connects them, with no new process, service or provider call.

| | PR 1 | PR 2 |
|---|---|---|
| Main modules | `brain/claude.py`, `brain/codex.py`, `brain/turns.py`, new `bobi/budget.py`, `subagent.py`, `concurrency_semaphore.py`, `webapp/runs.py` | `bobi/budget.py`, `config.py`, `brain/turns.py`, `workflow/orchestrator.py` (charge points), `workflow/state.py`, `proctree.py`, `cli.py`, events, web UI static |
| Rough size, code + tests | ~700 + ~900 lines | ~900 + ~1,000 lines |
| Runtime overhead | One hook round trip per native spawn; one locked ledger write per model call (about 3K calls per day in the incident) | None beyond PR 1 |
| Capability change | None unless caps are explicit; then denied spawns over the cap | Stops only where a budget is set |

**Payoff, replayed against the incident.**
- The task spent about 510M tokens (86.7% of the day).
- With `run_tokens: 60M` and a 400M five-hour window:
  - each parent run stops at 60M plus the overshoot bound;
  - the three parent runs together stop by the window at 400M, even if they ran under different names;
  - the 22 children are charged to their parents, and spawns over the cap of 8 are denied.

  Whether the three parents actually ran under one name is not known from the report.
- The deployment-side stopgap (lower effort, continuations gated on a human) makes the problem rarer, but cannot bound a run: nothing at the deployment layer sees per-call usage or native spawns.

## Not verified yet

- **Background tasks:** F3's out-of-step turns through the orchestrator, and whether `task_type` separates agent tasks from Bash tasks. PR 1 starts with both.
- **Codex:** the `features.multi_agent` flag in codex-cli 0.144.5.
- **The forwarding flag:** that `--forward-subagent-text` via `extra_args` behaves like the env var probed. Also forwarding at depth beyond two, and under wide parallel fan-out.
- **Other spawners:** forked-skill spawns (`context: fork`) and the `Workflow` tool in Bob sessions, and how settings env interacts with the env Bob passes.
- **Hook failure behavior:** whether a hook timeout lets the call through (one reviewer read the CLI as failing open on callback errors). The wrapper denies before any timeout either way.
- **CLI versions:** behavior on versions other than 2.1.311. PR 1 adds a capability check at session start with the refuse fallback.

## Review record

Revision 1 was reviewed by three independent read-only reviewers (engineering, operator UX and scope lenses, all Claude). Each returned blockers; all blockers were folded into this revision:

- **Stops recorded as `failed` plus `stop_reason`** (D9). All three found that a new status would read as done in the runs view, and that relaunch only adopts `failed` runs.
- **Run allowance requires the window, and AC6 stated as partial** (D4, D5). Reported by scope and engineering.
- **Unconfigured teams observe only.** Native admission applies only where caps are explicit, background tasks stay on and a quiescent drain is used instead, and the `native_delegation` switch was added (D2, D3). Reported by scope and engineering.
- **Gate redesign:** fail-closed hook wrapper, reservations by `tool_use_id`, the spawner allowlist and cron disallow, and the forwarding flag instead of env. Reported by engineering.
- **Accounting:** last snapshot plus outstanding provisional, replaced by the next authoritative result; per-call ledger writes so processes sharing a budget see each other. Reported by engineering.
- **Close then reap:** `disconnect()` instead of an immediate SIGKILL, and the process tree snapshotted first. Reported by engineering.
- **Soft threshold inside the turn, and a fresh relaunch after a context-cap stop.** Reported by design and scope.
- **Surfaces:** the brick chip, the event names, the specified deny texts, and config that is off when unset. Reported by design.
- **Two PRs with docs in each, Approach B, and the payoff replay.** Reported by scope.

Also from scope's minor findings: per-category weights deferred, and the separate wrap-up turn cut.

Not re-reviewed after folding. The `plan_review` step gives this revision an independent adversarial pass.
