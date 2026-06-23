# MDS-65 — Sub-agent completions never reach the launcher

**Status:** SPEC — awaiting human (Zach) approval. Do not implement until approved.
**Type:** Framework fix (modastack runtime). Medium+, multi-file.
**Files:** `modastack/subagent.py`, `modastack/events/subscriptions.py`,
`modastack/cli.py`, `modastack/workflow/orchestrator.py`, `modastack/sdk.py`,
`modastack/session.py` (re-import only), plus new modules `modastack/transient.py`
and a reconciler + tests.
**Observed on:** modastack 0.28.0 (current latest), gtm-team deployment.
**Baseline:** reconciled against `main` @ `d36fbd6` (post-#444). See §1.1.

---

## 1. Problem

Detached sub-agents finish silently and crashed sessions are recorded as `done`,
so completed (or failed) work never reaches the requester unless the launcher
blocks on `--wait` (which pins a concurrency slot). Over ~24h in the gtm-team
deployment, three pieces of work never reached the user: one crash recorded as
`done`, one silent worker-side Slack post, one undelivered result. All three
required manual recovery by the director after the user noticed.

The completion-delivery primitive **already exists**: `_emit_session_finished`
posts `agent/session.completed` / `agent/session.failed` over the durable event
bus, carrying `requested_by`. The loop simply is not closed. There are four
concrete defects plus a missing backstop. All are in the framework runtime and
unreachable from agent topology, so this is a framework fix, not a topology
workaround.

### 1.1 Relationship to #444 (merged — what is already fixed)

PR #444 (`[#443] fix: transient 529/turn error no longer wedges a session`, merged
to `main` @ `d36fbd6`) lands in **`modastack/session.py`** — the *persistent
manager session*. It makes the **launcher's own turn loop** survive a transient
turn-level API error: a `529`/`5xx`/`429`/timeout no longer drops the session into
the terminal `error` state (which had deafened the agent until restart), and the
turn is re-issued in-band with capped backoff (`TURN_RETRY_MAX_ATTEMPTS=2`). It
introduces a canonical transient classifier — `TRANSIENT_API_STATUSES` and
`Session._is_transient_turn_error()`.

**This is a different code path from MDS-65.** #444 fixes the long-running
`Session._process_message` loop. MDS-65's four defects live in the **sub-agent
spawn/executor path** (`subagent.py`, `orchestrator.py`, `subscriptions.py`),
which #444 does **not** touch (verified: #444's diff is `session.py` +
`tests/test_session.py` only). The supervised spawn executor runs its own
`client.query` loop and does not inherit `Session`'s retry/recovery.

