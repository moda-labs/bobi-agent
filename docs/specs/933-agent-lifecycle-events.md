# 933 - Agent start / stop / restart lifecycle events in the event queue

Spec for [#933](https://github.com/moda-labs/bobi-agent/issues/933).
Status: awaiting build approval (Gate 1).
Every code citation below was read first-hand in a worktree branched from
`origin/main` at `b5388bb`.

## 1. Problem

The event timeline shows inbound events, manager decisions, and session
lifecycle.
It does not show when the agent manager starts, stops, or restarts, so an
operator cannot tell whether downtime was intentional, who asked for it, or
whether a start command actually reached a healthy manager.

Hosted deployments already have half of this.
The supervisor sidecar emits `manager_started` / `manager_stopped` /
`manager_restarted` onto `fleet/lifecycle`
(`bobi/supervisor/supervision.py:389,441,453,462,671,694`), the Worker folds
them into a bounded 48h trail (`event-server/worker/src/fleet.ts:120,247-256`),
and `EventBusRuntime.health_summary` renders that trail
(`bobi/webapp/event_bus.py:538-551`).

Local deployments have none of it.
`LocalRuntime.health_summary` returns `"lifecycle": []` literally
(`bobi/webapp/runtime.py:839`), and the local start/stop/restart paths
manipulate the process directly with no record anywhere.

And even on a hosted box the record is not durable: every lifecycle edge is a
best-effort bus publish (`bobi/supervisor/telemetry.py:112-118, 208-212`), so a
broker outage erases the operational record of exactly the incident an operator
most needs to reconstruct.

## 2. Solution

Add a durable, append-only lifecycle journal at `run/state/lifecycle.jsonl`,
written directly to disk by whichever process **confirmed the effect**, never
through the event bus.
Reuse the supervisor's existing `fleet/lifecycle` vocabulary and payload shape
as the canonical contract for its entries.

Then unify the reads: one fold projects journal entries and hosted
`fleet/lifecycle` edges into one row shape, and `bobi agent events`, the
`health_summary` lifecycle trail, and a new event-queue read model all render
from that single fold.

## 3. Load-bearing findings

Each of these is a premise the design rests on. Each was verified in the tree,
not inferred.

**F1. `correlation_id` already exists in the canonical contract, and nothing
uses it.**
`Telemetry._emit_lifecycle` accepts and serializes `correlation_id`
(`bobi/supervisor/telemetry.py:196-206`), but a repo-wide grep finds no caller
that passes one:

```
$ rg -n 'correlation_id' -g '!CHANGELOG.md'
bobi/supervisor/telemetry.py:196:    def _emit_lifecycle(self, event: str, *, correlation_id: str | None = None,
bobi/supervisor/telemetry.py:203:        if correlation_id is not None:
bobi/supervisor/telemetry.py:204:            payload["correlation_id"] = correlation_id
```

So the field name for "one ID linking request and effect" is already chosen by
the contract. This spec fills it rather than inventing `operation_id`.

**F2. The admin command id never reaches the lifecycle edge.**
`admin.py` dispatches `restart` / `stop` / `start` by calling
`request_manager_restart()` / `_stop()` / `_start()`
(`bobi/supervisor/admin.py:257-265`), and those setters take no arguments
(`bobi/supervisor/supervision.py:399-409`).
The `command_id` the operator polls is therefore unlinkable to the edge the
command produced.

**F3. No local start path waits for readiness.**
The CLI `start` command calls `service.spawn_team` (`bobi/cli.py:421`) and
`LocalRuntime.start_team` calls it too (`bobi/webapp/runtime.py:597`).
`spawn_team` "spawn[s] the manager detached and return[s] without waiting for
registration" (`bobi/service.py:340-346`).
The only code that waits for registration *and* transport readiness is
`service.start_team` (`bobi/service.py:434-449`), via
`_wait_for_manager_entry` and `_wait_for_manager_transport`
(`bobi/service.py:301-337`) - and its only caller is `launch_team`, used by two
integration tests.
`service.restart_team` (`bobi/service.py:722-729`) has no callers at all; this
was already recorded as Q031 in `plans/2026-07-22-review-remediation.md:204`.

Consequence: **no operator-facing process observes a local start completing**,
so "record `manager_started` after registration and transport readiness"
cannot be satisfied by instrumenting the requester.

**F4. `stop_team` already observes the confirmed effect of a stop.**
It polls up to 6s for `ProcessLookupError` and reports a kinded outcome -
`stopped` / `killed` / `stale` / `invalid_pid` / `permission_denied` /
`still_running` (`bobi/service.py:668-719`, `StopResult` at
`bobi/service.py:108-118`).
Both operator stop paths go through it (`bobi/cli.py:1038`,
`bobi/webapp/runtime.py:610`), so it is the single correct writer for stop
effects and stop failures.

**F5. Restart is two independent calls on both local paths.**
CLI: `ctx.invoke(stop)` then `ctx.invoke(start, fresh=fresh)`
(`bobi/cli.py:1091-1092`).
Webapp: `service.stop_team(root)` then `self.start_team(name)`
(`bobi/webapp/runtime.py:619-628`).
Nothing links the two halves today.

**F6. `child_agent_env` inherits the parent environment, and already strips one
inherited-id hazard.**
It documents stripping `BOBI_LAUNCH_LINEAGE` because "an agent running the
routine `bobi agent <x> restart` copies its own chain into the new manager
daemon" (`bobi/env.py:140,146-155`).
A correlation id passed by env has exactly the same hazard and gets exactly the
same treatment (see §5.4).

**F7. The manager child is spawned through two hops.**
Supervisor spawns `bobi agent <name> start`
(`bobi/supervisor/supervision.py:269-277`), which spawns
`bobi agent <name> start --foreground` with `child_agent_env`
(`bobi/service.py:376-405`).
An env-carried id survives both hops for free.

**F8. `run/state/lifecycle.jsonl` has no name collision.**
`state_path()` is `<run>/state` (`bobi/paths.py:215-217`).
Enumerating its children mechanically finds no `lifecycle*` path:

```
$ rg -n 'state_path\((.*)\) / "' bobi/*.py bobi/**/*.py
```

19 hits: `workflow/runs`, `manager-health.port`, `admin-cursor.json`,
`monitor_runs`, `scripts`, `format_version`, `kb`, `decisions.jsonl`,
`spend_governor.json`, `deployments` (x2), `monitor_state.json`, `cursors`,
`bubble.json`, `manager.pid`, `long_term_memory.md`,
`long_term_memory_cursor`, `sessions`, and one re-read of
`manager-health.port`. Plus the `events-*.jsonl` glob (`bobi/cli.py:1906`,
`bobi/doctor.py:638`), which `lifecycle.jsonl` does not match.

**F9. `bobi agent events` has no test coverage.**
`rg -n '_show_events' tests/` returns nothing.
`_show_events` (`bobi/cli.py:1897-1973`) is untested today, which is why this
spec moves its fold into a tested module rather than adding a fourth source to
it in place.

**F10. A local event-queue *view* is a standing non-goal, and lifecycle is not
what that non-goal excludes.**
The merged single-agent-view plan lists "*Decisions and raw event deliveries
stay CLI-only* (`bobi agent events`) - they are log lines, not runs (no status,
no cost)" (`plans/2026-07-31-single-agent-view.md:135-136`) and repeats
"decisions/raw events in the UI" under Non-goals (`:316`).
Issue #933 argues the opposite category on the same axis: lifecycle entries are
not runs *and* not log lines, they "explain changes to the runtime that performs
that work".
§4 resolves this explicitly rather than quietly overriding an approved plan.

## 4. Scope

### In scope

1. `bobi/manager_lifecycle.py`: the journal (schema, append, reconcile,
   retention) and the read fold (collapse, projection).
   Named `manager_lifecycle`, not `lifecycle`, because `LIFECYCLE_EVENTS` in
   `bobi/events/subscriptions.py:64` already means *session* lifecycle
   (`agent/session.completed`). Two "lifecycle" vocabularies in one package is
   how a reader ends up debugging the wrong one.
2. Local writers: manager-side start confirmation, `service.stop_team` stop
   and stop-failure entries, start-failure entries at the two operator entry
   points, restart correlation on both restart paths.
3. Supervisor: thread the admin `command_id` into `correlation_id` on operator
   edges (F2), and journal every supervisor edge locally through a new
   observer.
4. Reads: fill `LocalRuntime.health_summary()["lifecycle"]`, fold the journal
   into `bobi agent events`, and add `GET /api/agents/{name}/events` on both
   runtimes.
5. One visible local surface: a `last_transition` entry in the status strip's
   existing `segments` list (§5.6). No new component, no new frontend code.
6. Docs: `docs/ADMIN_PROTOCOL.md` (additive fields), `docs/AGENT_STATE.md`
   (local trail is now populated), a new `docs/LIFECYCLE_JOURNAL.md`.
7. Tests per §7.

### Out of scope, and why

- **A full event-queue *list view* in the local `bobi app` UI.**
  F10: an in-UI event/decision queue is a standing non-goal of the merged
  single-agent-view plan. What this spec ships instead is the read model, the
  *existing* rendering seam (`health_summary()["lifecycle"]`, the key both
  runtimes fill and the hosted console already consumes), and one status-strip
  segment so the local page is not left with a populated-but-invisible
  payload. An operator on `bobi app` sees the last transition; the full history
  is `bobi agent events` and the `/events` endpoint.
- **The hosted console's event-queue view.**
  The console is private (`moda-labs/moda-agents`). This repo ships the API it
  reads; the console-side render is the separate management-UI ticket named in
  §11.
- **`event` / `decision` rows on the new endpoint.**
  The row shape carries `kind` so they can be added without a contract change,
  but only `kind: "lifecycle"` is emitted here. Adding the other two would
  require a box-consulting admin command for hosted and would contradict F10.
- **The Runs table.** Unchanged. `bobi/webapp/runs.py:12-14` already states the
  exclusion; this spec adds a line naming lifecycle explicitly.
- **`service.restart_team`.** Still zero callers (F3). Not adopted, not
  deleted here; deleting it is Q031's job, not this issue's.
- **Version / changelog.** Untouched, per the repo's release rules.

## 5. Technical approach

### 5.1 The canonical contract, reused verbatim

A journal entry **is** a `fleet/lifecycle` payload
(`bobi/supervisor/telemetry.py:196-206`) plus two additive fields:

```json
{
  "deployment": {"fleet": "default", "instance": "eng-team",
                 "platform": "unknown", "machine": null,
                 "region": null, "node": null},
  "event": "manager_started",
  "generated_at": "2026-08-06T04:10:22.114233+00:00",
  "correlation_id": "01J8XKQ2R4ZC1V0S9F3W7NBE6M",
  "reason": "operator",
  "manager_pid": 4711,
  "phase": "start",
  "origin": "manager"
}
```

- `deployment` comes from `resolve_deployment_identity`
  (`bobi/supervisor/identity.py:60-91`), which already resolves for an
  unsupervised local agent (`fleet: "default"`, `instance` from the run-root
  basename, `platform: "unknown"`). One definition, not two.
  `bobi/manager_lifecycle.py` imports it **function-locally**, matching the repo's
  pervasive lazy-import style, so a core module never pays the sidecar's import
  cost at startup. Rejected alternative: hoist identity to `bobi/identity.py`
  and re-export from the supervisor. Cleaner arrow direction, but it edits a
  module whose docstring pins its dependency discipline
  (`bobi/supervisor/__init__.py:14-19`) for a cosmetic gain.
- `phase` (`"start" | "stop" | "restart"`) and `origin`
  (`"manager" | "operator" | "supervisor"`) are the two additive fields.
  Additive is free under the protocol's compatibility promise
  (`docs/ADMIN_PROTOCOL.md:29-31`), so `SUPERVISOR_VERSION` does not move.

**Vocabulary.** The three confirmed effects keep the supervisor's names.
Failures get three new names in the same family, because a failure is a
distinct event and not a `manager_stopped` with a flag:

| Event | Written when |
|---|---|
| `manager_started` | registration **and** transport readiness both confirmed |
| `manager_stopped` | the process is confirmed gone, or a stale pid is reconciled |
| `manager_restarted` | derived by the fold from a correlated stop+start (§5.5) |
| `manager_start_failed` | a start attempt failed with a known reason |
| `manager_stop_failed` | the process would not exit, or could not be signalled |
| `manager_restart_failed` | derived: a restart whose start phase never landed |

### 5.2 The journal file

`run/state/lifecycle.jsonl`, one JSON object per line, newest last.

**Every write holds `fsutil.file_lock` for the whole open-append-close.**
That is stricter than the existing event-log writer's bare `O_APPEND`
(`bobi/events/client.py:65-74`), and the extra strictness is load-bearing
rather than defensive.
Retention (below) prunes by rewriting the file through
`fsutil.atomic_write_text`, and that helper lands the new content as a **new
inode renamed over the target** - a hazard its own docstring names
(`bobi/fsutil.py:30-36`).
An appender holding an fd on the pre-rename inode would write into an orphaned
file and silently lose its line.
Serializing every writer against the same lock closes that race; there is no
version of this design where a plain append and an atomic rewrite coexist
safely on one path.

The cost is nil in practice. Lifecycle writes are a handful per day, not per
second, and the lock is held for one `json.dumps` and one `write`.

**Multiple writers are expected**: the manager process, a CLI `stop`, a webapp
worker, and the supervisor can all append.

**Every write is fail-open.** A read-only state directory, a full disk, or a
lock timeout is logged and swallowed. A journal failure must never fail a
start, a stop, or a supervisor restart decision - the record exists to explain
operations, not to gate them.

**Retention** is `LIFECYCLE_RETENTION_S = 48 * 60 * 60`, chosen to equal the
Worker's `LIFECYCLE_TTL_S` (`event-server/worker/src/fleet.ts:120`) so the two
trails agree, plus a hard `MAX_LIFECYCLE_ENTRIES = 500` backstop for a
restart-looping box. Pruning happens **at write time only**, under the same
lock the append holds, and only when the file exceeds the entry cap. Read paths
never mutate.

The 48h bound is a real trade: a Friday-evening stop is gone by Monday. It is
taken deliberately because the issue asks for retention "consistent with the
supervisor's lifecycle trail", and one constant with one comment beats two
trails that disagree.

**`--fresh` does not wipe the journal.** `clear_manager_session` removes only
the saved session id, the bubble state, and the `deployments/` and `cursors/`
directories (`bobi/service.py:145-155`). That is correct: `--fresh` clears
conversation state, not the operational record of why the runtime moved.

### 5.3 Who writes each confirmed effect

The rule is: **the writer is whichever process can observe the effect.**

| Effect | Writer | Where |
|---|---|---|
| `manager_started` | the manager itself | a daemon confirmer thread started in `run_manager_from_config`, just before the blocking `spawn_adhoc` call (`bobi/service.py:657`) |
| `manager_stopped` | the stopper | `service.stop_team`, on `stopped` / `killed` / `stale` (`bobi/service.py:695-708`) |
| `manager_stop_failed` | the stopper | `service.stop_team`, on `still_running` / `permission_denied` / `invalid_pid` |
| `manager_start_failed` | the requester | CLI `start` and `LocalRuntime.start_team`, in the existing `except` arms |
| every supervisor edge | the supervisor | a journal observer in the `_MultiObserver` fan-out |

**The start confirmer.** It reuses `_wait_for_manager_entry` +
`_wait_for_manager_transport` (`bobi/service.py:301-337`) against its own root
and session name, which are the two predicates the issue's wording names. On
success it writes `manager_started` with the confirmed pid; on timeout it
writes `manager_start_failed` with the timeout's reason. It is a daemon thread
so it can never hold shutdown, and it is fail-open: any exception is logged and
swallowed, exactly as the supervisor's observers are.

Why the manager and not the requester: F3. The requester returns before
readiness on every local path, and changing that would make `bobi agent x
start` block for up to 30s and terminate a slow-but-healthy manager on timeout
(`bobi/service.py:441-449`). Writing from inside the manager also covers the
paths that have no requester at all - systemd, the container entrypoint, and a
supervisor respawn.

**Reconciliation covers what nobody observed.** A manager killed out of band
(OOM, `kill -9`, a container stop) produces no `manager_stopped`.
Reconciliation runs in exactly two places, both under the §5.2 lock, so two
processes can never both append a reconciled stop for the same generation:

- inside `record()` **only when the entry being written is
  `manager_started`** - if the newest existing entry is a `manager_started`
  for a pid that is no longer alive with no later stop, a
  `manager_stopped` with `reason: "reconciled"` is appended first. Scoping it
  to start records keeps every other write a single lock-and-append, and a
  start is precisely the moment the previous generation is provably over.
- `stop_team`'s existing stale-pid branch (`bobi/service.py:687-689`), which
  already detects the same condition and now records it.

So the next start or the next stop closes the gap, and no read path ever
writes.

### 5.4 Correlation ids

One id per operator *operation*, in the contract's existing field (F1).

- **Local restart** mints one id and threads it through both halves:
  `stop_team(..., correlation_id=op, phase="restart")` for the stop, and
  `BOBI_LIFECYCLE_CORRELATION_ID=op` in the child env for the start.
  **Only the id crosses the process boundary.** `phase` is set by whichever
  writer already knows the operation's shape - the stopper knows it is stopping
  for a restart; the manager's confirmer thread cannot know why it was spawned
  and is never asked to. §5.5's fold derives the restart from the pair, which
  is why no second env var is needed.
- **Hosted** uses the admin `command_id` as the id, closing F2:
  `request_manager_restart(correlation_id=...)` / `_stop` / `_start` carry it,
  `_operator_*` and `_note_restart` pass it to
  `observer.lifecycle(..., correlation_id=...)`, and `_respawn` puts the same
  value in the child's env so the manager's own confirmation entry correlates
  with the supervisor's edge.
- **Everything else** mints a fresh id per entry, so every row has one and the
  fold never has to special-case a missing key.

**The inherited-id hazard, and its fix.** `child_agent_env` inherits the parent
environment (F6), so an agent that runs `bobi agent x restart` from a session
launched with the variable set would stamp a stale id onto an unrelated
manager. `child_agent_env` therefore **strips**
`BOBI_LIFECYCLE_CORRELATION_ID`, in the same clause and for the same reason it
strips `BOBI_LAUNCH_LINEAGE` (`bobi/env.py:140,146-155`), and the spawner
injects the fresh value into the returned dict immediately before spawning -
the pattern `launch_agent` already uses for the lineage stamp.

### 5.5 One row per operation: the fold

`bobi/manager_lifecycle.py::fold(entries)` groups by `correlation_id` and collapses
each group to one row. This is the single place the three-rows-for-one-restart
problem is solved, and it also makes the supervisor/manager double-recording of
a single start collapse for free.

Collapse rules, applied per group, in order:

1. **A group holding both a stop effect and a start effect is a restart.**
   The row is `manager_restarted`. Its `at` and `manager_pid` come from the
   **start** entry, because that is when the replacement became ready and which
   process it is; its `reason` comes from the stop entry, because that is where
   the requester's intent was recorded.
   This derivation is why no `phase` has to cross a process boundary (§5.4).
2. **A group holding only a stop that carries `phase: "restart"` is a failed
   restart.** The row is `manager_restart_failed` with that entry's reason.
   Rule 1 cannot infer this, which is the whole reason `phase` exists as a
   field: a restart whose replacement never came up is indistinguishable from a
   plain stop without it.
3. **Otherwise the row is the group's highest-ranked entry**, by this fixed
   rank: `manager_restarted` > `manager_started` > `manager_stopped` >
   any `*_failed`. When the winner is `manager_restarted` but the group also
   holds a `manager_started`, the row still takes its `at` and `manager_pid`
   from the `manager_started`, for rule 1's reason.
   Ties inside a rank go to `origin: "manager"` over `origin: "supervisor"`,
   because the manager's entry is the one that waited for readiness.
4. **A single-entry group passes through unchanged**, which is every hosted
   `probe_failing`, `probe_recovered`, and `budget_exhausted` edge.

Rule 3 is doing real work on two pairs that occur constantly, not just in
theory. A supervised **operator restart** produces `manager_restarted` (the
supervisor, at respawn) and `manager_started` (the manager, at readiness) under
one id, and collapses to one honest row carrying the supervisor's reason and
the manager's confirmed timestamp. A supervised **boot or wedge restart**
produces the same pair.

Rule 3 is also the deduplication the issue asks for, and it is worth stating
plainly rather than overselling: a published copy and a locally journaled copy
of the same effect share a `correlation_id`, so they collapse - but **today no
single response carries both copies**, because `EventBusRuntime` reads only the
server-side trail and never the box's journal
(`bobi/webapp/event_bus.py:525-534`). The rule earns its place on the two local
pairs above; it makes a future merged read correct for free.

Ordering is newest first, keyed on `generated_at` with the file's append order
as the tiebreak, matching `listLifecycle`'s ordering
(`event-server/worker/src/fleet.ts:184-186`).

### 5.6 Reads

`bobi/manager_lifecycle.py::to_rows(entries)` is the one projection. All three read
surfaces call it, which is what makes "the CLI and the UI show the same
history" mechanical rather than a promise.

**`bobi agent events`** (`bobi/cli.py:1897-1973`) gains lifecycle lines in its
existing timeline, sorted with the other entries.
`--decisions-only` continues to mean decisions only: lifecycle rows are
suppressed by it exactly as event deliveries already are, because a lifecycle
transition is not a manager decision.
Because the fold moves into a tested module, the CLI keeps only formatting -
which is also how F9's coverage gap gets closed without rewriting the command.

**`health_summary`.** `LocalRuntime` replaces `"lifecycle": []`
(`bobi/webapp/runtime.py:839`) with the folded rows, capped at the same
`MAX_HEALTH_LIFECYCLE_EVENTS = 50` the hosted runtime uses
(`bobi/webapp/event_bus.py:66`), and its docstring's "there is no supervisor
here, so ... the lifecycle trail is empty" (`bobi/webapp/runtime.py:772-780`)
is corrected in the same commit.
This is the parity win: the payload key, the row shape, and the renderer are
now identical for local and hosted.

**One visible line in the local UI: a `last_transition` status segment.**
Without this, the local `bobi app` page would show nothing new, because it
renders only `state`, `detail`, and `segments` today
(`bobi/webapp/static/views/agent.js:219-233`) and has no lifecycle-row
renderer - so the payload would be populated and invisible.
`bobi/webapp/health.py::build_state` therefore gains one best-effort segment
built from the newest folded row:

```json
{"key": "last_transition", "label": "last transition", "kind": "text",
 "value": "restarted by operator", "note": "2026-08-06T04:10:22+00:00"}
```

This is deliberately the smallest surface that closes the gap. `segments` is
already documented as an ordered, best-effort list a client must render as
given (`bobi/webapp/runtime.py:281-287`), and `fmtSegment` already handles
`kind: "text"` (`bobi/webapp/static/views/agent.js:61-68`) - so this is **zero
new frontend code, zero new components, and no design-system decision**, which
is exactly why it does not reopen F10's non-goal. The status strip's job is
already "is this thing running"; why it last changed belongs there.

**`GET /api/agents/{name}/events`.** The event-queue read model, added beside
the existing `/runs` route (`bobi/webapp/server.py:190`) with a
`TeamRuntime.events()` abstract method implemented by both runtimes in the same
commit, per the ABC's own widening rule (`bobi/webapp/runtime.py:89-94`).

- `LocalRuntime.events()` reads the journal and folds it.
- `EventBusRuntime.events()` folds `buildInstanceDetail`'s `lifecycle` array
  (`event-server/worker/src/fleet.ts:551-556`) through the **same**
  `to_rows`, so the response shape cannot drift between deployments. No
  box-consulting command, no new admin verb, no `SUPERVISOR_VERSION` bump, and
  no TTL cache: it is one KV read per poll with no round-trip to the box, for
  the same reason `health_summary` needs none
  (`bobi/webapp/event_bus.py:525-532`).

Rejected name: `/api/agents/{name}/lifecycle`. It describes what ships today
more literally, but the row carries `kind` precisely so `event` and `decision`
rows can join without a contract change, and renaming a route later is the one
kind of churn a documented API should not need.

Response:

```json
{
  "rows": [
    {
      "kind": "lifecycle",
      "id": "01J8XKQ2R4ZC1V0S9F3W7NBE6M",
      "event": "manager_restarted",
      "at": "2026-08-06T04:10:22.114233+00:00",
      "received_at": null,
      "correlation_id": "01J8XKQ2R4ZC1V0S9F3W7NBE6M",
      "reason": "operator",
      "manager_pid": 4711,
      "restart_count": null,
      "origin": "manager",
      "detail": "manager restarted by operator (pid 4711)"
    }
  ],
  "counts": {"lifecycle": 12},
  "truncated": false,
  "retention_seconds": 172800
}
```

Field semantics: `at` is the recording box's own clock, ISO 8601 UTC;
`received_at` is server-receipt epoch **milliseconds** and is non-null only for
rows folded from the hosted trail, matching the existing convention
(`bobi/webapp/event_bus.py:545-548`); `restart_count` is the supervisor's
window count and is null for local rows because a local agent has no restart
budget; `detail` is one line of prose for a human, built server-side because
the vocabulary is the server's; `truncated` is true when the limit clipped
rows. `?limit=` defaults to 100, matching `runs.py`'s `DEFAULT_LIMIT`
(`bobi/webapp/runs.py:51`). Unknown fields are ignored by consumers, per the
protocol's compatibility promise.

### 5.7 Supervisor changes

Two, both additive:

1. `correlation_id` threading (F2), as described in §5.4.
2. A `JournalObserver` added to the `_MultiObserver` fan-out
   (`bobi/supervisor/supervision.py:207-212`), writing every edge to the local
   journal. The fan-out is already per-observer fail-open, so a journal write
   error cannot reach telemetry, the alerter, or the restart state machine.

This is what makes the issue's "a broker failure must not erase the local
operational record" true: the durable copy is now written on the box, by the
process that decided, before any publish is attempted.

## 6. Mechanical inventories

Every call site below was produced by the printed command and every hit is
classified. No list here was written by hand.

### A. Local lifecycle entry points

```
$ rg -n -g 'bobi/**/*.py' '\b(start_team|stop_team|restart_team|spawn_team|run_team_foreground)\s*\('
```

| Hit | Classification |
|---|---|
| `bobi/cli.py:419` `run_team_foreground(...)` | foreground start; **confirmer thread covers it** (it runs inside `run_manager_from_config`) |
| `bobi/cli.py:421` `spawn_team(...)` | CLI start; **add `manager_start_failed` in the existing `except` arms** |
| `bobi/cli.py:1038` `stop_team(...)` | CLI stop; **pass `correlation_id` when invoked by `restart`** |
| `bobi/service.py:340` `def spawn_team` | definition; unchanged |
| `bobi/service.py:420` `def start_team` | definition; unchanged (still only `launch_team`'s callee) |
| `bobi/service.py:428` `spawn_team(...)` | inside `start_team`; unchanged |
| `bobi/service.py:466` `start_team(...)` | inside `launch_team`; unchanged |
| `bobi/service.py:474` `def run_team_foreground` | definition; unchanged |
| `bobi/service.py:668` `def stop_team` | definition; **journal writer, gains `correlation_id` kwarg** |
| `bobi/service.py:722` `def restart_team` | definition; **zero callers, out of scope** |
| `bobi/service.py:728,729` | inside `restart_team`; out of scope |
| `bobi/webapp/event_bus.py:375,380,385` | hosted lifecycle commands; unchanged (the supervisor journals its own edges) |
| `bobi/webapp/runtime.py:119,123,127` | `TeamRuntime` ABC declarations; unchanged |
| `bobi/webapp/runtime.py:597` `service.spawn_team` | webapp start; **add `manager_start_failed` in the existing `except` arms** |
| `bobi/webapp/runtime.py:610` `service.stop_team` | webapp stop; unchanged (the writer is inside `stop_team`) |
| `bobi/webapp/runtime.py:623,628` | webapp restart; **mint and thread `correlation_id`** |
| `bobi/webapp/server.py:227,231,235` | HTTP handlers; unchanged |

### B. `health_summary` implementations and the `lifecycle` key

```
$ rg -n -g 'bobi/**/*.py' -g 'bobi/**/*.js' 'health_summary|"lifecycle"|health\.lifecycle'
```

| Hit | Classification |
|---|---|
| `bobi/webapp/event_bus.py:525` | hosted impl; unchanged |
| `bobi/webapp/event_bus.py:539,575` | hosted trail fold; **re-pointed at the shared `to_rows`** |
| `bobi/webapp/health.py:245` | `normalize` docstring; unchanged |
| `bobi/webapp/runtime.py:256` | ABC declaration; **docstring gains the new row fields** |
| `bobi/webapp/runtime.py:271` | ABC shape doc; **updated** |
| `bobi/webapp/runtime.py:772` | local impl; **docstring corrected** |
| `bobi/webapp/runtime.py:839` `"lifecycle": []` | **the defect; replaced with the folded rows** |
| `bobi/webapp/server.py:180` | route; unchanged |

No frontend hit. The local `bobi app` page renders `state`, `detail`, and
`segments` only (`bobi/webapp/static/views/agent.js:219-233`); the
`lifecycle`-row CSS at `bobi/webapp/static/app.css:449-451` is a leftover from
the five-panel page the single-agent view replaced. Consistent with F10, this
spec adds no local renderer; the stale CSS comment is corrected.

### C. The events read path

```
$ rg -n -g 'bobi/**/*.py' 'events-\*\.jsonl|decisions\.jsonl|_show_events|_log_event'
```

| Hit | Classification |
|---|---|
| `bobi/cli.py:1897` `def _show_events` | **gains lifecycle rows via `to_rows`** |
| `bobi/cli.py:1906` `events-*.jsonl` glob | unchanged |
| `bobi/cli.py:1946` `decisions.jsonl` | unchanged |
| `bobi/cli.py:2027` `_show_events(...)` | command body; unchanged |
| `bobi/doctor.py:638` | doctor's own glob; unchanged |
| `bobi/events/client.py:49,435` `_log_event` | the append pattern §5.2 follows; unchanged |
| `bobi/inbox.py:196` | comment; unchanged |
| `bobi/session.py:649,723,773` | session lifecycle writes into the event log; unchanged |

### D. Webapp read-model routes

```
$ rg -n '@app\.(get|post)\("/api' bobi/webapp/server.py
```

23 routes; all unchanged. The new `GET /api/agents/{name}/events` is added
after `/api/agents/{name}/runs` (`bobi/webapp/server.py:190`), the route it is
the sibling of.

### E. Supervisor lifecycle emitters

```
$ rg -n -g 'bobi/**/*.py' '\.lifecycle\(|_emit_lifecycle\(|_note_restart\('
```

| Hit | Classification |
|---|---|
| `bobi/supervisor/supervision.py:209` | `_MultiObserver` fan-out; **`JournalObserver` joins here** |
| `bobi/supervisor/supervision.py:349` `budget_exhausted` | journaled; no correlation id (not operator-initiated) |
| `bobi/supervisor/supervision.py:380,389` `_note_restart` | **gains `correlation_id` passthrough** |
| `bobi/supervisor/supervision.py:441` operator restart | **passes the admin `command_id`** |
| `bobi/supervisor/supervision.py:453` operator stop | **passes the admin `command_id`** |
| `bobi/supervisor/supervision.py:462` operator start | **passes the admin `command_id`** |
| `bobi/supervisor/supervision.py:489,516,528` wedge/crash restarts | journaled; minted id, `reason` already carried |
| `bobi/supervisor/supervision.py:671,694` `run()` boot/teardown | journaled; minted id |
| `bobi/supervisor/telemetry.py:115` | observer entry point; unchanged |
| `bobi/supervisor/telemetry.py:186,193` probe episodes | journaled; minted id |
| `bobi/supervisor/telemetry.py:196` `_emit_lifecycle` | **`correlation_id` finally reaches it** |

## 7. Verification plan

Unit and integration, in `tests/`. The acceptance criteria map one-to-one.

**`tests/test_manager_lifecycle_journal.py`** (new)
- append then read round-trips the canonical payload shape
- two concurrent appenders both land, neither line is torn
- **an append racing a retention prune is not lost** - the specific race §5.2's
  lock exists for, and the one this suite must fail without it: hold a writer
  at the lock while another crosses the entry cap, then assert both lines are
  in the surviving file
- retention drops entries past 48h and past the 500-entry cap; a read never
  mutates the file
- reconciliation appends `manager_stopped{reason: "reconciled"}` when the last
  start's pid is gone, and does not when it is alive; two concurrent starts
  produce exactly one reconciled stop
- fold rule 1: a correlated stop+start collapses to one `manager_restarted`
  carrying the start's `at`/`manager_pid` and the stop's `reason`
- fold rule 2: a lone stop with `phase: "restart"` collapses to
  `manager_restart_failed`; a lone stop without it stays `manager_stopped`
- fold rule 3: a `manager_restarted` + `manager_started` pair (the supervised
  restart) yields one `manager_restarted` row at the confirmed timestamp
- fold rule 4: a lone hosted `probe_failing` passes through unchanged
- a write to a read-only directory is logged and swallowed, not raised
- a malformed line is skipped, not fatal

**`tests/test_service_lifecycle_journal.py`** (new)
- `stop_team` writes `manager_stopped` on a confirmed exit, on a force kill,
  and on a stale pid
- `stop_team` writes `manager_stop_failed` on `still_running` and on
  `permission_denied`
- the start confirmer writes `manager_started` once both waiters return, and
  `manager_start_failed` on either timeout
- `child_agent_env` strips an inherited `BOBI_LIFECYCLE_CORRELATION_ID`

**`tests/test_supervision_operator.py`** (extend)
- an admin `restart` / `stop` / `start` puts its `command_id` on the edge
- `JournalObserver` writes every edge; a raising journal does not disturb
  telemetry, the alerter, or the restart decision

**`tests/test_webapp_health.py` / `test_webapp_event_bus.py`** (extend - these
are where `LocalRuntime` and `EventBusRuntime` are exercised today; there is no
`test_webapp_runtime.py`)
- `LocalRuntime.health_summary()["lifecycle"]` is populated and newest-first
- both runtimes' `events()` return byte-identical row shapes for equivalent
  input; the hosted fold sets `received_at` and the local fold sets it null
- `?limit=` clips and sets `truncated`
- the `last_transition` segment appears when the journal has entries and is
  absent (not faked) when it is empty, per `segments`' best-effort contract

**`tests/test_webapp_server.py`** (extend)
- `GET /api/agents/{name}/events` returns 200 with the documented shape and
  404 for an unknown agent

**`tests/test_cli.py`** (extend, closing F9)
- `bobi agent events` interleaves lifecycle, event, and decision lines in
  timestamp order
- the CLI and `LocalRuntime.events()` return the same lifecycle history for the
  same journal, which is the acceptance criterion stated as a test

**Integration (`tests/integration/test_manager_lifecycle.py`, extend)**
- a real `launch_team` then `stop_team` writes exactly one confirmed
  `manager_started` and one `manager_stopped` to the real file
- the entries survive a simulated event-server outage, since nothing on the
  write path touches the bus

Brain-agnostic by the repo's own rule (`CLAUDE.md`, "Real-Claude e2e as
acceptance criteria"): this is process lifecycle and a read-model fold, so the
stub path proves it and no `[claude]` leg is warranted.

**Proof of work for the PR**: the real `run/state/lifecycle.jsonl` produced by
a real start/stop/restart cycle against an isolated `BOBI_HOME`, plus the real
`GET /api/agents/{name}/events` response body, both pasted into the PR
description.

## 8. Implementation plan

Ordered so each step is independently green.

1. `bobi/manager_lifecycle.py`: schema, `record()`, `read()`, locking,
   retention, reconcile, `fold()`, `to_rows()`, and its unit tests. No callers
   yet.
2. `service.stop_team` writes stop and stop-failure entries; `child_agent_env`
   strips the correlation var.
3. The manager-side start confirmer in `run_manager_from_config`.
4. Restart correlation on both local paths; start-failure entries at the two
   operator entry points.
5. Supervisor: `correlation_id` threading and `JournalObserver`.
6. Reads: `LocalRuntime.health_summary`, the `last_transition` segment in
   `build_state`, `_show_events`, `TeamRuntime.events()` on both runtimes, the
   route.
7. Docs: `docs/LIFECYCLE_JOURNAL.md`, `docs/ADMIN_PROTOCOL.md`,
   `docs/AGENT_STATE.md`, `docs/RUNS_VIEW.md`, and the stale CSS comment.

## 9. Risks and open questions

- **48h retention loses a weekend.** Accepted deliberately (§5.2) to keep one
  bound across both trails. If review prefers a longer local bound, it is one
  constant, and the disagreement with the Worker's TTL must then be documented
  at both ends.
- **The confirmer thread is a new thread in the manager.** It is a daemon,
  bounded by the existing 30s waiter budget, and fail-open. It cannot delay
  shutdown and cannot fail a start.
- **Multi-writer correctness rests on an advisory `flock`.** `fsutil.file_lock`
  is `fcntl.flock`, so it binds only writers that take it. Every writer in this
  design does, and the suite pins the append-versus-prune race directly. A
  future writer that appends without the lock would reintroduce the
  lost-line bug; the module's docstring says so at the write function.
- **A supervised box now records each edge twice** (journal + bus). That is the
  point (durability), and the fold collapses them on read. It does cost one
  small file write per edge.
- **Open question for the requester:** should `manager_start_failed` also fire
  when a manager boots successfully and then dies during startup, before the
  confirmer's waiters return? As specced, that produces a
  `manager_start_failed` from the timeout, which reads as "start failed" rather
  than "started then crashed". Distinguishing them needs the requester to watch
  the child, which no local path does today (F3). Recommendation: ship as
  specced and revisit if the reason string proves misleading in practice.

## 10. Review record

This spec was reviewed before submission and revised; the findings below are
folded into the design above, not appended to it.

**Second-opinion tooling was unavailable in this session and is not being
claimed.** `codex exec` returns `401 Unauthorized` and `aichat` has no
configured gateway (no `OPENROUTER_API_KEY` / `OPENAI_API_KEY` in the
environment). The review below was run directly against the tree, with every
challenge checked against real code rather than argued from memory.

| # | Finding | Resolution |
|---|---|---|
| B1 | The retention prune rewrites the file via `atomic_write_text`, which replaces the inode (`bobi/fsutil.py:30-36`). An appender holding an fd on the old inode would silently lose its line. | §5.2 now takes `fsutil.file_lock` on **every** write, not just the prune. A test pins the race. |
| B2 | Two processes could each append a reconciled `manager_stopped` for the same dead generation. | §5.3 scopes reconciliation to `manager_started` writes and `stop_team`'s stale branch, both under B1's lock. |
| M1 | The original fold rule keyed on `phase: "restart"`, but the start half is written by the manager, which cannot know why it was spawned. The rule was unimplementable. | §5.4 propagates only the id; §5.5 rule 1 derives the restart from the stop+start pair, and `phase` now earns its place solely on rule 2 (a restart whose start never landed). |
| M2 | A supervised restart produces `manager_restarted` **and** `manager_started` under one id, and the original rules gave no precedence. | §5.5 rule 3 adds an explicit rank and says which fields the row takes from which entry. |
| M3 | Nothing would have changed in the local `bobi app` UI: the payload would be populated and invisible. | §5.6 adds one `last_transition` entry to the existing `segments` list - zero new frontend code - and §4 now says plainly what an operator sees where. |
| m1 | `bobi/lifecycle.py` collides with `LIFECYCLE_EVENTS` (`bobi/events/subscriptions.py:64`), which means *session* lifecycle. | Module renamed `bobi/manager_lifecycle.py`; §4 records why. |
| m2 | `--decisions-only` must not start showing lifecycle rows. | Stated in §5.6. |
| m3 | A journal write failure must never fail a start or stop. | Fail-open stated in §5.2 and tested. |
| m4 | Does `--fresh` wipe the journal? | Verified it does not (`bobi/service.py:145-155`); stated in §5.2. |
| m5 | Does the hosted `events()` need the spend cache's TTL treatment? | No - one KV read, no box round-trip. Stated in §5.6. |
| m6 | `/events` returns only one `kind` today; why not `/lifecycle`? | Rejected alternative recorded in §5.6 with the reason. |

## 11. Tracking

The issue asks for a Linear backlink to the existing Bobi management UI ticket.
Linear access is unavailable in this environment, so the backlink is **not**
created by this spec.
It is a tracking action, not a design dependency, and it does not block
implementation: the console-side render (§4, out of scope) is that ticket's
work, and this spec's deliverable is the API it reads.
Flagged here so it is not silently dropped.
