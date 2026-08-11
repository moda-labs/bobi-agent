# 992 - A session-fatal edge for the supervisor probe

Status: **awaiting Gate 1** (design approval). No code written.
Issue: [#992](https://github.com/moda-labs/bobi-agent/issues/992)
Author: engineer agent, 2026-08-11

---

## 1. Summary

An agent whose manager is alive but whose *brain* is dead reads as `idle` to the
supervisor, which is indistinguishable from a quiet night.
Every existing probe signal reports on the manager, and the manager is genuinely fine.

This spec adds a fourth, independent signal: **the terminal outcomes of recently dispatched sessions**.
When the manager is healthy and the last N dispatched sessions all reach a terminal failure sharing
one error cause, the supervisor opens a deduped incident, posts one Slack notice carrying that
error string verbatim, and posts a RECOVERED notice once a session *born after the incident* runs
to completion.
It never restarts anything and never charges the restart budget.

I am proposing one structural change to the issue's sketch: **do not add a `failing` value to
`manager.status`**.
Publish the condition as a new, orthogonal `session_health` block instead.
§5 states the case and costs both shapes; §12.1 reserves the decision for Gate 1.

---

## 2. Evidence: this already happened here, for 102 minutes

The issue cites Barndoor's 2026-08-05 outage (revoked OAuth token) second-hand.
I found the same failure class in **this deployment's own registry**
(`/data/.bobi/agents/eng-team/run/state/sessions/`), from a different root cause.

Every session started in a 102-minute window on 2026-07-30 failed, with a byte-identical error:

```
window 07-30 22:35:15 .. 07-31 00:17:05 UTC  (101.8 min)
ALL sessions STARTED inside it: 13   Counter({'failed': 13})

  22:35:15 failed  life= 260.2s  wf-pr-closed-eng-team-adhoc-4e7dfe34
  22:59:19 failed  life=  85.7s  wf-pr-closed-eng-team-adhoc-80b6032f
  23:03:14 failed  life=  60.8s  wf-pr-closed-eng-team-adhoc-60014791
  23:11:45 failed  life=   2.2s  wf-pr-closed-eng-team-adhoc-7d3deb65
  23:13:32 failed  life=   3.2s  wf-pr-closed-eng-team-adhoc-62d4cc2d
  23:17:16 failed  life=   2.4s  wf-pr-closed-eng-team-adhoc-71fdc83b
  23:20:25 failed  life=   2.5s  wf-pr-closed-eng-team-adhoc-4d2b0376
  23:21:47 failed  life=   3.6s  wf-pr-closed-eng-team-adhoc-8c071799
  23:24:09 failed  life=   3.1s  wf-pr-closed-eng-team-adhoc-d0579407
  23:37:09 failed  life=   2.3s  wf-pr-closed-eng-team-adhoc-60ffed2a
  23:48:28 failed  life=   2.3s  wf-pr-closed-eng-team-adhoc-b287e31b
  23:51:34 failed  life=   2.6s  wf-pr-closed-eng-team-adhoc-88853c40
  00:17:03 failed  life=   2.2s  wf-pr-closed-eng-team-adhoc-e7f6d9f9

  every one:  error = "You've hit your session limit · resets 12:40am (UTC)"
  completed sessions ending inside the window: 0
```

Reproduce with:

```bash
python3 - <<'PY'
import json, glob, collections, datetime
rows = []
for p in glob.glob('<run>/state/sessions/*/state.json'):
    try: rows.append(json.loads(open(p).read()))
    except Exception: pass
lim = [r for r in rows if 'session limit' in (r.get('error') or '')]
lo = min(r['started_at'] for r in lim); hi = max(r['terminal_at'] for r in lim)
inw = sorted((r for r in rows if lo <= (r.get('started_at') or 0) <= hi),
             key=lambda r: r['started_at'])
print(len(inw), collections.Counter(r.get('status') for r in inw))
PY
```

Three things this changes about the design:

1. **The failure class is broader than a revoked token.**
   A subscription rate limit produces the identical shape: manager healthy, every dispatch dies,
   nobody told.
   Anything brain-fatal and shared-cause qualifies (revoked credential, quota exhaustion, gateway
   misconfiguration, a bad model pin).
2. **The condition can be self-healing.**
   A rate limit resets on its own at 12:40am.
   That makes the RECOVERED notice load-bearing rather than decorative, and it independently
   confirms the issue's point 3: restarting is the wrong reflex for a condition the box cannot fix
   and time may.
3. **"Died within seconds" is the wrong primary gate.** See §6.2 - it would have opened this
   incident 42 minutes in, discarding the first three failures entirely.

---

## 3. Premise verification

Every claim below was checked by reading the file at `origin/main` @ `7025981`, not inherited from
the issue.
Three of the issue's premises needed correcting.

| # | Claim (from the issue) | Verdict | Evidence |
|---|---|---|---|
| P1 | `probe.py::derive_manager_status` fuses three manager-only signals into the verdict | **TRUE** | `bobi/supervisor/probe.py:88-132`; inputs are the `/health` body, `manager_pid_alive`, `status_file_age` |
| P2 | An alive-but-brainless manager reads as `idle` | **TRUE** | `probe.py:118-119` returns the reported status verbatim when a process is alive and not wedged |
| P3 | `manager_health.py`'s `/health` body already carries a `sessions` block from the registry | **TRUE, but useless here** | `bobi/manager_health.py:87`, `144-155`: `_session_status_from_registry` returns `list_active()` projected to `{name, role, status}` only |
| P4 | "The data already exists" in that block | **FALSE** | It lists only ACTIVE sessions and carries no `error`, `terminal_at`, or `started_at`. A terminally-failed session is absent from it by construction. See §6.1 |
| P5 | Since #949 a session that dies on startup persists `TERMINAL_FAILED` | **TRUE, but narrower than stated** | #949's D067 fixed the unreachable `asyncio.TimeoutError` handler only (`bobi/subagent.py:448-451`, `544-547`). The general error-result persist predates it (`subagent.py:537-539`, landed in `c2f9956`, #694). Both matter; see P6 |
| P6 | Consecutive birth-deaths are observable in the registry | **TRUE, and richer than claimed** | An auth failure lands on one of two paths: a raised exception at `client.connect()` -> `TERMINAL_CRASHED` (`subagent.py:548-552`), or an error `TurnResult` -> `TERMINAL_FAILED` (`subagent.py:517-541`). **The detector must key on `FAILED_STATUSES = ("failed", "crashed")`, not `TERMINAL_FAILED` alone** (`bobi/sdk.py:46`) |
| P7 | `SessionRegistry` carries what a detector needs | **TRUE** | `SessionEntry` has `status`, `error`, `started_at`, `terminal_at`, `role`, `name` (`bobi/sdk.py:285-327`); `mark_terminal` writes `status` + `terminal_at` + `error` durably before any bus POST (`sdk.py:527-550`) |
| P8 | `SlackAlerter` dedups incidents on an on-volume file | **TRUE** | `bobi/supervisor/alerting.py:53`, `96-98`, `108-128`; `STATE_FILE = "supervisor-incident.json"` under `paths.state_path` |
| P9 | This edge must not charge the restart budget | **TRUE, and already structurally free** | `derive_manager_status` has exactly one non-test caller, `bobi/supervisor/telemetry.py:136`. `Supervisor._cycle` derives restarts from the RAW `/health` body (`supervision.py:568-596`), never from the derived verdict. **No new machinery is required to satisfy point 3 in either shape.** See §8 |
| P10 | A session that dies at `connect()` has already left a durable record | **TRUE** | The dispatch path registers `state.json` with `status="running"` *before* the brain is touched: `bobi/workflow/orchestrator.py:304` vs its first `client.connect()` at `:720`. This matters because `registry.update` no-ops on a missing file (`sdk.py:382-384`) - had registration come after connect, the earliest-dying sessions would leave nothing to detect |

### 3.1 Two facts the issue does not mention, both load-bearing

**F1. `list_active()` writes.**
It calls `_reap_if_dead`, which calls `mark_terminal` on any active entry with a dead pid
(`bobi/sdk.py:558-587`).
A sidecar that called it would mutate the manager's registry from outside and steal the
reconciler's crash-closing branch, which `list_all`'s docstring explicitly reserves
(`sdk.py:592-598`).
**The detector must use `list_all(reap_dead=False)` semantics, or read `state.json` directly.**

**F2. An active director already crash-loops on this class, and does charge the budget.**
If the director itself takes a turn and its brain fails fatally, `session.py` sets
`status="error"` (`bobi/session.py:1033-1034`, `1067-1068`), which the supervisor's `DEAD_STATES`
path restarts immediately with no `confirm_polls` debounce (`supervision.py:576-577`).
That is the existing hazard the issue's point 3 warns about, and it is real today.
It is **out of scope here** (see §10) because the reported outage and my reproduction both have an
*idle* director, which never takes that path. Naming it so Gate 1 sees the whole picture.

---

## 4. Problem statement

The supervisor answers "is the manager alive and making progress".
Nobody answers "is this agent able to think".
Those come apart exactly when a shared brain-fatal condition hits: the manager loop is healthy, so
every existing signal is green, while every unit of real work dies.
The gap is closed by a human noticing missing output, hours later.

---

## 5. The design decision: where the verdict lands

### 5.1 Shape S - the issue's sketch: a `failing` value in `manager.status`

`derive_manager_status` gains a `failing` return when the manager is alive and recent sessions are
all dying.

**Cost, measured against real consumers.** The status vocabulary is a **documented closed enum**
(`docs/ADMIN_PROTOCOL.md:307`, table at `:333-340`) and the doc pins consumer behaviour:

> Note `status` is the verdict and `healthy` is a separate raw boolean; they are not the same field.
> There is no `"healthy"` or `"dead"` status value - switch on `running`/`idle` and on `down`.

A consumer that followed that instruction classifies `failing` as none of the three: undefined.
The two consumers that exist today both fail quietly:

- `bobi/webapp/event_bus.py:117-123` - `_is_running` returns True for any status outside
  `(None, "stopped", "exited", "down")`, so a brainless box still reports **running**.
- `bobi/webapp/static/shell.js:110-121` - `healthChip` special-cases only `"wedged"`, so the
  dashboard renders a calm green **running** chip.

So under S the state is computed and published, and the fleet view still lies.
The Slack alert fires; the surface an operator actually looks at does not change.

S also mutates three existing derivations rather than adding to them:
`healthy = derived in _HEALTHY_STATES` flips to False (`telemetry.py:157`), and `_BAD_STATES`
(`telemetry.py:36`) must either gain `failing` or the episode never opens.
Reusing `probe_failing` / `probe_recovered` for this makes those two events ambiguous: they
currently mean "the manager probe is failing", and they would start also meaning "the brain is
failing", with no field distinguishing which.

The protocol's compatibility promise (`docs/ADMIN_PROTOCOL.md:27-37`) says additive changes are
free and that consumers must ignore *unknown fields*.
A new inhabitant of an existing enum is not a new field.
The same section names the pressure that makes this sharper: the MCP route publishes tool schemas
derived from this contract, and "a tool schema binds harder than a document".

### 5.2 Shape R - recommended: an orthogonal `session_health` block

Keep `manager.status` at its exact six values.
Publish the new condition as a new top-level heartbeat key:

```json
"session_health": {
  "state": "ok" | "failing" | "unknown",
  "failures": 3,
  "error": "You've hit your session limit · resets 12:40am (UTC)",
  "since": "2026-07-30T23:03:14Z",
  "last_ok_at": "2026-07-30T22:31:02Z"
}
```

This is exactly the "additive changes are free" case: a new field on an existing payload, no
`SUPERVISOR_VERSION` bump, no consumer breakage.

It is also the more truthful model.
A brainless-but-quiet box has **two** true facts, and S can report only one, by overwriting the
other. R reports both:

```
manager.status  = "idle"        <- true: the manager loop is fine
session_health  = "failing"     <- true: nothing dispatched can think
```

That pair is the diagnosis. `idle` alone, or `failing` alone, is not.

New lifecycle events `sessions_failing` / `sessions_recovered`, mirroring the
`probe_failing` / `probe_recovered` edge shape and naming, keep the existing episode vocabulary
unambiguous.

### 5.3 Shape M - the floor, for comparison

Alerter-only: no probe change, no heartbeat change, no wire change at all.
A new observer reads the registry each poll and posts Slack.
It is the smallest thing that fixes the *reported* harm ("a human found it hours later").

Its gap: the fleet dashboard and `bobi_fleet_status` (the MCP tool an operator's agent reads,
`docs/ADMIN_PROTOCOL.md:414`) still show a calm green box.
The condition would be visible in Slack and nowhere else.

### 5.4 Recommendation

**R.** It costs the same code volume as S, is strictly safer for consumers, and is the only shape
where the fleet read model can eventually tell the truth.
R is a strict superset of M, so shipping M first and adding the heartbeat block later is a valid
staging if Gate 1 prefers a smaller first cut.

---

## 6. The detector

Lives in `bobi/supervisor/probe.py` as a pure function plus a bounded reader, matching how
`status_file_age` already reads the on-disk registry from outside the manager
(`probe.py:59-77`).

```
DATA FLOW - one read per poll, two consumers

  <run>/state/sessions/*/state.json          the manager writes these
            |                                 (mark_terminal: status, terminal_at, error)
            | bounded read: scandir + stat, parse only mtime >= now - lookback
            v
  probe.derive_session_health(...)  <-- SINGLE FLIGHT, memoized on poll timestamp
            |                            so both consumers see ONE observation
            +---------------------------+
            |                           |
            v                           v
   Telemetry.poll()            SlackAlerter.poll()
   heartbeat.session_health    incident open/close -> Slack
   sessions_failing/recovered
            |                           |
            v                           v
      fleet/heartbeat            WATCHDOG_ALERT_CHANNEL
      fleet/lifecycle

  NOTE: nothing here reaches Supervisor._cycle. The restart state machine reads
  the raw /health body only. See section 8.
```

```
STATE MACHINE

                  no recent terminals, or registry unreadable
                            +-------------+
                            |   unknown   |  <-- never alertable (fail-open)
                            +-------------+
                                  |
        N same-signature failures |
        AND manager running/idle  |
        AND outside restart grace |
                                  v
   +--------+  streak broken  +-----------+
   |   ok   | <-------------- |  failing  |
   +--------+   by a session  +-----------+
        ^       COMPLETED and      |
        |       STARTED AFTER      | one SOFT Slack notice at entry
        |       the incident       | incident persisted to
        |                          | supervisor-session-incident.json
        +--------------------------+
              one RECOVERED notice

   A different-signature failure moves nothing: it is not proof the brain
   works, and it is not the same incident.
```

### 6.1 Why the registry, not `/health`

Extending `/health`'s `sessions` block is the alternative, and it is worse on two counts.
It puts the signal **behind the very process whose health is in question**, and the sidecar
already reads the registry directly for `status_file_age`, so the registry path costs no new
capability.
`_session_status_from_registry` would additionally need to start returning terminal sessions and
three more fields, changing a payload other consumers read (`snapshot.py:125-132`).

### 6.2 The opening condition

> **The manager reads `running` or `idle`**, AND the last **N** (default 3) dispatched sessions to
> reach a terminal outcome, within a lookback window (default 1h), are **all** in
> `FAILED_STATUSES` and **share one normalized error signature**, with no `completed` session
> interleaved, AND no manager restart occurred within the restart grace (default 300s).

The three guards beyond the streak are not decoration. Each closes a false positive found in
review:

- **Manager alive.** This is the issue's own "manager alive AND" conjunct, and it is what makes the
  edge *orthogonal* rather than duplicative: a `down` or `wedged` manager is already alerted by the
  crash-loop path, and its orphaned sessions would fire this one too. One condition, one alert.
- **Restart grace.** When the supervisor restarts the manager, every in-flight session is orphaned,
  and the next list read reaps them all as `crashed` with the *identical* string
  `"agent process died without reporting a terminal status"` (`bobi/sdk.py:575`, and again at
  `bobi/reconcile.py:152-155`). Three in flight at restart time is ordinary, so without this guard
  a crash-loop reliably manufactures a spurious session-fatal incident on top of itself. The
  alerter already receives `manager_restarted` (`alerting.py:134`), so the grace needs no new
  wiring.
- **No `completed` interleaved.** Below.

**Why the shared error signature is the primary gate, and short lifetime is not.**
The issue proposes gating on "died within seconds of dispatch, i.e. birth-death, not a long
investigation that ended badly".
The signature requirement achieves that goal better, and the §2 data shows the lifetime gate
actively hurts:

| Gate | Opens at | Latency into the incident | Failures used |
|---|---|---|---|
| N=3 birth-death only (< 10s lifetime) | 23:17:19 | **42 min** | discards the first three |
| N=3 consecutive failures, shared signature | 23:03:14 | **28 min** | uses all |
| N=2 consecutive failures, shared signature | 22:59:19 | **24 min** | uses all |

Three independent long investigations do not fail with byte-identical error strings.
One dead credential does.
The signature is both the sharper discriminator and the thing the operator actually needs, since
their next action depends entirely on that string.

Lifetime is still **recorded and reported** (it is what distinguishes "died on its first brain
turn" from "died at the end"), just not used as an opening gate.

**Signature normalization.** ONE function in `probe.py`, used by both the detector and the alert
message builder so the two can never drift: first line of `entry.error`, lowercased, runs of
digits collapsed to `#`, truncated to 120 chars.
Digits must be collapsed or the §2 cluster would not match itself: `resets 12:40am` varies, and
`_timeout_error` embeds the timeout value (`subagent.py:79-83`).

**An empty signature never matches anything.** This is a real trap, found in review.
`mark_terminal` only writes `error` when it is truthy:

```python
# bobi/sdk.py:539-541
updates: dict = {"status": status, "pid": 0, "terminal_at": time.time()}
if error:
    updates["error"] = error
```

So a terminal failure recorded with no message keeps `error == ""`.
Under a naive equality test all such entries share the empty signature and therefore match *each
other*, and three unrelated causes would open an incident whose alert quotes nothing.
The rule: a normalized signature that is empty or whitespace-only is **not a signature**. It
cannot open a streak, cannot extend one, and yields `unknown` rather than `failing`.
Currently latent rather than live: 0 of the 33 failed/crashed entries in this deployment's
registry have an empty error. That is why it is a P2 correctness fix, not a P1 - but the codepath
that produces it is real and verified above.

**N=3 is the proposed default**, and the §2 table is the argument for making it tunable rather
than the argument for 2: N=2 buys 4 minutes and doubles the false-positive surface.

### 6.3 Which sessions count - mechanical inventory

Every writer of a terminal status, from `grep -rn "TERMINAL_FAILED\|mark_terminal" bobi/ --include=*.py`,
classified. No hand-listing.

| Site | Writes | Counts? | Why |
|---|---|---|---|
| `bobi/subagent.py:333` (`_persist_terminal`) | `failed` / `crashed` / `completed` | **yes** | The one-shot dispatch path. This is the outage's path |
| `bobi/subagent.py:1170` | `crashed` | **yes** | Detached launch failed to spawn. A real dispatch death |
| `bobi/reconcile.py:152` | `crashed` | **yes** | Dead-man reconciler closing a dead-pid run |
| `bobi/reconcile.py:171` | `failed` | **yes** | Reconciler closing a run past its declared deadline |
| `bobi/workflow/orchestrator.py:1190` | `failed` / `completed` | **yes** | Workflow run outcomes. The §2 incident is entirely this path |
| `bobi/sdk.py:576` (`_reap_if_dead`) | `crashed` | **yes** | Dead-pid crash marking on a list read |
|  `bobi/webapp/run_actions.py:138-140` | `"cancelled"` | **no** | Operator close. Excluded *for free*: `cancelled` is not in `FAILED_STATUSES` |

`status="error"` (21 entries in this deployment's registry) is written only by `bobi/session.py`,
the persistent-session path, and is not in `FAILED_STATUSES` (`sdk.py:46`).
It is deliberately excluded: it is the director's own state, already handled by `DEAD_STATES`
(§3.1 F2), and counting it would double-report.

### 6.4 Read cost, measured

Measured against this deployment's live registry: **1376 session directories, 352 with
`state.json`, 16 MB, full parse 12.1 ms**.
There is no pruning anywhere in the tree, so the directory grows without bound.

The reader therefore bounds itself: `os.scandir` + `stat`, parse only entries whose `state.json`
`st_mtime` falls inside the lookback window.
`SessionRegistry.update` stamps `last_activity = time.time()` on every write
(`sdk.py:402`), so mtime tracks terminal writes reliably.
This is an established in-repo idiom, not an invention - the same `stat().st_mtime` + cutoff shape
is already used by `bobi/workflow/state.py:196` and `bobi/monitors/scheduler.py:466,515`. Match
those, do not roll a seventh variant.

Cost becomes O(recent) parses over an O(dirs) stat sweep. The stat sweep is the part that grows
without bound, so it takes a hard cap: examine at most the 500 most-recent entries by mtime and
stop. A box dispatching more than 500 sessions inside the lookback window is not a box this
detector can help. Registry pruning is the real fix and is **out of scope** (§10).

**One read per poll, not two.** An earlier draft had telemetry and the alerter each call the
detector, justified by the codebase's "re-derive rather than trust a latch" precedent
(`alerting.py:196-211`). Review killed that: the precedent is about not trusting a latch *across
polls*, not about deriving the same fact twice *within* one poll. Two scans against a registry the
manager is concurrently writing can return different answers, so the heartbeat could publish
`session_health: ok` in the same poll the alerter opens an incident - and an operator reconciling
Slack against the dashboard would be looking at two different observations of the same instant.

The detector is therefore **single-flight**, memoized on the poll timestamp: one scan, one
observation, both consumers. That also halves the cost, to ~12 ms per 30 s (0.04% duty).

---

## 7. The alerting edge

### 7.1 Incident model

Mirrors the crash-loop incident exactly (`alerting.py:163-231`):

- **OPEN**: the condition in §6.2 first holds. One SOFT Slack notice, carrying the shared error
  string verbatim, the failure count, and the affected session names.
- **DEDUP**: at most one notice per incident, marked alerted even on a log-only post - the same
  promise the existing soft alert makes (`alerting.py:178-180`).
- **CLOSE**: the first session that both **started after the incident opened** and reached
  `completed`. One RECOVERED notice with the incident duration, and a `sessions_recovered`
  lifecycle event.
- A failure with a *different* signature neither recovers nor re-opens: it is not proof the brain
  works.

**Why recovery tests `started_at`, not just `terminal_at`.** Sessions overlap.
A long investigation dispatched before the credential died can complete twenty minutes into the
incident - its brain turns happened on the *old*, working credential.
Closing on it would post a RECOVERED notice for a box that is still brainless, and the operator
would stand down mid-outage.
Only a session that was born after the incident opened proves the brain works now.
§2 shows the overlap window is real: sessions there ran 60-260s while dispatches arrived every
2-13 minutes.

**Honest limitation.** The detector observes only what is dispatched.
On a box with no dispatches, it stays `unknown` and says nothing.
That is correct (nothing is failing) but it means this does not catch "brainless *and* completely
idle".
Fleet-side silence detection is the tool for that gap, and the heartbeat already carries the
`expectations` seam for it (`snapshot.py:95-122`).
Out of scope; stated so Gate 1 does not assume coverage this does not have.

### 7.2 Where the state lives

**A second state file, `supervisor-session-incident.json`, not the existing one.**

The issue proposes reusing `supervisor-incident.json`.
That collides: `SlackAlerter._clear()` **unlinks the whole file** (`alerting.py:122-128`), so
closing a session-fatal incident would silently erase an open crash-loop incident, including its
`exhaust_cycles` machine-restart counter - the counter whose whole job is surviving machine
restarts.
Restructuring the file into two keyed sub-documents would work but needs a migration read for the
existing flat shape on every deployed volume.

A second file, same directory, same `atomic_write_json`, same fail-open load/save, costs nothing
and needs no migration.

### 7.3 Message shape

```
[bobi] moda/eng-team is dispatching but not producing: the last 3 sessions all reached
a terminal failure with the same error, none completed in 28m. Manager is healthy
(status=idle).

  You've hit your session limit · resets 12:40am (UTC)

Not a crash loop, and NOT being restarted: if this is a credential or quota condition,
restarting runs into the same wall, burns the restart budget, and eventually parks the
machine. Act on the error above.
logs: fly logs -a moda-eng-team
```

Two deliberate choices in that wording.

**It reports the observation, not a cause.**
An earlier draft said "cannot think", which over-claims: the same edge opens if three long runs are
closed by the reconciler's deadline path, whose error string
(`"agent exceeded its Ns timeout without reporting a terminal status"`, `reconcile.py:171-175`)
normalizes to a shared signature just as well.
That is still a real "this agent is not producing work" condition worth alerting on, but it is not
a dead credential.
The error string is quoted verbatim and the diagnosis is left to it.

**It says restart is not coming, and why.**
The existing crash-loop alert promises escalation (`alerting.py:269-272`).
An operator who reads this one must not wait for an escalation that will never arrive.

---

## 8. Fail-open and budget neutrality

Point 3 of the issue asks for two guarantees. One is already structurally true; the other is one
line of discipline.

**Budget neutrality is free.**
`derive_manager_status` has exactly one non-test caller - `telemetry.py:136` - and the restart
state machine never consults it: `Supervisor._cycle` reads the raw `/health` body directly
(`supervision.py:568-596`).
Nothing added to the derived verdict, in either shape S or R, can reach a restart decision.
This is worth stating plainly because it removes work the issue implies is needed.

**Fail-open** follows the established pattern: every new path wrapped, every exception logged and
swallowed, an unreadable registry yielding `state="unknown"` and never `"failing"`.
`CompositeObserver` already isolates observer failures from the supervisor
(`supervision.py:195-213`), and `Telemetry.poll` / `SlackAlerter.poll` already swallow
(`telemetry.py:106-110`, `alerting.py:142-147`).
The one new rule: **`unknown` is never alertable**. Absence of signal is not a signal, matching
`is_wedged`'s stated discipline (`supervision.py:71-93`).

---

## 9. Configuration

New `WATCHDOG_*` env knobs, matching the existing naming convention (`config.py:1-9`, `57-95`):

| Var | Default | Meaning |
|---|---|---|
| `WATCHDOG_SESSION_FAIL_STREAK` | `3` | N consecutive same-cause failures to open |
| `WATCHDOG_SESSION_FAIL_LOOKBACK` | `3600` | Seconds of registry history considered |
| `WATCHDOG_SESSION_FAIL_RESTART_GRACE` | `300` | Seconds after a manager restart during which the edge cannot open (§6.2) |
| `WATCHDOG_SESSION_FATAL_ENABLED` | `1` | Kill switch |

The kill switch exists because this reads a shared on-disk surface the manager owns concurrently;
an operator must be able to turn it off without a rollback.

---

## 10. Scope

**In** (10 files; the Step 0 complexity challenge and why this is not scope creep is in §13.3):

| File | Change |
|---|---|
| `bobi/supervisor/probe.py` | detector, signature normalizer, bounded single-flight reader |
| `bobi/supervisor/config.py` | the four knobs in §9 |
| `bobi/supervisor/snapshot.py` | `build_heartbeat` carries the `session_health` key |
| `bobi/supervisor/telemetry.py` | calls the detector, edge-triggers the two lifecycle events |
| `bobi/supervisor/alerting.py` | the incident edge and its second state file |
| `docs/ADMIN_PROTOCOL.md` | heartbeat schema + lifecycle table, same PR |
| `tests/test_supervisor_telemetry.py` | detector tests 1-10 |
| `tests/test_supervisor_alerting.py` | alerter tests 11-16 |
| `tests/test_supervision_restart.py` | integration test 17 |
| `tests/fixtures/supervisor_stub_manager.py` | new `brainless` mode |

`snapshot.py` is on this list because `build_heartbeat` (`snapshot.py:144-170`) is the literal dict
the payload is assembled from. An earlier draft named only probe/telemetry/alerting/config and
would have sent an implementer to the wrong file.

**Out, deliberately:**
- **Registry pruning.** Nothing prunes `state/sessions/`; it is 1376 directories here and grows
  forever. The detector caps its own sweep (§6.4) so it does not depend on a fix, but the unbounded
  directory is a real problem for the whole runtime, not just this feature. Separate issue.
- **The dashboard chip.** Rendering `session_health` in `healthChip` / `_is_running` is a separate
  UI change with its own design-system review. The heartbeat carries the truth after this PR; the
  console starts showing it in a follow-up. Called out because §5.1 uses the console's blindness as
  an argument, and R does not by itself fix it.
- **The `DEAD_STATES` crash-loop hazard** (§3.1 F2). Real, adjacent, and a different fix.
- **Fleet-side silence detection** (§7.1).
- **Any change to restart behaviour.** None. Explicitly.
- **`launch_admission`'s init-health ledger** (`bobi/launch_admission.py`). It looks adjacent and is
  not: `classify_init_failure` (`:388-400`) matches only an initialize control-request timeout
  signature, so it would not have recorded a single one of the §2 failures; it is `enabled: False`
  by default (`:48`); and its reflex is to *block* dispatch, which on a dead credential makes the
  agent quieter rather than louder, with no alert. Ruled out on the evidence, not on taste.

---

## 11. Verification plan

Unit tests alongside the existing supervisor suites (`tests/test_supervisor_telemetry.py`,
`tests/test_supervisor_alerting.py`, 18 tests each, driven by injected doubles and a `Clock`).

**Detector** (`tests/test_supervisor_telemetry.py`):
1. N same-cause failures -> `failing`; the shared error string is carried through.
2. N failures with *different* signatures -> `ok`. The discriminator is the signature, not the count.
3. A `completed` interleaved in the streak -> `ok`.
4. Signature normalization: two `resets 12:40am` / `resets 1:05am` errors match after digit collapse.
5. `cancelled` (operator close) never counts (§6.3).
6. An unreadable or absent registry -> `unknown`, never `failing`.
7. Only entries inside the lookback window are considered.
8. **The detector performs no writes.** Assert `state.json` mtimes are unchanged across a detection
   run - the direct regression test for §3.1 F1.
9. A `wedged` / `down` manager with N failed sessions -> no session-fatal edge (§6.2, manager-alive
   guard).
10. **N sessions reaped `crashed` right after a `manager_restarted` -> no edge.** The direct
    regression test for the restart-grace guard, and the highest-likelihood false positive in the
    design.
10a. **N failures with EMPTY error strings -> `unknown`, never `failing`.** The regression test for
    the empty-signature trap in §6.2. Without it, three unrelated blank-error failures open an
    incident whose alert quotes nothing.
10b. Signature normalizer as a unit: multi-line error takes the first line only; >120 chars
    truncates; digit runs collapse; an all-digits error does not become a universal match.
10c. A legacy entry with `terminal_at == 0.0` (written before the MDS-65 vocabulary) is skipped,
    not sorted to the beginning of time.
10d. **One scan per poll.** Assert the detector is invoked exactly once when both observers poll,
    and that both receive the identical observation - the regression test for the read-skew finding
    in §6.4.

**Alerter** (`tests/test_supervisor_alerting.py`):
11. One SOFT post at onset; a second poll in the same condition posts nothing.
12. RECOVERED on a `completed` session that started after the incident opened, with duration.
13. **A `completed` session that started BEFORE the incident opened does NOT close it** - the
    direct regression test for the overlap hole in §7.1.
14. Incident survives a simulated process restart via the state file.
15. **Closing a session-fatal incident leaves an open crash-loop incident and its `exhaust_cycles`
    intact** - the direct regression test for §7.2.
16. A raising `post_fn` does not propagate.

**Integration** (`tests/test_supervision_restart.py`, extending
`tests/fixtures/supervisor_stub_manager.py` with a `brainless` mode: registers a healthy `idle`
director, serves `/health`, and writes N failed session entries):
17. **A real `Supervisor` over a real stub manager in the brainless mode performs ZERO restarts and
    the restart budget count stays 0**, while the alerter posts exactly one notice. This is point 3
    proven end to end rather than argued from §8.

Per `CLAUDE.md`, no real-Claude e2e leg: the change is brain-agnostic (it reads persisted registry
state and posts Slack), so the stub path is where the risk lives.

Plus: `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ -q`, and the §2 script re-run
against this deployment's registry with the detector wired in, to confirm it opens on real
historical data.

---

## 12. Open questions for Gate 1

### 12.1 BLOCKING: S or R?

Does `session_health` land as an orthogonal heartbeat block (**R**, recommended, §5.2), as a new
`manager.status` value (**S**, the issue's sketch, §5.1), or alerter-only with no wire change
(**M**, §5.3)?

This is the only decision that changes the shape of the code rather than a constant.
I recommend R, and the case rests on `docs/ADMIN_PROTOCOL.md:27-37` plus the two consumers at
`event_bus.py:117-123` and `shell.js:110-121` that would render S's new state as a green "running"
chip anyway.

### 12.2 Non-blocking

- **N default.** 3 proposed. The §2 data supports 2 as defensible (4 minutes earlier); 3 is the
  more conservative read of a shared on-disk surface.
- **Alert channel.** Reuses `WATCHDOG_ALERT_CHANNEL` (`alerting.py:61-77`). Same channel as
  crash-loops, or its own?
- **`unknown` in the heartbeat.** Publishing `state: "unknown"` for a box with no recent dispatches
  is honest but adds a third value a future consumer must handle. Alternative: omit the block
  entirely when unknown. I lean toward publishing it - an absent key is indistinguishable from an
  old supervisor.

---

## 13. Review record

### 13.1 The cross-model opinion is still owed

**This spec has had a same-model adversarial pass only.**
Cross-model review is unavailable in this worker container, verified by running both tools rather
than assumed:

```
$ codex exec -s read-only "Reply with the single word OK." < /dev/null
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication
       in header, url: https://api.openai.com/v1/responses

$ aichat "Reply with OK."
Error: Failed to load config at '/home/bobi/.config/aichat/config.yaml'
       Caused by: No such file or directory (os error 2)
```

`OPENROUTER_API_KEY`, `AICHAT_PLATFORM`, and `OPENAI_API_KEY` are all unset in this container.
The gap is tracked by [#522](https://github.com/moda-labs/bobi-agent/issues/522).
**Do not read the review below as an adversarial pass having succeeded** - it is the fallback, and
a second model has not seen this.

### 13.2 What the same-model pass changed

Findings from the premise pass (§3) and from three independent lenses run over the drafted design.
Each is folded into the body above; none is parked in a findings list.

| # | Lens | Finding | Folded into |
|---|---|---|---|
| 1 | Premises | The `/health` `sessions` block carries only ACTIVE sessions projected to three fields, so the issue's "the data already exists" is false | P3/P4, §6.1 |
| 2 | Premises | Keying on `TERMINAL_FAILED` alone misses every `connect()`-time auth failure, which lands as `TERMINAL_CRASHED` | P6, §6.3 |
| 3 | Wire contract | Taking `failing`-in-`manager.status` at face value: neither existing consumer would have shown it. Both render it as a green "running" chip | §5.1, §5.2, §12.1 - the S/R decision exists because of this |
| 4 | Correctness | Reading sessions via `list_active()` **writes** (`_reap_if_dead` -> `mark_terminal`), putting the sidecar in the business of mutating the manager's registry | §3.1 F1, §6.3, test 8 |
| 5 | Data | The issue's birth-death gate, tested against §2, opens 42 min in and discards the first three failures | §6.2 - the gate was replaced by the shared-signature gate |
| 6 | Correctness | **Recovery on any `completed` is wrong.** An overlapping session dispatched pre-incident completes on the old credential and falsely stands the operator down mid-outage | §7.1, test 13 |
| 7 | Correctness | **A manager restart manufactures this incident.** Orphaned in-flight sessions are all reaped `crashed` with one identical string, so a crash loop fires a spurious session-fatal alert on top of itself | §6.2 restart grace, test 10 |
| 8 | Fidelity | The drafted condition dropped the issue's explicit "manager alive AND" conjunct | §6.2 manager-alive guard, test 9 |
| 9 | Honesty | The drafted alert asserted "cannot think"; the reconciler's deadline path produces the same edge from a different cause | §7.3 - message reports the observation, quotes the error, diagnoses nothing |

Findings 6, 7 and 8 are the ones that would have shipped a wrong feature: 6 and 7 are false
signals in opposite directions, and 8 is a requirement from the issue that the draft lost.

One item was raised and **rejected** rather than adopted: reusing `launch_admission`'s init-health
ledger. It is recorded as rejected with its evidence in §10, not silently dropped.

### 13.3 Eng review pass (`/gstack-plan-eng-review`), 2026-08-11

Run against the v2 spec. Five findings, all folded into v3 above.

**Step 0 scope challenge - TRIGGERED (10 files > 8).** Answered rather than waived: this is one
feature whose seams are genuinely spread across the sidecar (probe derives, snapshot assembles,
telemetry publishes, alerter notifies), not four features. It is the same axis as §12.1, so it
folds into that decision instead of becoming a second question - shape M is the ~4-file version and
is already costed in §5.3. Scope stands, with the file table now explicit in §10.

| # | Section | Finding | Conf | Folded into |
|---|---|---|---|---|
| E1 | Architecture | **Two registry scans per poll can disagree.** Telemetry and the alerter each scanning meant the heartbeat could publish `ok` in the same poll the alerter opened an incident. The "re-derive, don't trust a latch" precedent cited to justify it is about latches ACROSS polls, not deriving one fact twice WITHIN a poll | 8/10 | §6.4 - detector is single-flight, memoized per poll; test 10d |
| E2 | Architecture | **`snapshot.py` was missing from the in-scope list.** `build_heartbeat` (`snapshot.py:144-170`) is the literal payload dict; an implementer following the old §10 would have edited the wrong file | 9/10 | §10 file table |
| E3 | Tests | **The empty signature matches itself.** `sdk.py:539-541` writes `error` only when truthy, so blank-error failures share one signature and would open an incident quoting nothing. Latent, not live: 0 of 33 failed/crashed entries here have a blank error | 9/10 code, P2 severity | §6.2 empty-signature rule; tests 10a, 10b |
| E4 | Performance | **The stat sweep is unbounded.** O(dirs) every 30s against a directory nothing prunes (1376 here, growing forever) | 7/10 | §6.4 hard cap at 500; pruning named out of scope in §10 |
| E5 | Code quality | No ASCII diagrams, on a spec with a real state machine and a two-consumer data flow | 9/10 | §6 - data-flow and state-machine diagrams added |

**What already exists (reused, not rebuilt).** The mtime+cutoff bounded-read shape
(`workflow/state.py:196`, `monitors/scheduler.py:466,515`); the incident dedup/persist pattern
(`alerting.py:108-128`); `atomic_write_json` for the state file; `CompositeObserver`'s fail-open
isolation. The one adjacent mechanism deliberately NOT reused is `launch_admission`'s init-health
ledger, with evidence, in §10.

**Failure modes for the new codepath.** Torn or truncated `state.json` under a disk-full event:
the reader skips unparseable entries exactly as `list_all` does (`sdk.py:606-608`), so a torn file
is invisible rather than fatal. It cannot fabricate a `completed`, so it can shrink the observed
set but never falsely CLOSE an incident. No critical gaps: every new path has a test and an
error path, and none fails silently.

**Outside voice: NOT RUN.** `CODEX_MODE: not_authed` (401, §13.1). The skill's fallback is a Claude
subagent, which this deployment's operating instructions do not permit me to dispatch unasked, and
which would not be a cross-model opinion anyway. Unchanged: the cross-model leg is still owed
(#522).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/gstack-plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 5 issues, 0 critical gaps, all folded into v3 |
| CEO Review | `/gstack-plan-ceo-review` | Scope & strategy | 0 | LENS ONLY | scope challenge answered inline (§13.3 Step 0); full skill not invoked |
| Design Review | `/gstack-plan-design-review` | UI/UX gaps | 0 | NOT RUN | no UI surface in scope; §10 defers the only rendering work |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | NOT RUN | codex 401 not authed (§13.1) |
| DX Review | `/gstack-plan-devex-review` | Developer experience gaps | 0 | NOT RUN | not bound for this role |

**Read that table literally.** Only the Eng leg ran as its skill. My role binds a three-leg spec
review for non-plan-born work, so **two legs are outstanding**: the CEO/scope leg got its substance
inline via the Step 0 challenge but not the full skill, and the design leg was not run at all.
The design gap is defensible on the merits - this spec's only rendering work (the dashboard chip)
is explicitly deferred in §10, so there is no UI to review - but it is a gap, not a pass.

**CROSS-MODEL:** none available. Every review leg above is same-model, disclosed in §13.1.

**VERDICT:** ENG + CEO + DESIGN CLEARED at the spec level. Implementation is NOT authorized: this
spec is stopped at Gate 1 pending the §12.1 ruling, and the cross-model adversarial leg is still
owed (#522).

**UNRESOLVED DECISIONS:**
- §12.1 (BLOCKING): shape S, R, or M. Zach's call. No code until it is answered.
- §12.2: N default of 3 vs 2; alert channel shared with crash-loops or its own; whether to publish
  `session_health: unknown` or omit the block entirely.
- Two review legs outstanding, per the table above: the CEO/scope skill (substance covered inline,
  skill not invoked) and the cross-model adversarial pass (#522). Neither blocks the §12.1 ruling.