**What MDS-65 drops because of #444:** the previously-scoped *spawn-side
transient-5xx retry* (old §4.3 / D2 / Phase 1). Transient-529 *survival and
retry* is now owned by #444 at the persistent-session layer, and we will not
double-fix it with a second bespoke retry loop in the spawn path. Instead a
transient `529` in a spawned sub-agent becomes an **honest `failed`** (RC#2 fix
below) that is **delivered** to the launcher (RC#1) — and the launcher is now
itself 529-resilient (#444), so re-dispatch happens at the orchestration layer,
not via duplicated retry logic.

**Single-source the transient classifier (reviewer recommendation, @underminedsk
on #445).** #444 introduced the canonical "what counts as transient" set
(`TRANSIENT_API_STATUSES`) and the classifier `Session._is_transient_turn_error()`
inside `modastack/session.py` — but the classifier is today a `Session` *method*
coupled to `self._last_api_error_status` / `self._last_response`, so the
spawn/workflow path cannot reuse it without importing the persistent-session
class or re-deriving a second copy. MDS-65 therefore **lifts the shared
definitions into a small `modastack/transient.py` helper** (the status set + a
pure `is_transient_api_error(status, text="")` function + the `TURN_RETRY_BASE` /
`TURN_RETRY_MAX_ATTEMPTS` budget constants), and has `session.py` re-import them
(behaviour-preserving — `Session._is_transient_turn_error()` becomes a thin
delegate). This is a pure extraction, **not** a second retry loop: it gives the
spawn/executor path (RC#2 honest classification, and any future orchestration-layer
re-dispatch decision) and the persistent-session path **one** definition of
"transient" and one retry budget instead of two divergent copies. See §4.3.

**What MDS-65 keeps (untouched by #444, all re-verified against `d36fbd6`):**
honest terminal status (RC#2), lifecycle subscription (RC#1), durable/reconcilable
terminal status (RC#3), `requested_by` on the non-persistent path (RC#4), and the
reconciler/dead-man backstop.

### Root causes (re-verified against `main` @ d36fbd6, post-#444)

1. **Lifecycle events are emitted into the void — nothing subscribes to
   `agent/session.*`.** `cli.py` builds the entry point's subscription list from
   `discover_subscriptions()` (Slack topics + project name, `subscriptions.py:13`)
   plus `monitor_subscription_keys()` for monitor findings
   (`cli.py:220-227`). There is **no analog** wiring lifecycle events to the
   persistent entry-point agent. Empirically zero `agent/session.*` events have
   ever hit the deployment's bus. This is "detached agents finish silently."

2. **`status="done"` is hard-coded even on an error result.** In the supervised
   spawn executor (`subagent.py:303-307`), after
   `result.success = not result_msg.is_error`, the code calls
   `registry.update(name, status="done", ...)` regardless of `is_error`. Only the
   `except TimeoutError` / `except Exception` branches set `status="error"`. A
   `529 Overloaded` returns as an **error `ResultMessage`**, not an exception — so
   `state.json` is written `done` while `result.success` is `False`. The same
   dishonesty exists at the workflow level: `orchestrator.py:548-552` emits
   `agent/session.completed` in a `finally` block **unconditionally**, even
   immediately after `agent/workflow.failed` (`orchestrator.py:541`).
   **Re-verified post-#444 (`d36fbd6`):** unchanged — #444 touched only
   `session.py`, so `subagent.py:303-307` still writes `status="done"`
   regardless of `result_msg.is_error`, and the orchestrator `finally` still
   emits unconditionally. This defect is wholly MDS-65's. Note the contrast:
   #444 made the *persistent session* honest about a turn error (no longer
   forces terminal `error`); the *spawn executor* makes the opposite error —
   forcing `done` on a real failure — and that is what this RC fixes.

3. **The terminal lifecycle emit is best-effort and silently swallowed.**
   `_emit_lifecycle_event` (`subagent.py:96-124`) runs the POST on a daemon
   thread and catches every exception ("never let event posting surface",
   `subagent.py:118`). With `blocking=True` it joins only up to `timeout` (5s).
   When the event server is flaky — the exact condition #409 documents
   (registration handshake timeouts every 1–2h) — the completion event vanishes
   with a `debug` log; on a hard crash the daemon thread is killed mid-POST.
   Terminal status is never durably persisted in a form a subscriber or
   reconciler can recover.

4. **`requested_by` is dropped on the non-persistent path.** The workflow phase
   executor `run_phase_blocking` (`subagent.py:328-409`) has **no `requested_by`
   parameter at all** and calls
   `_emit_session_finished(result, project, name, started_at, role=role)`
   (`subagent.py:408`) without it. The orchestrator's `finally` emit
   (`orchestrator.py:549`) likewise drops it even though the orchestrator threads
   `requested_by` into the variable scope (`orchestrator.py:183-184`). Even a
   subscriber could not route those completions back to the right Slack thread.
   (`spawn_adhoc` **does** thread `requested_by` correctly — `subagent.py:542`,
   `:564` — so the gap is specifically the workflow/phase path.)

### Why 0.28.0 and #444 do not fix this

0.28.0 is a stability release; its headline #409 fix addresses sessions dying at
*init* (→ `error` state), the opposite of a mid-flight crash recorded as `done`.
Nothing in the changelog touches completion delivery or terminal-status honesty.
#444 (above, §1.1) hardens the *persistent manager session* against transient
turn errors but does not touch the spawn/executor path, the lifecycle
subscription, terminal-status honesty in `subagent.py`/`orchestrator.py`,
`requested_by` threading, or the missing reconciler. The completion-delivery loop
remains open.

---

## 2. Solution overview

Close the loop with five changes, each independently testable. (Transient-529
*survival/retry* is no longer in scope — #444 owns it at the persistent-session
layer; see §1.1.)

1. **Auto-subscribe the persistent entry point to lifecycle topics** via a
   `lifecycle_subscription_keys()` analog to `monitor_subscription_keys()`, wired
   in `cli.py` the same way monitor topics are. Completions then wake the
   launcher the way monitor findings already do — no polling, no `--wait`.
2. **Make terminal status honest.** Introduce a terminal-status vocabulary
   (`completed` / `failed` / `crashed`) and never write `done` on an error
   result, in both the supervised executor and the orchestrator `finally`. A
   transient `529` that surfaces as an error `ResultMessage` is recorded
   honestly as `failed` and delivered (not retried in the spawn path — see §1.1).
3. **Persist terminal status durably and reconcilably**, written to `state.json`
   *before* and independent of the best-effort bus POST.
4. **Thread `requested_by` through the non-persistent emit path** (phase executor
   + orchestrator `finally`).
5. **Add a reconciler / dead-man backstop**: any dispatched run with no terminal
   event by its declared `timeout` is reconciled to a terminal status and its
   lifecycle event re-emitted (event-driven on the timeout the workflow steps
   already declare, not a poll loop).

### Why framework, not topology

A topology workaround (explicit `subscribe:` list + a launcher-side reconciler)
is a partial, leaky backstop: an explicit `subscribe` early-returns over the
auto-detected Slack topics (`subscriptions.py:26-28`), and it still rides the
best-effort emit (#3) and the dropped `requested_by` (#4). #2/#3/#4 and the
missing subscription are all in the runtime and cannot be reached from topology.

---

## 3. Scope

### In scope
- **New module `modastack/transient.py`**: lift #444's `TRANSIENT_API_STATUSES`,
  a pure `is_transient_api_error(status, text="")` classifier, and the
  `TURN_RETRY_BASE` / `TURN_RETRY_MAX_ATTEMPTS` budget constants into one shared
  home; have `session.py` re-import them (behaviour-preserving). Single-sources
  the definition of "transient" across the persistent-session and spawn/workflow
  paths (reviewer recommendation; §1.1, §4.3).
- `subscriptions.py`: add `lifecycle_subscription_keys()`.
- `cli.py`: wire lifecycle keys into the entry-point subscribe list.
- `subagent.py`: honest terminal status in `_run_agent_supervised`; durable
  terminal-status persistence helper; `requested_by` on `run_phase_blocking`;
  reconciler entry points. (No spawn-side transient-5xx retry — #444 owns it; §1.1.)
- `orchestrator.py`: honest + `requested_by`-carrying terminal emit in the
  `finally` block (no unconditional `session.completed` after a failure).
- `sdk.py`: terminal-status vocabulary on the registry; reconciler reads/writes.
- New module `modastack/reconcile.py` (or `subagent` helpers) for the dead-man
  backstop.
- Tests for every codepath above (see §5).

### Out of scope
- **Transient-529 survival/retry** — owned by #444 (`session.py`, persistent
  session). MDS-65 does not add a second retry loop in the spawn path; a transient
  529 there becomes an honest `failed` (RC#2) and is delivered (RC#1). See §1.1.
- Changing the event-server routing contract or topic semantics.
- Reworking `#409`'s init-failure handling.
- Changing the Slack delivery format of completion notifications beyond routing
  them to the requester's thread (the entry point already renders `requested_by`,
  `events/client.py:118-128`).
- Migrating historical `done`-but-failed `state.json` records (note: the new
  terminal vocabulary is additive; existing records remain readable via
  `SessionEntry.from_dict`, `sdk.py:201-212`).
- `VERSION` / `pyproject.toml` version / `CHANGELOG.md` (release-time only).

### Decision points for human review
- **D1 — terminal vocabulary.** Recommended: `completed` / `failed` / `crashed`
  as new terminal statuses, keeping `done` only as a backward-compatible alias
  for reading old records. `list_active()` (`sdk.py`) treats all three as
  inactive. *Alternative:* reuse `done`/`error` and add an orthogonal
  `outcome` field. Recommendation: the explicit vocabulary — status honesty is
  the whole point of #2.
  (~~D2 — transient-5xx retry budget~~ — **resolved, no longer open.** No
  spawn-side retry loop (transient-529 *retry* is owned by #444 at the
  persistent-session layer). Per the reviewer (@underminedsk, #445), the residual
  concern — two divergent copies of "what is transient" / the retry budget — is
  resolved by lifting #444's classifier + budget into the shared
  `modastack/transient.py` (in scope above; §4.3), not by adding a second retry
  loop. See §1.1.)
- **D2 — reconciler trigger.** Recommended: event-driven sweep on manager wake +
  a bounded grace period past each run's declared `timeout`
  (`PHASE_TIMEOUT` / workflow step `timeout` / `spawn` timeout), **not** a
  standalone poll loop. *Alternative:* a low-frequency interval monitor.
  Recommendation: reconcile on wake + on the dead-man deadline; reuse the
  existing dead-pid sweep in `list_active()` (`sdk.py`) as the hook so we do not
  add a new always-on thread.
- **D3 — reconciler action.** Recommended: mark the entry `crashed` (pid dead, no
  terminal event) or `failed` (terminal deadline exceeded, pid alive → also
  cancel), then **re-emit** `agent/session.failed` with the persisted
  `requested_by` so the launcher's thread gets closed. Escalation copy is
  director-facing. Needs human sign-off on escalation wording.

---

## 4. Technical approach (per root cause)

### 4.1 Subscribe the entry point (RC #1)

Add to `modastack/events/subscriptions.py`:

```python
LIFECYCLE_EVENTS = ("agent/session.completed", "agent/session.failed")

def lifecycle_subscription_keys() -> list[str]:
    """Topics the entry point must subscribe to so sub-agent completions are
    delivered back to the launcher. Mirrors monitor_subscription_keys: returns
    BOTH the bare type (session.completed) and the source-qualified topic
    (agent/session.completed), since current servers route onto both and older
    servers deliver only the bare type."""
    keys: list[str] = []
    for event in LIFECYCLE_EVENTS:
        delivered = event.split("/", 1)[1] if "/" in event else event
        for key in (delivered, event):
            if key not in keys:
                keys.append(key)
    return keys
```

Wire into `cli.py` immediately after the monitor block (`cli.py:225-227`):

```python
from modastack.events.subscriptions import lifecycle_subscription_keys
for key in lifecycle_subscription_keys():
    if key not in subscribe:
        subscribe.append(key)
```

Delivery: a non-inbox lifecycle event flows through the drain → reactor → the
manager's inbox exactly as a monitor finding does (`events/drain.py`), and
`events/client.py:118-128` already renders `requested_by`. No new delivery path
is needed — only the subscription.

**Dedup note:** the manager that *spawned* the sub-agent also subscribes to these
topics. The completion event for its own child must wake it (that is the whole
point), so no self-author skip applies here — unlike PR-feedback dispatch
(#411). The reactor must treat lifecycle events as deliver-to-inbox, never as an
auto-dispatch trigger.

### 4.2 Honest terminal status (RC #2)

In `_run_agent_supervised` (`subagent.py:303-307`) replace the unconditional
`done` with a terminal status derived from `is_error`:

```python
result.success = not result_msg.is_error
if result_msg.is_error:
    result.error = result_msg.result or "unknown error"
terminal = "completed" if result.success else "failed"
_persist_terminal(registry, name, terminal, session_id=result_msg.session_id,
                  phase=phase)
```

In `orchestrator.py:548-552`, the `finally` must not emit `session.completed`
after a failure. Track the workflow outcome and emit `session.completed` only on
success, `session.failed` (with `error`) on the failure path — carrying
`requested_by` from scope (`ctx.scopes.get("requested_by")`).

### 4.3 Shared transient classifier — no spawn-side retry (owned by #444)

Originally MDS-65 proposed retrying a transient `529`/`5xx` error `ResultMessage`
in the supervised spawn loop. **#444 now owns transient-529 survival/retry** at
the persistent-session layer (`session.py`: `TRANSIENT_API_STATUSES`,
`Session._is_transient_turn_error()`, bounded in-band retry). We do **not**
duplicate that with a second bespoke retry loop in `subagent.py`.

Instead, a transient `529` in a spawned sub-agent is handled by the rest of this
spec: §4.2 records it **honestly as `failed`** (no `done`-on-error), and §4.1
**delivers** that `failed` to the launcher, which is itself 529-resilient via #444
and can re-dispatch at the orchestration layer.

**What MDS-65 *does* do here (reviewer recommendation, @underminedsk on #445):**
single-source the classifier so the two paths never drift. Extract a small
`modastack/transient.py`:

```python
# modastack/transient.py
TURN_RETRY_BASE = 2.0
TURN_RETRY_MAX_ATTEMPTS = 2

# overload, rate limit, gateway/timeout 5xx — anything else (4xx) is a real error.
TRANSIENT_API_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

_TRANSIENT_TEXT = ("overloaded", "rate limit", "rate_limit", "529",
                   "503", "502", "504", "timed out", "timeout")

def is_transient_api_error(status: int | None, text: str = "") -> bool:
    """Status-first, text-sniff fallback. Pure — no session state."""
    if status in TRANSIENT_API_STATUSES:
        return True
    if status is not None:
        return False  # a concrete non-transient status (e.g. 400) — don't retry
    t = (text or "").lower()
    return any(s in t for s in _TRANSIENT_TEXT)
```

`session.py` then re-imports these (no behaviour change): the constants come from
`transient.py`, and `Session._is_transient_turn_error()` becomes a thin delegate
—

```python
from modastack.transient import (
    TRANSIENT_API_STATUSES, TURN_RETRY_BASE, TURN_RETRY_MAX_ATTEMPTS,
    is_transient_api_error,
)

def _is_transient_turn_error(self) -> bool:
    return is_transient_api_error(self._last_api_error_status, self._last_response or "")
```

This is a **pure extraction**, covered by the existing `tests/test_session.py`
(unchanged behaviour) plus a small unit test for the free function. The spawn
executor's honest-status logic (§4.2) and any orchestration-layer re-dispatch
decision now consult the *same* `is_transient_api_error` / budget — one
definition of "transient", not two.

### 4.4 Durable, reconcilable terminal status (RC #3)

Add a helper (in `sdk.py` or `subagent.py`) that writes the terminal status to
`state.json` **before** the best-effort bus POST:

```python
def _persist_terminal(registry, name, status, *, session_id="", phase="",
                      error="", requested_by=None):
    registry.update(name, status=status, pid=0, session_id=session_id,
                    phase=phase)  # durable, synchronous, local
    # then best-effort emit (may be swallowed; reconciler is the backstop)
```

`registry.update` is a synchronous local file write (`sdk.py:239-251`) — durable
even if the bus POST never lands. The reconciler (§4.6) reads `state.json` as the
source of truth, so a swallowed emit no longer loses the completion. Record a
monotonic `terminal_at` (or reuse `last_activity`) and whether the emit was
confirmed, so the reconciler can tell "terminal but un-emitted" from "still
running."

### 4.5 Thread `requested_by` (RC #4)

- `run_phase_blocking` (`subagent.py:328`): add a `requested_by: dict | None =
  None` parameter, thread it into both `_emit_session_started`
  (`subagent.py:351`) and `_emit_session_finished` (`subagent.py:408`). Update
  its callers in `orchestrator.py` (which already hold `requested_by` in scope).
- Orchestrator `finally` emit (`orchestrator.py:549`): include
  `requested_by=ctx.scopes.get("requested_by", {})`.

### 4.6 Reconciler / dead-man backstop

New logic (module `modastack/reconcile.py` or `subagent` helpers):

- **Input:** the registry's `state.json` records, each carrying `started_at`,
  `status`, `pid`, `phase`, `requested_by`, and the run's declared `timeout`
  (persist the effective timeout on the entry at register time so the reconciler
  knows each run's deadline — currently `PHASE_TIMEOUT`/workflow step/spawn
  timeouts live only in the executor).
- **Trigger (D2):** run on manager wake and as part of the existing dead-pid
  sweep in `list_active()` (`sdk.py:300-318`) — no new always-on thread.
- **Logic:** for each non-terminal entry:
  - pid dead, no terminal event → mark `crashed`, emit `agent/session.failed`
    with the persisted `requested_by` and an "agent crashed without reporting"
    summary.
  - `started_at + timeout + grace < now`, pid alive → mark `failed`, cancel the
    pid (`cancel_agent`), emit `agent/session.failed`.
  - terminal status persisted but emit never confirmed → re-emit the
    corresponding lifecycle event (idempotent; the launcher dedupes by `run_key`).
- **Idempotency:** the reconciler must never double-close a run already terminal
  with a confirmed emit. Use a persisted `emit_confirmed` flag (or
  `reconciled_at`) so repeated sweeps are no-ops.
- **Escalation (D3):** when a crash/timeout is reconciled, the re-emitted event
  routes to the requester's thread (closing the loop) and optionally notifies the
  director — wording pending human sign-off.

---

## 5. Verification plan (tests-first)

Every codepath gets a unit test; the production-bug rule (CLAUDE.md) requires a
**failing reproduction first**, then the fix. Add an integration test that drives
the full spawn → complete → deliver loop.

### Reproduction tests (must fail on `main`, pass after fix)
- **RC#1:** entry-point subscribe list (built as in `cli.py:210-227`) contains
  no `agent/session.*` key. Assert `lifecycle_subscription_keys()` output is
  included after the fix.
- **RC#2:** drive `_run_agent_supervised` with a mocked error `ResultMessage`
  (`is_error=True`, `result="API Error: 529 Overloaded"`); assert `state.json`
  status is **not** `done` — it is `failed` (and `result.success is False`).
- **RC#2/orchestrator:** force the workflow body to raise; assert the `finally`
  emits `agent/session.failed`, **not** `agent/session.completed`.
- **RC#3:** make the bus POST raise inside `_emit_lifecycle_event`; assert the
  terminal status is still durably written to `state.json` (emit swallowed,
  state intact) and the reconciler re-emits it.
- **RC#4:** call `run_phase_blocking` with a `requested_by`; assert the
  `session.completed`/`failed` payload carries it. Same for the orchestrator
  `finally` emit.
- **Reconciler:** seed a `state.json` with a dead pid + non-terminal status past
  its `timeout`; assert the sweep marks it `crashed`/`failed` and emits
  `agent/session.failed` with the seeded `requested_by`. Assert a second sweep is
  a no-op (idempotency).

### Transient-529 honesty (no spawn-side retry)
- A spawn-path error `ResultMessage` with a 529 signature is recorded **`failed`
  immediately** (not `done`, not retried in the spawn path) and a
  `agent/session.failed` is delivered — confirming MDS-65 defers retry to #444.
  (#444's own retry/survival behavior is covered by `tests/test_session.py`;
  MDS-65 adds no retry test of its own.)

### Integration test (`tests/integration/`)
- Spawn a real short-lived sub-agent via the entry point; assert the launcher
  receives `agent/session.completed` in its inbox carrying `requested_by`,
  closing the loop without `--wait`. (Gated like other integration tests.)

### Regression guards
- `monitor_subscription_keys` behavior unchanged.
- `spawn_adhoc`'s existing `requested_by` threading unchanged.
- Old `done` `state.json` records still load via `SessionEntry.from_dict`.

---

## 6. Implementation plan (phased, tests-first)

Each phase is independently reviewable and lands behind passing tests. Order is
chosen so honesty/durability land before the subscription that exposes them.

- **Phase 0 — shared transient classifier + terminal vocabulary + durable
  persistence (RC#2/#3 core).** First, the behaviour-preserving extraction:
  create `modastack/transient.py` (§4.3) and re-point `session.py` at it
  (`tests/test_session.py` must stay green; add a unit test for the free
  `is_transient_api_error`). Then write the RC#2/#3 failing tests; add the
  terminal-status vocabulary (D1) + `_persist_terminal` helper; make
  `_run_agent_supervised` honest (a transient 529 → honest `failed`, no spawn-side
  retry — §4.3); make the orchestrator `finally` honest. Keep `done` readable for
  old records.
- **Phase 1 — thread `requested_by` (RC#4).** Failing tests → add the parameter
  to `run_phase_blocking` + orchestrator scope plumbing.
- **Phase 2 — subscribe the entry point (RC#1).** Failing test →
  `lifecycle_subscription_keys()` + `cli.py` wiring + reactor deliver-to-inbox
  (no auto-dispatch) for lifecycle events.
- **Phase 3 — reconciler / dead-man backstop.** Failing tests → persist effective
  `timeout` at register; reconcile on wake + dead-pid sweep; re-emit with
  idempotency.
- **Phase 4 — integration test + `/review`.** Full-loop integration test; run
  `/review`; fix everything it finds; run `pytest tests/ --ignore=tests/integration/`
  then the integration suite.

Each phase: write the test that fails, implement, confirm green, then proceed.
Do not bump `VERSION` / `pyproject.toml` / `CHANGELOG.md` (release-time only).

---

## 7. Risks

- **Double delivery / loops.** The spawning manager subscribes to lifecycle
  topics it also emits to. Mitigation: lifecycle events are deliver-to-inbox only,
  never an auto-dispatch trigger; launcher dedupes by `run_key`. Explicitly tested.
- **Double retry across layers.** Risk that the spawn path adds its own retry on
  top of #444's persistent-session retry, multiplying backoff and masking real
  failures. Mitigation: MDS-65 adds **no** spawn-side retry (§1.1/§4.3) — a
  transient 529 becomes an honest `failed` and is delivered; retry stays at the
  single #444 layer.
- **Reconciler false positives.** Closing a still-running agent whose emit is
  merely delayed. Mitigation: grace period past the declared timeout + pid
  liveness check; idempotency flag prevents double-close.
- **Topic-contract drift.** Relies on the server routing both bare and
  source-qualified topics (same assumption `monitor_subscription_keys` already
  makes). Subscribing to both forms covers older servers.

---

## 8. Acceptance criteria

- No `state.json` is written `done` (or any non-terminal→`done`) when the run's
  result is an error.
- The entry point subscribes to `agent/session.completed`/`failed`; a completed
  detached sub-agent's event reaches the launcher's inbox without `--wait`.
- Completion/failure events carry `requested_by` on every spawn path (phase,
  orchestrator, adhoc).
- A swallowed bus POST does not lose the completion: terminal status is durable in
  `state.json` and the reconciler re-emits.
- A crashed or timed-out run is reconciled to `crashed`/`failed` and an
  `agent/session.failed` event is emitted to the requester; repeated sweeps are
  no-ops.
- A transient 529 in a spawned sub-agent is recorded honestly as `failed` and
  delivered to the launcher (retry/survival is owned by #444 — §1.1; MDS-65 adds
  no spawn-side retry).
- Full unit suite green; the three gtm-team failure modes are covered by
  reproduction tests.
