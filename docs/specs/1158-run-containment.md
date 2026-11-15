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
| D2 | Teams that configure nothing | **Observe only.** Every run gets a ledger, plus per-call metering shown in the runs view, so operators can size budgets. Nothing new is enforced and no capability changes. The default `max_concurrent_agents` of 2 (`bobi/config.py:533`) keeps counting Bob launches only, so native children stay uncounted until the key is set. |
| D3 | Native descendants and concurrency | Where `max_concurrent_agents` or `budget:` is **explicitly set**, each native descendant counts toward `max_concurrent_agents` from its spawn until it ends. That is the cap the incident team believed covered them: 9 overlapping children at a cap of 8. A spawn over the cap is denied, never blocked. The manager and monitors stay outside the shared count, as today. Per-team kill switch: `native_delegation: admit \| refuse`. |
| D4 | Identity across continuations | An allowance belongs to the run's session name, so everything that resumes that name shares it. A run allowance **requires the team window (D5)**, the backstop that a new name cannot escape. Inferring "the same work" across different workflow names is not mechanical (see AC6 below), so the reporter should sign off on this. Not recommended: a `budget.identity: run_key` option. In the eng-team deployment, follow-up workflows run under keys of their own (`1071-review-<id>`, `1114-ci-<hash>`, `pr-1037-conflict`, `adhoc-<hash>`), never the issue's key, so it would not join them either. |
| D5 | Team-wide rolling window | `budget.window.tokens` over `budget.window.hours`, charged by every metered call from every Bob session. It is **required whenever `run_tokens` is set** and optional on its own. An exhausted window is renewed like a run: `budget renew --team --add N`. |
| D6 | Codex | Codex CLI 0.144.5 has native multi-agent, which Bob cannot observe. Where caps are explicitly configured, refuse it mechanically (`features.multi_agent=false`, to be verified), since the adapter cannot enforce caps there. |
| D7 | Delivery | **Two PRs**, each shipping its own docs. PR 1: metering, the observe-only ledger and native admission. PR 2: allowance, window, stops and surfaces. The draft PR opened now carries this spec as `docs/specs/1158-run-containment.md`. |
| D8 | #1131 engine freeze | The engine change is real but bounded. PR 1 adds two calls: `_make_session` merges the shared containment options, and a prompt step waits for a reused session to be quiet before it sends (quiescent drain). PR 2 records stops through the existing failure path (D9), and makes the turn-cap loop, control reroute and handoff-repair branches charge the same allowance. Metering and admission live in the adapter, drain and launch admission, which #1131 keeps. Zach decides whether this fits the freeze. |
| D9 | How a stop is recorded | As `failed` with `stop_reason`: `budget_run`, `budget_window`, `context_cap`, `unmetered` or `hook_error`. There is no new status. The runs view already treats any unknown status as done (`bobi/webapp/runs.py:119`), while relaunch and the failed tab already handle `failed` (`bobi/workflow/orchestrator.py:399`). |
| D10 | The manager and the window | Its calls are metered and charge the window, so the window measures the team's real draw on the subscription. A window stop never silences it: budgeted runs stop and launches are denied, while the manager keeps answering and can report what stopped. The alternatives: block its new turns at exhaustion (the team goes dark until the window refills or an operator renews), or leave its calls out of the window. |
| D11 | Context cap: stop or rotate | **Stop**, recorded as `context_cap`. The ordinary relaunch then restarts the stopped step in a fresh session, seeded from the workflow context and `budget-stop.yaml`. Automatic rotation inside a step is out of v1. The trade: where calls routinely exceed the cap (the incident's median context was 512K), each step stops once and waits for an operator relaunch. `context_tokens` is off unless set. |

## What the investigation found

All probes ran on `main` 5ed76cf4 with the real `drain_turn`. The real-Claude probes used the real Claude adapter, built exactly as `orchestrator._make_session` builds a step session, with claude CLI 2.1.311 and claude-agent-sdk 0.2.178 on haiku at low effort. They ran in a sandboxed env with an access-only credential copy, deleted after each run. Total probe spend was about $0.03.
P1 is a background child, P4 a foreground child that spawns a grandchild.
SDK file paths below are in claude-agent-sdk 0.2.178.

**F1. The workflow drain never checks context (reproduced).**
A fake client yielded 12 assistant messages, each reporting a 600,000-token per-call context against the persistent driver's 275,000 cap, then a successful result.
`drain_turn` consumed all 12 and returned success, with no stop and no checkpoint.

**F2. Native children bypass every Bob admission check (reproduced through the real adapter).**
The model called the CLI's built-in `Agent` tool (alias `Task`), and a child ran.
`check_concurrency`, `wait_for_slot`, `count_active_agents`, `check_spend_cap` and `record_invocation` were each wrapped with a counter: zero calls.
The child has no registry entry, so `max_concurrent_agents` cannot see it.

**F3. By default a child can outlive the turn that spawned it, and the CLI then runs a turn on its own (new).**
P1 recorded this order on one session, with no second prompt sent:

1. The parent calls `Agent`. `TaskStartedMessage` (the task id and the call's `tool_use_id`) follows, then the tool's result, while the child keeps running in the background.
2. The child's call, then `TaskUpdatedMessage` and `TaskNotificationMessage`, both `completed`.
3. The parent's last call, whose text says "The subagent is still running", then the first `ResultMessage`, at 3.5 s.
4. A new `init` at the same 3.5 s mark, a parent call answering `PARENT-SAW CHILD-OK`, and a second `ResultMessage`.

The notification arrived before the parent's last call, yet that call did not see it.
So at the first result no task was open, and the CLI still ran a second turn to deliver the notification.
`drain_turn` returns at the first result (`bobi/brain/turns.py:107-136`), so it never reads that turn or charges its usage.
Prompt steps reuse one session (`bobi/workflow/orchestrator.py:1279`), so the next step's drain would take the leftover turn's result as its own answer, and every later step would read the turn before its own. That last part is inferred from the code, not yet run.

**F4. What the stream reports, and where double counting hides.**

- Per call:
  - Each `AssistantMessage` carries one call's `usage` and a `message_id`.
  - The SDK repeats it once per content block (seen: identical usage twice).
  - The adapter already de-duplicates `BrainInvocation` by `message_id` (`bobi/brain/claude.py:355-372`), but yields `AssistantText.usage` raw (`:411-414`).
- End of turn:
  - `ResultMessage.usage` covers the main thread for that turn only: in P1 each result's figures equal the sum of its own turn's parent calls.
  - `model_usage` includes the child: cache writes were parent 10,588 plus child 11,236 = 21,824.
  - `model_usage` is cumulative per Claude session, and Bob diffs it against `state/usage_baselines/claude-<sid>.json`.
  - A missing baseline makes the first resumed turn report zero, by design (`bobi/brain/usage_baseline.py:10-14`).
- Descendants:
  - Without forwarding, a foreground child's text-only calls and all grandchild calls are missing from the stream (P4).
  - A background child's call did reach the stream without forwarding (P1), tagged with `parent_tool_use_id`. The adapter yields it as ordinary `AssistantText`, so `drain_turn` briefly took the child's text as the turn's final text (`bobi/brain/turns.py:108-113`).
  - `TaskNotificationMessage.usage` holds only `total_tokens`.
  - Streamed per-call output counts are partial; the end-of-turn totals are authoritative.

**F5. Enforcement points that work today (real adapter).**

| Mechanism | Observed |
|---|---|
| `disallowed_tools: ["Agent", "Task"]` | The tool was absent and no child ran. |
| `PreToolUse` hook on `Agent\|Task` returning `deny` | It fired under `bypassPermissions` and blocked the spawn, and the model saw the reason. |
| The same session-level hook, nested spawn | It fired for the parent's spawn (`agent_id` null) **and** the child's spawn of a grandchild (`agent_id` = child). |
| `SubagentStart` / `SubagentStop` | They fired at both depths with `agent_id` and `agent_type`, but no `tool_use_id`. |
| Task messages | `TaskStartedMessage` carries the task id and the spawning `tool_use_id`; `TaskNotificationMessage` carries the task id, `status` and `tool_use_id`. They appeared for the grandchild too (P4). For all three spawns where both were recorded (P1, P4), the task id equaled the `agent_id` the Subagent hooks reported. |
| Background spawn timing | The `Agent` tool's result arrived right after `TaskStartedMessage`, while the child was still running (P1). `PostToolUse` fires when a tool returns, so for a background child it marks the launch, not the end. |
| `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` | The child finished inside the turn, and nothing arrived in 20 s after the result. It also disables background Bash (reviewer, from the CLI binary), so the spec does not use it. |
| `CLAUDE_CODE_FORWARD_SUBAGENT_TEXT=1` | Every child and grandchild call reached the stream, each with `parent_tool_use_id`, `message_id` and per-call usage. |
| Process layout | Children run inside the CLI process (no extra pids at result time). |

**F6. Code facts that shape the design** (`main` 5ed76cf4):

- **No usage record for workflow runs.**
  - Workflow, adhoc and supervised runs write no usage to the registry. Only `Session._drain_turn` calls `record_cost` (`bobi/session.py:985-1005`).
  - `metrics.db` is off unless `BOBI_METRICS_MODE=enabled`.
  - `WorkflowRun` has no usage fields (`bobi/workflow/state.py:28-57`).
- **No tool restriction for agent sessions.** `ClaudeBrain.make_session` sets only `permission_mode: bypassPermissions` (`bobi/brain/claude.py:913-941`). `skills: all` loads the user and project settings, custom `.claude/agents/*` included.
- **No hooks on the workflow path.** `orchestrator._make_session` passes only `max_turns`, `skills`, `mcp_servers`, `model` and `effort` (`bobi/workflow/orchestrator.py:776-789`). The only hook in Bob is the `AskUserQuestion` defer hook on the supervised launch path (`bobi/subagent.py:357-375`, `:431`).
- **One read per prompt.** Every caller sends one prompt and drains to the next result, the manager included (`bobi/session.py:1331-1332`). The adapter drops every system message except `init` (`bobi/brain/claude.py:318-322`, `:472`), so no drain sees task messages today.
- **Admission sees only Bob launches** (`bobi/subagent.py:1251-1741`). In-run session rebuilds, `workflows resume` and native children bypass it.
- **The concurrency check races.** It (`bobi/subagent.py:1588`) sits outside the cross-process ledger lock (`:1594`).
- **Teardown and reaping:**
  - `_ClaudeSession.abort()` is an immediate SIGKILL of the CLI (`bobi/brain/claude.py:202-223`).
  - `disconnect()` takes the SDK's close path: stdin EOF, then SIGTERM and SIGKILL after 5 s grace each. It runs shielded from cancellation and can take about 20 s by the SDK's own account (`_internal/query.py:1207-1210`). Bob's pin allows SDK versions back to 0.2.87.
  - Tool subprocesses run in their own sessions (`bobi/proctree.py:3-7`).
  - `cancel_agent` refuses inside its own tree (`bobi/subagent.py:3274-3277`).
- **Turn caps do not bound a step.** Turn-cap resumes start a fresh CLI with a fresh turn budget, up to `MAX_TURN_BUDGET_RESUMES = 3` (`bobi/workflow/orchestrator.py:83-97`).
- **Launch lineage cannot anchor identity.** It is env-only and never persisted (`docs/BUILDING_AGENT_TEAMS.md:176-183`).

## Solution

1. **Meter every call (PR 1, every team).**
   - Bob Claude sessions pass `--forward-subagent-text` (through `extra_args`, not env, so settings cannot override it).
   - The adapter emits one de-duplicated usage record per `message_id`, from the parent and every descendant. Descendant calls become usage records only, never output text.
   - Every run gets a ledger. Teams that configure nothing get observe-only figures.
2. **Read every turn (PR 1, every team).** A step's drain ends when the CLI reports itself idle with no agent task open, not at the first result, so the turns the CLI starts on its own after a background task ends are read and charged.
3. **Admit every child (PR 1, where caps are explicit).**
   - A fail-closed `PreToolUse` gate on `Agent|Task` reserves a slot under the team-wide admission lock, or denies.
   - The slot is held until the child ends (`SubagentStop`), not until the spawn's tool returns, which for a background child is the moment it launches.
   - Spawners Bob cannot gate are disallowed. Codex multi-agent is refused.
4. **Charge one allowance (PR 2, opt-in).** A durable per-run ledger plus the team window. Charges are persisted per call, so processes sharing a budget see each other within one call. Renewal is an explicit operator action, per run or for the window.
5. **Stop safely and say so (PR 2).**
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
`budget renew` is the only way to add allowance: to a run, or with `--team` to the current window, where the credit ages out with the window.
It refuses from inside a Bob session, which is best-effort, since `setsid` can evade it. Every renewal is appended to the ledger and alerts the operator channel.

## Scope

In:
- Forwarding plus per-call metering, with an observe-only ledger for every run.
- One reader per Claude session, and the quiescent drain with its wait before a prompt into a reused session.
- The fail-closed native gate with reservations held until each child ends, live counts and the `native_delegation` switch.
- One containment options builder used by every Bob Claude session path.
- Disallowing spawners Bob cannot gate.
- Codex refusal under explicit caps.
- Fixing the concurrency race.
- The run allowance with the required window, and the context cap.
- Soft and hard stops, close and reap, `stop_reason` recording.
- `budget show` and `budget renew`, per run and per team window.
- Engine charge points, and admission on `workflows resume` and relaunches.
- Runs view, CLI and alert surfaces.
- Docs in each PR.

Out:
- Dollar or quota units, and weights. The ledger keeps reported dollars for display only.
- Automatic context-cap rotation inside a step. The stop records a fresh-relaunch path instead.
- Allowances for the manager. Its calls are metered and charge the window, and a window stop never silences it (D10).
- The manager's own drain. It pairs one read with each prompt (`bobi/session.py:1331-1332`), so a turn the CLI starts on its own there would be read as the reply to the next message (inferred from the code). Fixing that means routing the turn to the conversation that spawned the child, a manager change of its own.
- #1154's restart loop and alerting.
- Any deployment's numbers or policy.

## Technical approach

### Accounting identity (AC1, AC2)

- **Per-call records.** The adapter emits one record per distinct `message_id`, from the parent and from forwarded descendants. Reasoning output is a subset of output, never added.
- **Provisional, then authoritative.** Per-call records are provisional and are persisted to the ledger as they arrive.
  - For each Claude session, the ledger holds the last authoritative cumulative snapshot (from `model_usage`, never `ResultMessage.usage`) plus all outstanding provisional charges.
  - The next authoritative result from any process for that session id (a turn result, an unprompted turn's result, or a resumed process's first result) replaces both with `cumulative - last_snapshot`.
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
  - The team window lives beside it in `state/budgets/window.json`, with its own renewal log.
- **Identity.** `budget_id` is the session name (`wf-<workflow>-<repo>-<run_key>`), recorded on `WorkflowRun`.
- **What shares one allowance:**
  - turn-cap restarts, handoff repairs, control reroutes and model switches, through the engine charge points;
  - `workflows resume`, await resume and manager restarts;
  - `--fresh` relaunches under the same key.
- **What gets a new allowance.** A run that completes successfully closes its ledger, so a later run under that key is new work with a new allowance.
- **Inheritance.**
  - A launch from inside a budgeted run inherits its `budget_id`. It is carried in env and recorded server-side on the child's registry entry, so clearing the env var does not detach it.
  - `subagents launch --budget <key>` inherits explicitly.
- **Fail closed, for budgeted runs only.** When the team has `run_tokens` set and the run records a `budget_id`, a missing or unreadable ledger stops the run (`stop_reason: budget_run`, detail `ledger_missing`), never resets it. An observe-only team, or a run created before PR 2 (no `budget_id`), gets a fresh ledger instead.
- **AC6 honestly stated.**
  - The director sends one issue through several workflows under different names: lifecycle, then PR feedback, build failure or merge conflict.
  - Different names are different runs, so each gets its own allowance. The required window bounds their sum.
  - Making "the same work" one allowance across workflow names needs an identity key that Bob does not have today, such as the issue or PR. The run key is not one: follow-up workflows use keys of their own (D4). That is the Gate-1 sign-off asked in D4.

### Quiescent drain (F3)

A step's drain ends when the session is quiet, not at the first result.

- **One reader per session.** The adapter consumes the SDK stream from `connect()` to `disconnect()` with one task that moves every message into Bob's own queue, and drains read from that queue.
  - The SDK dispatches hook callbacks from the same loop that buffers messages for the caller, and that buffer holds 100 messages (`_internal/query.py:233-236`, `:441-446`, `:535`).
  - So with nobody reading between steps, a long unprompted turn would stall every hook callback queued behind it, the gate's and `SubagentStop` included.
- **What the adapter surfaces,** from system messages it drops today (F6):
  - `TurnStarted`, from `init`;
  - `SessionState(state)`, from the CLI's `session_state_changed` (`running`, `idle` or `requires_action`). The SDK already asks the CLI for these frames but keeps them to itself; Bob sets `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`, the caller's opt-in to seeing them (`_internal/transport/subprocess_cli.py:853-861`);
  - `TaskStarted(task_id, tool_use_id, task_type)`;
  - `TaskEnded(task_id, status)`, from a `TaskNotificationMessage`, or from a `TaskUpdatedMessage` with a terminal status (`completed`, `failed`, `stopped`, `killed`). The SDK documents that a background task can end with only the latter, for example one stopped through `TaskStop`.

  Codex yields none of these, so its drain is unchanged.
- **Open agent tasks** are the ones the SDK itself waits on: `task_type` `local_agent` or `local_workflow` (`_internal/query.py:57`), kept because they reliably reach a terminal status (`:55-56`).
  - A background shell is never open, because a dev server may never end.
  - A task leaves the set on `TaskEnded`, or when its child ends by any other signal, through the same `child_ended` event that releases its reservation (below).
- **Quiet** means the step's result has arrived, the CLI's latest state is `idle`, and no agent task is open. Until then the drain keeps reading, and reads each unprompted turn to its result and charges it.
  - This is the SDK's own test for the end of a run (`_internal/query.py:492-503`): a CLI that reports session state stays `running` while it owes a turn, because a background agent that finished just before the result still wakes the session for another one. That is P1's order (F3).
  - It is bounded by the step deadline (`deadline_at`) and, in PR 2, by the budget.
- **A CLI that sends no state events** gets the fallback. The drain keeps reading while agent tasks are open. Once none are, if any task ended since the latest `init`, it waits 5 s, from the later of the result and that end, for an unprompted `init`; in P1 that `init` arrived at the same 3.5 s mark as the first result. A turn that touched no task returns at its result.
- **Step outcome.** The last result read decides the step: its `is_error`, turn-cap kind and final text. Every result read writes its own `stop` record and is charged.
- **Reused sessions.** Prompt steps reuse one session (F3). Before a step sends its prompt, it waits for quiet, reading and charging any unprompted turn already under way and logging it under the previous step. So no leftover turn can answer the new prompt. This also covers a background Bash whose end wakes a turn between steps.
- **One rule, one place.** A helper in the turn primitive (`brain/turns.py`) holds the rule, and both spawn-style drains use it.
- PR 1 first re-runs P1 with state events on, to record the real order, then turns it into failing tests: that order replayed into `drain_turn`, the same order without state events for the fallback, and the out-of-step turn through the orchestrator.

### Native descendants (AC3)

- **One options builder.** PR 1 adds `containment_options()` in a new `bobi/containment.py`, because the workflow path has no hooks today (F6).
  - It returns the hooks, `disallowed_tools`, `extra_args` and env for one Bob Claude session.
  - `orchestrator._make_session`, `subagent._build_client` and the manager's `Session` all merge it, and the defer hook joins its `PreToolUse` list, so no session path can miss the gate.
  - Sessions outside the shared count (the manager, monitors) get forwarding and metering only.
- **Reservation lifecycle.** A slot is held from the spawn until the child ends.
  - *Reserve,* on `PreToolUse` for `Agent|Task`: admission reserves a slot keyed by `tool_use_id` (state `pending`), scoped to the CLI pid and its start token, under the team-wide admission lock, or denies. A nested spawn fires the same session-level hook, with the spawner's `agent_id` (F5).
  - *Bind,* on `SubagentStart`: the CLI's oldest `pending` reservation becomes `live` under the child's `agent_id`. The hook carries no `tool_use_id`, so binding is per CLI; for counting, slots are interchangeable. With nothing pending (a forked skill, or any spawn that skipped the gate), it reserves a `live` slot if capacity allows, otherwise it sets the stop flag.
  - *Release,* only when the child ends, through one idempotent `child_ended(agent_id)` event, which also closes the drain's open-task entry. Its triggers are `SubagentStop` (fired at both depths in P4, and for P1's background child), a `TaskEnded` for the same id (task id and `agent_id` matched in F5), and the CLI's exit. With one reader per session, the `SubagentStop` callback never waits behind unread messages.
  - *Never on `PostToolUse`.* For a background child the `Agent` tool returns at launch (F5). Releasing there would free every slot while 8 background children run, which is the incident's shape.
  - *Spawns that start no child:* `PostToolUseFailure` releases a reservation still `pending`. At each result, the drain releases that CLI's reservations still `pending`, for example a spawn that another `PreToolUse` hook denied after admission reserved it. A `SubagentStart` that arrives after such a release takes the nothing-pending path above, so the count stays right.
- **Fail closed.**
  - The gate's `HookMatcher`s set `on_failure="block"`. A callback that raises, returns nothing, or returns a value the CLI rejects then blocks the spawn; left unset, the SDK counts such a failure as no decision (`types.py:855-864`).
  - The SDK refuses to connect to a CLI that cannot honor `on_failure`, which doubles as the start-time capability check. PR 1 raises the `claude-agent-sdk` floor (`>=0.2.87` today) to a version that has it.
  - Every hook body is also wrapped: any exception, or a lock not acquired within 2 s (taken in a worker thread, with an explicit `HookMatcher` timeout), denies the spawn with a reason and records `hook_error`. A failed `SubagentStop` also records `hook_error`, and the other release triggers still apply.
  - Hooks never stop the session themselves; they set a flag that the drain acts on.
  - Reservations held by a dead CLI are dropped from the count.
- **The concurrency count.** Live registry sessions counted today, plus the `pending` and `live` reservations of counted sessions. `launch_admission` uses the same counter.
  - The race fix re-checks this count inside the second `ledger_lock` pass (`bobi/subagent.py:1594`), keeping the wait outside the lock.
- **Deny reasons the model sees:**
  - "Concurrency cap reached (8 of 8 in use). Do not retry; continue this work inline or wait for a running agent to finish."
  - "Run token budget nearly exhausted. Do not start new agents; write your handoff to <path> and stop."
- **Spawners Bob cannot gate.** `RemoteTrigger`, `CronCreate` and `ScheduleWakeup` go into `disallowed_tools`, and `CLAUDE_CODE_DISABLE_CRON=1` is set.
  - The init message's tool list is checked against a reviewed allowlist of spawning tools.
  - A session exposing an unknown spawner is refused at start (`stop_reason: hook_error`), and the operator is told.
- **Refuse modes.** `native_delegation: refuse`, or a CLI on which the hooks cannot be registered, falls back to `disallowed_tools: ["Agent", "Task"]`.
- **Descendant calls are usage, not output.** The adapter turns a descendant's `AssistantMessage` (one with `parent_tool_use_id`) into a per-call usage record only, and never yields it as `AssistantText`, as it does today (`bobi/brain/claude.py:411-414`).
  - This covers every drain at once, the manager's `Session._drain_turn` included. That drain takes both its reply text and its #454 context-fill sample from every `AssistantText` (`bobi/session.py:949-956`), so a child's text could become the manager's chat reply and a child's small context could hide the parent's.
  - The leak needs no forwarding: P1's background child reached the stream (F4).
- **Codex** under explicit caps gets `-c features.multi_agent=false` (to be verified in PR 1).

### Per-call context check (AC4)

- The per-call context of a call is `uncached + cache_read + cache_write` input for that one `message_id`. It is the same measure `session._context_fill_tokens` uses (#454), never a turn total.
- On a breach by the parent or any descendant, the drain stops the run exactly as for the hard limit below, and records `context_cap`. It does not wait for calls in flight; they fall under the overshoot bound.
- Resuming that transcript would breach again. So the stop clears the step's saved session id, and the ordinary relaunch (`subagents launch -w <workflow> --id <key>`) restarts the stopped step in a **fresh** session, seeded from the workflow context and `budget-stop.yaml`, the same seeding as an `agent:` boundary (D11). `budget show` prints that command.
- `context_tokens` is read live, not snapshotted, so raising it applies to the relaunch.

### Stop, reap, record (AC5, AC7)

- **Soft threshold (`soft_ratio`).**
  - Admission denies spawns and nested launches.
  - A `PreToolUse` hook on `*` denies every tool except writes under the session's handoff path, and the deny reason tells the agent to write its handoff and stop.
  - This happens inside the current turn: no CLI rebuild, no new turn.
- **Hard limit, or context cap.**
  1. The drain stops taking new work and sets the stop flag, so every later hook call denies.
  2. It snapshots the CLI's process tree (`proctree.kill_set`), the CLI included.
  3. It terminates the snapshot with `proctree.terminate` (SIGTERM, 10 s grace, SIGKILL, 5 s; `bobi/proctree.py:42-43`), in a worker thread. In-process children die with the CLI.
  4. It then calls `disconnect()`, which finds the CLI gone and only releases the SDK's resources.
- **Why terminate before `disconnect()`.** `disconnect()` runs shielded from cancellation and can take about 20 s by the SDK's own account (F6), so Bob cannot bound it from outside. The cost: SIGTERM can cut the transcript's last message, which the SDK's stdin-EOF grace exists to save (`_internal/transport/subprocess_cli.py:1024-1028`). The handoff and `budget-stop.yaml` carry what a relaunch needs.
- **Documented bound: everything the run owns is gone within 20 s of the stop decision** (10 + 5 s, plus the snapshot).
- **Recording.**
  - The run is recorded as `failed` with `stop_reason` and `error_kind: budget_exhausted` (D9). A soft stop whose handoff completes is still `failed`, because budget stops never read as done.
  - `budget-stop.yaml` in the session dir holds the last assistant text, the handoff path, the spent figures and the exact renew and relaunch commands.
  - Events: `agent/workflow.budget_stopped`, plus `system/budget.window.exhausted`, posted to the operator channel the same way `system/brain.*` is.
- **Maximum in-flight overshoot (documented).**
  - Checks run after each call, against the shared ledger. When the stop lands, each process sharing the budget can have one call in flight on its parent thread and one on each live native descendant.
  - Overshoot is therefore at most (parent threads + live descendants) × (per-call context + max output). A context cap bounds the per-call term.
  - With explicit caps, gated spawns keep that thread count within `max_concurrent_agents`, because sessions and live reservations share the count. An ungated spawn over the cap sets the stop flag instead.
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
- forwarding, with descendant calls kept out of results;
- the quiescent drain.

They do not get native admission, Codex refusal, allowances or stops, unless they set `max_concurrent_agents`, `budget:` or `native_delegation: refuse` explicitly.
The default cap of 2 keeps counting Bob launches only.
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
- **A background child outlives its turn.** Its slot stays held, and the drain keeps reading until it ends and the CLI's unprompted turn is read and charged.
- **A background Bash outlives its step.** It is not waited on. If its end wakes a turn before the next prompt, the reused-session wait reads and charges it.
- **A child running at the soft threshold** finishes and is charged. The hard limit kills it.
- **The model retries a denied spawn.** It is denied again. The reason says not to retry, and each retry is metered.
- **Custom `.claude/agents/*` subagents** use `Agent` and pass the same gate. Forked skills are caught at `SubagentStart`.
- **A hostile user `~/.claude/settings.json`** cannot turn forwarding off (it is a CLI flag) and cannot add cron tools (they are disallowed). An unknown spawner fails the start check.
- **`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`** is forced to `0`. Out-of-process agent teams are unsupported under containment.
- **A torn or edited ledger** fails closed for a budgeted run. Agents run as the same OS user, so this is a rail, not a security boundary; the docs say so.

## Verification plan

Failing tests first. This extends `tests/test_brain_turns.py`, `tests/test_concurrency_semaphore.py`, `tests/test_spend_governor.py` and the workflow suites.

- **Drain:**
  - F1's replay becomes a test: a configured context cap stops on the first over-cap call, parent or descendant.
  - P1's order, re-recorded with state events, is replayed into `drain_turn`: the `Agent` call, `TaskStarted`, the tool result, the child's call, `TaskUpdated` and `TaskNotification` (both `completed`), the parent's last call, a result while the CLI still reports `running`, then `init`, a call, a second result and `idle`. The drain must read both turns, charge both, and return the second result. On `main` it returns the first.
  - The same order without state events passes through the fallback.
  - A child still running at the result: the drain reads on until it ends and through the unprompted turn. A `SubagentStop` with no task frame also ends the wait.
  - A turn that touched no task returns at its result with no wait.
  - A task that ends with only `TaskUpdated` (`killed`) closes, and a background shell never holds the drain.
  - A 150-message unprompted turn that arrives between steps neither stalls hook callbacks nor answers the next prompt. The out-of-step turn is also reproduced through the orchestrator, then fixed.
  - Descendant calls never reach `final_text` or `response` records, nor the reply text or context fill of `Session._drain_turn` (a stub test on that drain).
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
  - a background spawn replayed in P1's order keeps its slot through `PostToolUse`: the count is 1 until `SubagentStop`, then 0;
  - `TaskEnded` and the CLI's exit release too, a repeated release is a no-op, and `PostToolUseFailure` and the end of the turn release reservations that never bound;
  - workflow, supervised and manager sessions all carry `containment_options()`;
  - a hook exception or timeout denies, and the gate matchers carry `on_failure="block"`;
  - the race closed: two processes at cap minus one admit exactly one;
  - an unknown spawner is refused at start;
  - unconfigured teams are not gated.
- **Identity:**
  - turn-cap restarts, retries, `workflows resume`, `--fresh` and manager restarts cannot reset spent allowance;
  - a missing ledger fails closed for a budgeted run, and starts fresh for an observe-only team;
  - `run_tokens` without a window fails config validation;
  - renew inside a session is refused, every renewal alerts, and `budget renew --team` credits the window.
- **Stop and reap:**
  - an orphaned `setsid` tool is reaped;
  - everything is gone within 20 s of the stop decision, including a CLI that ignores SIGTERM;
  - a relaunch after a `context_cap` stop opens a fresh session seeded with `budget-stop.yaml`;
  - the run is `failed` with `stop_reason` in `/runs`, `subagents show` and the event.
- **Negative guarantees,** as ordinary tests in CI. Each disables one guard with `monkeypatch` and asserts that the failure it prevents happens:
  - with the `Agent|Task` matcher removed from `containment_options()`, a spawn over the cap is admitted;
  - with the persisted spent check removed, a resumed run spends past its allowance;
  - with the `message_id` dedupe removed, duplicated fragments are charged twice.
- **Real-Claude legs**, gated on the CLI and auth, haiku at low effort, with matching stub legs fed injected fake usage:
  1. Reconcile: a generous allowance, a direct background child and a nested child. The ledger equals the results' `model_usage` deltas across both turns, `model_usage` never decreases across the unprompted turn, the slot is held until `SubagentStop`, and the task id equals the `agent_id`.
  2. Stop: an allowance of about 50K stops and reaps the run mid-turn.
  3. Deny: a full cap denies the spawn, and the model sees the reason. A test gate hook that sleeps past its matcher timeout also denies.
  4. Kill then resume charges once.
- **Web UI:** `tests/e2e/test_webapp_ui.py` covers the chip and the cell, with a GIF captured from the real app in the PR.

## Implementation plan

1. **PR 1, meter and native containment.** This meets AC3 for teams with explicit caps.
   - forwarding flag and per-call records, with descendant calls as usage only;
   - `containment_options()`, merged by the three session builders;
   - observe-only ledger and runs-view figures;
   - gate, reservations, bind on `SubagentStart` and release when the child ends;
   - spawner allowlist and the cron disallow;
   - `native_delegation`;
   - Codex refusal;
   - race fix;
   - one reader per session, session-state and task messages in the adapter, the quiescent drain and the reused-session wait;
   - the `claude-agent-sdk` floor raised to a version with `HookMatcher.on_failure`;
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
   - `budget show` and `budget renew`, per run and per team;
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
| Main modules | `brain/base.py`, `brain/claude.py`, `brain/codex.py`, `brain/turns.py`, new `bobi/containment.py` and `bobi/budget.py`, `subagent.py`, `session.py` (one merge call), `workflow/orchestrator.py` (two calls), `concurrency_semaphore.py`, `webapp/runs.py`, `pyproject.toml` (SDK floor) | `bobi/budget.py`, `config.py`, `brain/turns.py`, `workflow/orchestrator.py` (charge points), `workflow/state.py`, `proctree.py`, `cli.py`, events, web UI static |
| Rough size, code + tests | ~850 + ~1,100 lines | ~900 + ~1,000 lines |
| Runtime overhead | Three hook round trips per native spawn; one locked ledger write per model call (about 3K calls per day in the incident); on a CLI without state events only, a 5 s wait after a turn in which a task ended | None beyond PR 1 |
| Capability change | None unless caps are explicit; then denied spawns over the cap | Stops only where a budget is set |

**Payoff, replayed against the incident.**
- The task spent about 510M tokens (86.7% of the day).
- With `run_tokens: 60M` and a 400M five-hour window:
  - each parent run stops at 60M plus the overshoot bound;
  - the three parent runs together stop by the window at 400M, even if they ran under different names;
  - the 22 children are charged to their parents, and spawns over the cap of 8 are denied, background children included.

  Whether the three parents actually ran under one name is not known from the report.
- The deployment-side stopgap (lower effort, continuations gated on a human) makes the problem rarer, but cannot bound a run: nothing at the deployment layer sees per-call usage or native spawns.

## Not verified yet

- **Unprompted turns:**
  - that CLI 2.1.311 sends `session_state_changed` to a caller that opts in, in P1's order. Its binary carries the event and opt-in names; PR 1 re-runs P1 first;
  - the out-of-step turn through the orchestrator (PR 1 reproduces it first);
  - the fallback's 5 s window, which rests on P1's single observation.
- **Usage:** that `model_usage` stays cumulative across an unprompted turn. P1's second result was cut from the probe log. Leg 1 asserts it.
- **Release signals:** that the task id always equals the `agent_id` (three spawns, and not documented by the SDK), and that `SubagentStop` fires for a failed child and for one stopped through `TaskStop`. The CLI's exit releases everything either way. Leg 1 asserts the first.
- **Hook failure behavior:** whether a callback that outlives its matcher timeout counts as a failure under `on_failure="block"`. The wrapper answers within 2 s either way, and leg 3 asserts the deny.
- **Codex:** the `features.multi_agent` flag in codex-cli 0.144.5.
- **The forwarding flag:** CLI 2.1.311's binary lists `--forward-subagent-text` among its options, but passing it through `extra_args` was not probed (the env var was). Also forwarding at depth beyond two, and under wide parallel fan-out. PR 1's start-time capability check covers the first.
- **Other spawners:** forked-skill spawns (`context: fork`) and the `Workflow` tool in Bob sessions, and how settings env interacts with the env Bob passes.
- **CLI and SDK versions:** behavior on CLI versions other than 2.1.311 and SDK versions other than 0.2.178. PR 1 adds a capability check at session start with the refuse fallback.

## Reviews

Three read-only lens reviews (engineering, operator UX, scope), an independent adversarial review, and a fresh read-only review of this revision shaped the design; their blockers are folded in above.
Codex, the house second-opinion tool, was not authenticated, so both independent passes ran `claude -p` read-only on Claude Fable 5.1, a different model from the author's.
Not plan-born, so the scope lens applied.
