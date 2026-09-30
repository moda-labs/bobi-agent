# 992 - a session-fatal edge for the supervisor probe

Status: **awaiting Gate 1** (design approval). No code written.
Issue: [#992](https://github.com/moda-labs/bobi-agent/issues/992)
Author: engineer agent, 2026-08-11. Re-validated 2026-08-21 against `main` @ [`ac2471e6`](https://github.com/moda-labs/bobi-agent/commit/ac2471e6).

Every file:line in this document was re-read at `ac2471e6`. Citations from the
2026-08-11 draft that pointed at `7025981` are corrected, not carried forward.

---

## 1. Re-validation verdict

The spec is still valid and **smaller than it was**. Three changes on `main`:

| # | What changed | Effect |
|---|---|---|
| V1 | [#855](https://github.com/moda-labs/bobi-agent/issues/855) shipped ([#1041](https://github.com/moda-labs/bobi-agent/pull/1041), 2026-08-19): `bobi/brain_availability.py` alerts on brain auth failure and credit exhaustion, deduped, with recovery | Removes roughly half the harm. §2 rebounds the residual |
| V2 | [#903](https://github.com/moda-labs/bobi-agent/issues/903) shipped ([#1022](https://github.com/moda-labs/bobi-agent/pull/1022)): load grace published `load_grace` as an **orthogonal additive heartbeat block**, not a new `manager.status` value | Settles Q1 (the only blocking question) by precedent. See §5.4 |
| V3 | The terminal-writer inventory gained four sites | §6.3 re-derived; one new false-positive surface, R3 |

Nothing invalidates the design. The premise table (§4) survives re-verification
with every claim intact and most line numbers moved.

**What Gate 1 is now being asked to approve** is narrower than the 2026-08-11
draft: an outcome-shaped backstop plus the fleet-visibility half, not the whole
"nobody is told" problem. §2 bounds it.

---

## 2. What #855 already closes, and what it cannot

`bobi/brain_availability.py::observe_brain_turn` (`:103-195`) opens a deduped
incident on a durable state file, emits one alert carrying the error text, and
emits `system/brain.recovered` with `outage_seconds` when a later turn
succeeds. It runs on `bobi/brain/turns.py:98`, inside `drain_turn`, which
**both** dispatch drivers import (`bobi/subagent.py:30`,
`bobi/workflow/orchestrator.py:30`).

That is Shape M (§5.3) already built, for two causes. Three gaps remain, each
verified here rather than argued.

**G1. The classifier does not match this deployment's actual outage.**
`classify_brain_unavailability` (`bobi/brain/base.py:130-145`) matches fixed
substrings. Run against §3's evidence at `ac2471e6`:

```
$ python3 -c "from bobi.brain.base import classify_brain_unavailability as c; ..."
'<no match>'            <- "You've hit your session limit · resets 12:40am (UTC)"
'credits_exhausted'     <- "You've hit your usage limit · resets 12:40am (UTC)"
'<no match>'            <- "agent process died without reporting a terminal status"
'authentication_failed' <- "Failed to authenticate: OAuth session expired"
```

`_CREDIT_ERROR_TEXT` (`base.py:118-127`) carries `"you've hit your usage
limit"`. The string that took this box down for 102 minutes says **session**
limit. It does not match, and today's `main` would not alert on it.

This is deliberate, not a typo to patch: the classifier's docstring reserves
itself for "messages that say the operator must sign in or replenish an
account" and leaves rate-limit and overload transient. Widening it to catch a
resetting session limit would re-open the exact transient/terminal confusion
#855 closed. The classifier is right; it just does not cover this.

**G2. A `connect()`-time death is structurally invisible to it.**
`observe_brain_turn` fires only on a `TurnResult` (`brain/turns.py:96-102`).
`connect()` is never a turn (`subagent.py:477-478`, #1016), and a raise there
propagates to `subagent.py:546-551`, which records `crashed` in the registry
and emits no brain-availability alert. P6 shows `connect()` is one of the two
paths a revoked credential takes.

**G3. The alert reaches the bus, not a person.** `system/brain.auth.failed`,
`system/brain.credits.exhausted`, and `system/brain.recovered` are documented
(`docs/EVENT_SERVER.md:307-309`) and emitted. Nothing in this repo or in this
deployment's installed package subscribes to them (`grep -rn "system/brain"`
over `agents/` and the installed `run/package/` returns no subscriber). Routing
them is a deployment change, not this spec's work, but it means the "a human
found it hours later" harm is not yet closed in practice.

**The residual this spec owns.** An outcome-shaped detector keys on *what
happened to dispatched sessions*, not on *which error text a classifier
recognizes*. It therefore covers G1 and G2 by construction, and covers the
whole class the issue names (a bad model pin, a gateway misconfiguration, a
quota shape nobody has seen yet) without anyone adding a substring. It is a
backstop under #855, not a replacement for it: #855 is faster and names the
cause, and should stay the primary signal.

Per the team's fewer-tickets preference, this does **not** absorb another
ticket and should not be folded into one. #855, #934, and #903 are all closed;
there is no open sibling to merge with.

---

## 3. Evidence: this already happened here, for 102 minutes

The issue cites Barndoor's 2026-08-05 outage (revoked OAuth token)
second-hand. The same failure class is in **this deployment's own registry**
(`/data/.bobi/agents/eng-team/run/state/sessions/`), from a different root
cause. Re-run 2026-08-21, unchanged:

```
window 07-30 22:35:15 .. 07-31 00:17:05 UTC  (101.8 min)
ALL sessions STARTED inside it: 13   Counter({'failed': 13})

  22:35:15 failed  life= 260.2s  wf-pr-closed-eng-team-adhoc-4e7dfe34
  22:59:19 failed  life=  85.7s  wf-pr-closed-eng-team-adhoc-80b6032f
  23:03:14 failed  life=  60.8s  wf-pr-closed-eng-team-adhoc-60014791
  23:11:45 failed  life=   2.2s  wf-pr-closed-eng-team-adhoc-7d3deb65
  ... 9 more, all failed ...
  00:17:03 failed  life=   2.2s  wf-pr-closed-eng-team-adhoc-e7f6d9f9

  every one:  error = "You've hit your session limit · resets 12:40am (UTC)"
  completed sessions ending inside the window: 0
```

Reproduce with:

```bash
python3 - <<'PY'
import json, glob, collections
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

Three consequences for the design:

- **The failure class is broader than a revoked token.** A subscription rate
  limit produces the identical shape: manager healthy, every dispatch dies,
  nobody told. Anything brain-fatal and shared-cause qualifies.
- **The condition can be self-healing.** The limit resets at 12:40am on its
  own. That is why the RECOVERED notice exists, and it confirms the issue's
  point 3: restarting is the wrong reflex for a condition the box cannot fix
  and time may.
- **"Died within seconds" is the wrong primary gate.** §6.2 shows it would
  have opened this incident 42 minutes in, discarding the first three failures.

This window is also G1's proof: none of these 13 entries would classify under
`classify_brain_unavailability` today.

---

## 4. Premises, re-verified at `ac2471e6`

| # | Claim (from the issue) | Verdict | Evidence |
|---|---|---|---|
| P1 | `probe.py::derive_manager_status` fuses three manager-only signals into the verdict | **TRUE** | `bobi/supervisor/probe.py:88-132`. The file is byte-identical to the 2026-08-11 baseline |
| P2 | An alive-but-brainless manager reads as `idle` | **TRUE** | `probe.py:118-119` returns the reported status verbatim when a process is alive and not wedged |
| P3 | `manager_health.py`'s `/health` body already carries a `sessions` block | **TRUE, useless here** | `bobi/manager_health.py:87`, `146-151`: `_session_status_from_registry` returns `list_active()` projected to `{name, role, status}` |
| P4 | "The data already exists" in that block | **FALSE** | It lists only ACTIVE sessions and carries no `error`, `terminal_at`, or `started_at`. A terminally-failed session is absent by construction. See §6.1 |
| P5 | Since #949 a session that dies on startup persists `TERMINAL_FAILED` | **TRUE, narrower than stated** | #949's D067 fixed the unreachable `asyncio.TimeoutError` handler only (`bobi/subagent.py:448-456`, `542-545`). The general error-result persist predates it (`subagent.py:517-540`, `c2f9956`, #694) |
| P6 | Consecutive birth-deaths are observable in the registry | **TRUE, richer than claimed** | An auth failure lands on one of two paths: a raise at `client.connect()` -> `TERMINAL_CRASHED` (`subagent.py:546-551`), or an error `TurnResult` -> `TERMINAL_FAILED` (`subagent.py:517-540`). **Key on `FAILED_STATUSES = ("failed", "crashed")`, not `TERMINAL_FAILED` alone** (`bobi/sdk.py:44`) |
| P7 | `SessionRegistry` carries what a detector needs | **TRUE** | `SessionEntry` has `status`, `error`, `started_at`, `terminal_at`, `role`, `name` (`sdk.py:268-326`); `mark_terminal` writes `status` + `terminal_at` + `error` durably before any bus POST (`sdk.py:509-532`) |
| P8 | `SlackAlerter` dedups incidents on an on-volume file | **TRUE** | `bobi/supervisor/alerting.py:53`, `96`, `108-128`; `STATE_FILE = "supervisor-incident.json"` under `paths.state_path` |
| P9 | This edge must not charge the restart budget | **TRUE, already structurally free** | `derive_manager_status` still has exactly one non-test caller, `telemetry.py:141`. `Supervisor._cycle` (`supervision.py:687`) derives restarts from the RAW `/health` body, never from the derived verdict. No new machinery satisfies point 3, in either shape. See §8 |
| P10 | A session that dies at `connect()` has already left a durable record | **TRUE** | Dispatch registers `state.json` with `status="running"` *before* the brain is touched: `orchestrator.py:321` vs its first `client.connect()` at `:872`. `registry.update` no-ops on a missing file (`sdk.py:363-366`), so registration order is what makes the earliest-dying sessions detectable |

### 4.1 Two facts the issue does not mention

**F1. `list_active()` writes.** It calls `_reap_if_dead`, which calls
`mark_terminal` on any active entry with a dead pid (`sdk.py:544-565`). A
sidecar calling it would mutate the manager's registry from outside and steal
the reconciler's crash-closing branch, which `list_all`'s docstring explicitly
reserves (`sdk.py:571-580`). **The detector must use `list_all(reap_dead=False)`
semantics, or read `state.json` directly.**

**F2. An active director already crash-loops on this class, and does charge
the budget.** If the director takes a turn and its brain fails fatally,
`session.py:689-690` sets `status="error"`, which the supervisor's
`DEAD_STATES` path restarts with no `confirm_polls` debounce
(`supervision.py:740-745`).

Load grace (V2) narrowed this but did not remove it. `_defer_for_load`
(`supervision.py:741`) defers a `dead_director` verdict only while the host is
pegged AND the manager's own descendant tree is consuming that CPU
(`bobi/supervisor/load.py:1-15`). A brainless box is by definition not
consuming CPU, so grace will not defer it. F2 stands for this spec's condition.
It is **out of scope** (§10): the reported outage and §3's reproduction both
have an *idle* director, which never takes that path.

---

## 5. The design decision: where the verdict lands

### 5.1 Shape S - the issue's sketch: a `failing` value in `manager.status`

`derive_manager_status` gains a `failing` return when the manager is alive and
recent sessions are all dying.

The status vocabulary is a documented closed enum
(`docs/ADMIN_PROTOCOL.md:314`, table at `:338-345`) and the doc pins consumer
behaviour (`:347-349`):

> Note `status` is the verdict and `healthy` is a separate raw boolean; they are
> not the same field. There is no `"healthy"` or `"dead"` status value - switch
> on `running`/`idle` and on `down`.

A consumer that followed that instruction classifies `failing` as none of the
three. Both consumers that exist today fail quietly:

- `bobi/webapp/event_bus.py:117-125` - `_is_running` returns True for any
  status outside `(None, "stopped", "exited", "down")`, so a brainless box
  still reports **running**.
- `bobi/webapp/static/shell.js:121-132` - `healthChip` special-cases only
  `"wedged"`, so the dashboard renders a calm green **running** chip.

Under S the state is computed and published, and the fleet view still lies.

S also mutates three existing derivations rather than adding to them:
`healthy = derived in _HEALTHY_STATES` flips to False (`telemetry.py:162`), and
`_BAD_STATES` (`telemetry.py:36`) must gain `failing` or the episode never
opens. Reusing `probe_failing` / `probe_recovered` makes those two events
ambiguous: they currently mean "the manager probe is failing" and would start
also meaning "the brain is failing", with no field distinguishing which.

The compatibility promise (`docs/ADMIN_PROTOCOL.md:27-31`) makes additive
changes free and requires consumers to ignore *unknown fields*. A new
inhabitant of an existing enum is not a new field.

### 5.2 Shape R - recommended: an orthogonal `session_health` block

Keep `manager.status` at its exact six values. Publish the condition as a new
top-level heartbeat key:

```json
"session_health": {
  "state": "ok" | "failing" | "unknown",
  "failures": 3,
  "error": "You've hit your session limit · resets 12:40am (UTC)",
  "since": "2026-07-30T23:03:14Z",
  "last_ok_at": "2026-07-30T22:31:02Z"
}
```

A new field on an existing payload: no `SUPERVISOR_VERSION` bump, no consumer
breakage.

It is also the more truthful model. A brainless-but-quiet box has **two** true
facts, and S can report only one, by overwriting the other:

```
manager.status  = "idle"        <- true: the manager loop is fine
session_health  = "failing"     <- true: nothing dispatched can think
```

That pair is the diagnosis. Either alone is not.

New lifecycle events `sessions_failing` / `sessions_recovered`, mirroring the
`probe_failing` / `probe_recovered` edge shape, keep the episode vocabulary
unambiguous.

### 5.3 Shape M - the floor

Alerter-only: no probe change, no heartbeat change, no wire change. An observer
reads the registry each poll and posts Slack.

V1 changed M's standing. #855's `brain_availability.py` is M's architecture,
already shipped, for two causes. Choosing M here means building a second
cause-agnostic alerter beside a cause-specific one and getting no fleet
visibility from either. M is now the weakest of the three, not the cheap
option.

### 5.4 Recommendation: R, and the precedent now decides it

Load grace (V2) faced this exact fork three weeks after the draft and chose R.
It publishes `load_grace` as a top-level heartbeat block
(`snapshot.py:168`; `docs/ADMIN_PROTOCOL.md`, "Load grace":
"`load_grace` is an additive block"), and it did **not** add a `deferred` value
to `manager.status`, even though its whole subject is which liveness verdict to
report.

That is the same team, the same payload, the same trade, on the inverse failure
mode (#903), decided the opposite way from the issue's sketch. R was the
recommendation on the merits before the precedent existed; the precedent makes
S the outlier.

---

## 6. The detector

Lives in `bobi/supervisor/probe.py` as a pure function plus a bounded reader,
matching how `status_file_age` already reads the on-disk registry from outside
the manager (`probe.py:59-77`).

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

### 6.1 Why the registry, not `/health` or the lifecycle bus

Extending `/health`'s `sessions` block puts the signal **behind the very
process whose health is in question**, and
`_session_status_from_registry` would need to start returning terminal
sessions plus three more fields, changing a payload other consumers read
(`snapshot.py:130`). The sidecar already reads the registry directly for
`status_file_age`, so the registry path costs no new capability.

Reading the lifecycle bus instead is worse again, and there is a live defect
that shows why: `reconcile.py:176-188` re-emits any terminal entry whose
`emit_confirmed` is still False on **every** reconciler wake, so a box whose
bus POSTs are failing produces a repeating stream of `agent/session.completed`
for sessions that completed long ago. A bus-reading detector would see that as
proof of health. The registry is the durable record `mark_terminal` writes
before any POST (P7), so it is unaffected.

### 6.2 The opening condition

> **The manager reads `running` or `idle`**, AND the last **N** (default 3)
> dispatched sessions to reach a terminal outcome, within a lookback window
> (default 1h), are **all** in `FAILED_STATUSES` and **share one normalized
> error signature**, with no `completed` session interleaved, AND no manager
> restart occurred within the restart grace (default 300s).

The three guards beyond the streak each close a false positive found in review:

- **Manager alive.** The issue's own "manager alive AND" conjunct, and what
  makes the edge orthogonal rather than duplicative: a `down` or `wedged`
  manager is already alerted by the crash-loop path, and its orphaned sessions
  would fire this one too.
- **Restart grace.** When the supervisor restarts the manager, every in-flight
  session is orphaned and the next list read reaps them all as `crashed` with
  one identical string, the named constant `DIED_WITHOUT_TERMINAL`
  (`sdk.py:50`, used at `sdk.py:557` and `reconcile.py:132`). Three in flight
  at restart time is ordinary, so without this guard a crash loop reliably
  manufactures a spurious session-fatal incident on top of itself. The alerter
  already receives `manager_restarted` (`alerting.py:134`), so the grace needs
  no new wiring.
- **No `completed` interleaved.** Below.

**Why the shared error signature is the primary gate, and short lifetime is
not.** The issue proposes gating on "died within seconds of dispatch". The
signature requirement achieves that goal better, and §3's data shows the
lifetime gate actively hurts:

| Gate | Opens at | Latency into the incident | Failures used |
|---|---|---|---|
| N=3 birth-death only (< 10s lifetime) | 23:17:19 | **42 min** | discards the first three |
| N=3 consecutive failures, shared signature | 23:03:14 | **28 min** | uses all |
| N=2 consecutive failures, shared signature | 22:59:19 | **24 min** | uses all |

Three independent long investigations do not fail with byte-identical error
strings. One dead credential does. The signature is both the sharper
discriminator and the thing the operator needs, since their next action depends
entirely on that string.

Lifetime is still **recorded and reported** (it distinguishes "died on its
first brain turn" from "died at the end"), just not used as an opening gate.

**Signature normalization.** ONE function in `probe.py`, used by both the
detector and the alert message builder so the two cannot drift: first line of
`entry.error`, lowercased, runs of digits collapsed to `#`, truncated to 120
chars. Digits must be collapsed or §3's cluster would not match itself
(`resets 12:40am` varies), and `timeout_error` embeds the timeout value
(`bobi/brain/turns.py`).

**An empty signature never matches anything.** `mark_terminal` writes `error`
only when truthy:

```python
# bobi/sdk.py:521-523
updates: dict = {"status": status, "pid": 0, "terminal_at": time.time()}
if error:
    updates["error"] = error
```

A terminal failure recorded with no message keeps `error == ""`. Under a naive
equality test all such entries share the empty signature and match *each
other*, so three unrelated causes would open an incident whose alert quotes
nothing. The rule: a normalized signature that is empty or whitespace-only is
**not a signature**. It cannot open a streak, cannot extend one, and yields
`unknown` rather than `failing`. Still latent rather than live: 0 of the 38
failed/crashed entries in this deployment's registry have an empty error
(re-measured 2026-08-21; was 0 of 33). P2 severity, on a verified codepath.

**N=3 is the proposed default.** The table above is the argument for making it
tunable, not the argument for 2: N=2 buys 4 minutes and doubles the
false-positive surface.

### 6.3 Which sessions count - mechanical inventory

Every writer of a terminal status at `ac2471e6`, from
`grep -rn "TERMINAL_FAILED\|TERMINAL_CRASHED\|mark_terminal" bobi/ --include=*.py`,
with every hit classified. The 2026-08-11 draft listed seven sites; four more
now exist.

| Site | Writes | Counts? | Why |
|---|---|---|---|
| `subagent.py:334` (`_persist_terminal`) | `failed` / `crashed` / `completed` | **yes** | The one-shot dispatch path. §3's outage is this path |
| `subagent.py:1492` | `failed` | **yes**, see R3 | `Workflow '<name>' not found`, closing a `starting` corpse (#850) |
| `subagent.py:1518` | `crashed` | **yes** | Wait-mode backstop: `wait-mode run died: <exc>` |
| `subagent.py:1569` | `crashed` | **yes** | Detached launch failed to spawn. A real dispatch death |
| `subagent.py:2023` | `failed` | **yes**, see R3 | Same `Workflow '<name>' not found` text, from the CLI entry |
| `reconcile.py:133` | `crashed` | **yes** | Dead-man reconciler closing a dead-pid run |
| `reconcile.py:205` | `failed` | **yes** | Reconciler closing a run past its declared deadline |
| `orchestrator.py:1414` | `failed` / `completed` | **yes** | Workflow run outcomes. §3's incident is entirely this path |
| `sdk.py:558` (`_reap_if_dead`) | `crashed` | **yes** | Dead-pid crash marking on a list read |
| `webapp/run_actions.py:181` | `"cancelled"` | **no** | Operator close. Excluded *for free*: `cancelled` is not in `FAILED_STATUSES` |

Non-writing hits from the same grep, all imports, constants, or read-side
classification, none of which can open an incident: `sdk.py:39-55`,
`subagent.py:27,193,259,452,492,537`, `reconcile.py:37-40,176,182`,
`orchestrator.py:28`, `launch_lineage.py:27`, `webapp/runtime.py:537-549`,
`webapp/runs.py:107-117`.

`status="error"` (55 entries in this deployment's registry) is written only by
`bobi/session.py`, the persistent-session path, and is not in `FAILED_STATUSES`
(`sdk.py:44`). Deliberately excluded: it is the director's own state, already
handled by `DEAD_STATES` (§4.1 F2), and counting it would double-report.

**R3 (new since the draft).** `subagent.py:1492` and `:2023` write the same
constant text for a given workflow name. Three dispatches naming a missing
workflow share a signature exactly and open an incident. That is not a brain
failure. It is still a true instance of the condition the alert actually claims
("dispatching but not producing", §7.3), so it is a correct fire rather than a
false positive, and the operator's next action is still the quoted string. No
guard needed. Named so Gate 1 sees it and test 5a pins it.

### 6.4 Read cost, re-measured 2026-08-21

Against this deployment's live registry: **2125 session directories, 510 with
`state.json`, 23 MB on disk (0.4 MB of it `state.json`), full parse 12.6 ms**.
The 2026-08-11 draft measured 1376 / 352 / 12.1 ms. Nothing prunes the tree, so
the directory count grew 54% in ten days and will keep growing.

The reader therefore bounds itself: `os.scandir` + `stat`, parse only entries
whose `state.json` `st_mtime` falls inside the lookback window.
`SessionRegistry.update` stamps `last_activity = time.time()` on every write
(`sdk.py:384`), so mtime tracks terminal writes reliably. This is an
established in-repo idiom: the same `stat().st_mtime` + cutoff shape is at
`bobi/workflow/state.py:236` and `bobi/monitors/scheduler.py:530,579`. Match
those.

Cost becomes O(recent) parses over an O(dirs) stat sweep. The stat sweep is the
part that grows without bound, so it takes a hard cap: examine at most the 500
most-recent entries by mtime and stop. A box dispatching more than 500 sessions
inside the lookback window is not a box this detector can help. Registry
pruning is the real fix and is **out of scope** (§10).

**One read per poll, not two.** An earlier draft had telemetry and the alerter
each call the detector, justified by the codebase's "re-derive rather than
trust a latch" precedent (`alerting.py:196-211`). That precedent is about not
trusting a latch *across* polls, not about deriving the same fact twice
*within* one poll. Two scans against a registry the manager is concurrently
writing can return different answers, so the heartbeat could publish
`session_health: ok` in the same poll the alerter opens an incident, and an
operator reconciling Slack against the dashboard would be looking at two
observations of the same instant.

The detector is therefore **single-flight**, memoized on the poll timestamp:
one scan, one observation, both consumers. That also halves the cost, to ~12 ms
per 30 s (0.04% duty).

---

## 7. The alerting edge

### 7.1 Incident model

Mirrors the crash-loop incident (`alerting.py:170-231`):

- **OPEN**: the condition in §6.2 first holds. One SOFT Slack notice carrying
  the shared error string verbatim, the failure count, and the affected session
  names.
- **DEDUP**: at most one notice per incident, marked alerted even on a log-only
  post, the same promise the existing soft alert makes (`alerting.py:176-181`).
- **CLOSE**: the first session that both **started after the incident opened**
  and reached `completed`. One RECOVERED notice with the incident duration, and
  a `sessions_recovered` lifecycle event.
- A failure with a *different* signature neither recovers nor re-opens.

**Why recovery tests `started_at`, not just `terminal_at`.** Sessions overlap.
A long investigation dispatched before the credential died can complete twenty
minutes into the incident, its brain turns having happened on the old, working
credential. Closing on it would post RECOVERED for a box that is still
brainless, standing the operator down mid-outage. Only a session born after the
incident opened proves the brain works now. §3 shows the overlap window is
real: sessions there ran 60-260s while dispatches arrived every 2-13 minutes.

`brain_availability.py` reaches the same conclusion by a different route: it
clears an incident only on a turn that actually succeeded (`:164-174`), never
on elapsed time.

**Limitation.** The detector observes only what is dispatched. On a box with no
dispatches it stays `unknown` and says nothing. That is correct (nothing is
failing) but it does not catch "brainless *and* completely idle". Fleet-side
silence detection is the tool for that gap, and the heartbeat already carries
the `expectations` seam for it (`snapshot.py:95`). Out of scope; stated so Gate
1 does not assume coverage this does not have.

### 7.2 Where the state lives

**A second state file, `supervisor-session-incident.json`, not the existing
one.**

The issue proposes reusing `supervisor-incident.json`. That collides:
`SlackAlerter._clear()` **unlinks the whole file** (`alerting.py:122-128`), so
closing a session-fatal incident would erase an open crash-loop incident,
including its `exhaust_cycles` machine-restart counter, the counter whose job
is surviving machine restarts. Restructuring the file into two keyed
sub-documents would work but needs a migration read for the existing flat shape
on every deployed volume.

A second file, same directory, same `atomic_write_json`, same fail-open
load/save, costs nothing and needs no migration.
`brain_availability.py:23` took the same route with its own `STATE_FILE`.

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

**It reports the observation, not a cause.** An earlier draft said "cannot
think", which over-claims: the same edge opens if three long runs are closed by
the reconciler's deadline path (`reconcile.py:205-208`), or if three dispatches
name a missing workflow (R3). Both are real "this agent is not producing work"
conditions worth alerting on, and neither is a dead credential. The error
string is quoted verbatim and the diagnosis is left to it.

**It says restart is not coming, and why.** The crash-loop alert promises
escalation (`alerting.py:269-272`). An operator reading this one must not wait
for an escalation that will never arrive.

---

## 8. Fail-open and budget neutrality

Point 3 of the issue asks for two guarantees. One is already true; the other is
one line of discipline.

**Budget neutrality is free.** `derive_manager_status` has exactly one non-test
caller, `telemetry.py:141`, and the restart state machine never consults it:
`Supervisor._cycle` reads the raw `/health` body (`supervision.py:687`).
Nothing added to the derived verdict, in either shape, can reach a restart
decision. Re-verified at `ac2471e6` after load grace added 181 lines to
`supervision.py`.

**Fail-open** follows the established pattern: every new path wrapped, every
exception logged and swallowed, an unreadable registry yielding
`state="unknown"` and never `"failing"`. `CompositeObserver` already isolates
observer failures from the supervisor (`supervision.py:194-209`), and
`Telemetry.poll` / `SlackAlerter.poll` already swallow (`telemetry.py:104-121`,
`alerting.py:142-145`). The one new rule: **`unknown` is never alertable**.
Absence of signal is not a signal, matching `is_wedged`'s stated discipline
(`supervision.py:77`).

---

## 9. Configuration

New `WATCHDOG_*` env knobs, matching the existing convention
(`bobi/supervisor/config.py:1-9`, `59-110`):

| Var | Default | Meaning |
|---|---|---|
| `WATCHDOG_SESSION_FAIL_STREAK` | `3` | N consecutive same-cause failures to open |
| `WATCHDOG_SESSION_FAIL_LOOKBACK` | `3600` | Seconds of registry history considered |
| `WATCHDOG_SESSION_FAIL_RESTART_GRACE` | `300` | Seconds after a manager restart during which the edge cannot open (§6.2) |
| `WATCHDOG_SESSION_FATAL_ENABLED` | `1` | Kill switch |

The kill switch exists because this reads a shared on-disk surface the manager
owns concurrently; an operator must be able to turn it off without a rollback.
Load grace shipped the same way (`WATCHDOG_LOAD_GRACE`, `config.py:81`).

---

## 10. Scope

**In** (10 files):

| File | Change |
|---|---|
| `bobi/supervisor/probe.py` | detector, signature normalizer, bounded single-flight reader |
| `bobi/supervisor/config.py` | the four knobs in §9 |
| `bobi/supervisor/snapshot.py` | `build_heartbeat` carries the `session_health` key |
| `bobi/supervisor/telemetry.py` | calls the detector, edge-triggers the two lifecycle events |
| `bobi/supervisor/alerting.py` | the incident edge and its second state file |
| `docs/ADMIN_PROTOCOL.md` | heartbeat schema + lifecycle table, same PR |
| `tests/test_supervisor_telemetry.py` | detector tests 1-10d |
| `tests/test_supervisor_alerting.py` | alerter tests 11-16 |
| `tests/test_supervision_restart.py` | integration test 17 |
| `tests/fixtures/supervisor_stub_manager.py` | new `brainless` mode |

`snapshot.py` is on this list because `build_heartbeat` (`snapshot.py:136-176`)
is the literal dict the payload is assembled from. Load grace touched the same
five sidecar files plus one new module, which is the closest available estimate
of this shape's real cost.

**Out, deliberately:**

- **Widening `classify_brain_unavailability`** to catch §3's string. It is
  narrow by design (G1); widening it re-opens the transient/terminal confusion
  #855 closed.
- **Routing `system/brain.*` to an operator** (G3). A deployment
  subscription change, not framework work, and it belongs with whoever owns the
  team's subscription list.
- **Registry pruning.** Nothing prunes `state/sessions/`; 2125 directories here
  and growing (§6.4). The detector caps its own sweep so it does not depend on
  a fix, but the unbounded directory is a runtime-wide problem. Separate issue.
- **The dashboard chip.** Rendering `session_health` in `healthChip` /
  `_is_running` is a separate UI change with its own design-system review. The
  heartbeat carries the truth after this PR; the console shows it in a
  follow-up. Called out because §5.1 uses the console's blindness as an
  argument, and R does not by itself fix it.
- **The `DEAD_STATES` crash-loop hazard** (§4.1 F2). Real, adjacent, different
  fix.
- **The `reconcile.py:176-188` repeat-emit defect** (§6.1). Named because it
  motivates the registry choice; fixing it is not this spec's work.
- **Fleet-side silence detection** (§7.1).
- **Any change to restart behaviour.** None. Explicitly.
- **`launch_admission`'s init-health ledger** (`bobi/launch_admission.py`). It
  looks adjacent and is not: `classify_init_failure` (`:388`) matches only an
  initialize control-request timeout signature, so it would not have recorded
  one of §3's failures; it is `"enabled": False` by default (`:48`); and its
  reflex is to *block* dispatch, which on a dead credential makes the agent
  quieter rather than louder, with no alert. Ruled out on the evidence.

---

## 11. Verification plan

Unit tests alongside the existing supervisor suites
(`tests/test_supervisor_telemetry.py`, 24 tests; `tests/test_supervisor_alerting.py`,
18; driven by injected doubles and a `Clock`). `tests/test_supervisor_load.py`
(25 tests, added by #1022) is the closest model for a new sidecar signal's test
shape.

**Detector** (`tests/test_supervisor_telemetry.py`):

1. N same-cause failures -> `failing`; the shared error string is carried through.
2. N failures with *different* signatures -> `ok`. The discriminator is the signature, not the count.
3. A `completed` interleaved in the streak -> `ok`.
4. Signature normalization: `resets 12:40am` and `resets 1:05am` match after digit collapse.
5. `cancelled` (operator close) never counts (§6.3).
5a. Three `Workflow '<name>' not found` failures DO open the edge, and the alert quotes that string (R3). Pins the intended behaviour so a later reader does not "fix" it.
6. An unreadable or absent registry -> `unknown`, never `failing`.
7. Only entries inside the lookback window are considered.
8. **The detector performs no writes.** Assert `state.json` mtimes are unchanged across a detection run. The direct regression test for §4.1 F1.
9. A `wedged` / `down` manager with N failed sessions -> no session-fatal edge (§6.2 manager-alive guard).
10. **N sessions reaped `crashed` right after a `manager_restarted` -> no edge.** The regression test for the restart-grace guard, and the highest-likelihood false positive in the design.
10a. **N failures with EMPTY error strings -> `unknown`, never `failing`** (§6.2 empty-signature rule).
10b. Signature normalizer as a unit: multi-line error takes the first line only; >120 chars truncates; digit runs collapse; an all-digits error does not become a universal match.
10c. A legacy entry with `terminal_at == 0.0` is skipped, not sorted to the beginning of time.
10d. **One scan per poll.** Assert the detector is invoked exactly once when both observers poll and both receive the identical observation (§6.4).

**Alerter** (`tests/test_supervisor_alerting.py`):

11. One SOFT post at onset; a second poll in the same condition posts nothing.
12. RECOVERED on a `completed` session that started after the incident opened, with duration.
13. **A `completed` session that started BEFORE the incident opened does NOT close it.** The regression test for the overlap hole in §7.1.
14. Incident survives a simulated process restart via the state file.
15. **Closing a session-fatal incident leaves an open crash-loop incident and its `exhaust_cycles` intact** (§7.2).
16. A raising `post_fn` does not propagate.

**Integration** (`tests/test_supervision_restart.py`, extending
`tests/fixtures/supervisor_stub_manager.py` with a `brainless` mode beside the
existing `always-idle` / `dead-then-recover` / `busy-wedge-then-recover`:
registers a healthy `idle` director, serves `/health`, writes N failed session
entries):

17. **A real `Supervisor` over a real stub manager in `brainless` mode performs ZERO restarts and the restart budget count stays 0**, while the alerter posts exactly one notice. Point 3 proven end to end rather than argued from §8.

Per `CLAUDE.md`, no real-Claude e2e leg: the change is brain-agnostic (it reads
persisted registry state and posts Slack), so the stub path is where the risk
lives.

Plus `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ -q`, and
§3's script re-run against this deployment's registry with the detector wired
in, to confirm it opens on real historical data.

---

## 12. Open questions for Gate 1

### Q1 (was blocking): S, R, or M?

**Recommend R.** Load grace already answered this fork the same way on the
adjacent failure mode (§5.4). Q1 is no longer blocking on the merits; it needs
a yes rather than a decision. Overriding to S or M is still Zach's call, and
either would change the shape of the code rather than a constant.

### Q2 (non-blocking)

- **N default.** 3 proposed. §6.2's table supports 2 as defensible (4 minutes
  earlier); 3 is the more conservative read of a shared on-disk surface.
- **Alert channel.** Reuses `WATCHDOG_ALERT_CHANNEL` (`alerting.py:70`). Same
  channel as crash-loops, or its own?
- **`unknown` in the heartbeat.** Publishing `state: "unknown"` for a box with
  no recent dispatches is honest but adds a third value a future consumer must
  handle. Alternative: omit the block when unknown. Recommend publishing: an
  absent key is indistinguishable from an old supervisor. `load_grace` sets the
  precedent here too, publishing `null` rather than omitting
  (`snapshot.py:168`, `ADMIN_PROTOCOL.md:323`).

### Q3 (new, non-blocking)

Should this alert also fire `system/brain.*`-style topics so a single
subscription covers both signals, or stay on the supervisor's Slack channel?
G3 means neither reaches an operator by subscription today, so the answer
determines whether one routing change covers both or two are needed.

---

## 13. Adjacent defects: where this sits

| Defect | State | Relation to this spec |
|---|---|---|
| [#855](https://github.com/moda-labs/bobi-agent/issues/855) brain auth failure recorded as a successful turn | **closed** ([#1041](https://github.com/moda-labs/bobi-agent/pull/1041), 2026-08-19) | **Overlaps, and covers the larger share.** Cause-shaped, fires at the point of failure, names the cause. This spec is the outcome-shaped backstop for its three gaps (G1 classifier miss, G2 `connect()` blind spot, G3 unrouted alert). Keep both; #855 stays primary |
| [#903](https://github.com/moda-labs/bobi-agent/issues/903) busy worker's `last_activity` goes stale, healthy run reads as stalled | **closed** ([#1022](https://github.com/moda-labs/bobi-agent/pull/1022)) | **Inverse, and the design precedent.** #903 is a false *negative* verdict on a working box; this is a false *positive* verdict on a broken one. Its fix chose the additive-block shape this spec recommends, which is why §5.4 treats Q1 as settled. No code overlap: load grace reads `/proc`, this reads the registry |
| [#1063](https://github.com/moda-labs/bobi-agent/issues/1063) otel tool probe basename fallback resolves the wrong agent | **closed** ([#1068](https://github.com/moda-labs/bobi-agent/pull/1068), today) | **Unrelated. Different "probe".** #1068 touched `bobi/tool_library/otel/tool.yaml`, `paths.agent_name`, and the dep-bootstrap preflight. `git show --stat 4309bb6d` contains no `bobi/supervisor/` file, and `bobi/supervisor/probe.py` is byte-identical to this spec's original baseline. Changes no premise here |
| `session.completed` fires repeatedly for one session | **no issue filed** | **Adjacent, and an argument for §6.1.** `reconcile.py:176-188` re-emits any terminal entry whose `emit_confirmed` is False on every wake, so a box with failing bus POSTs streams stale completions. A lifecycle-bus detector would read that as health; the registry-reading detector is immune. Out of scope (§10), named because it motivates the read-source choice |

None of these should be absorbed into #992, and #992 should not be folded into
any of them: three are closed, and the fourth needs its own fix in
`reconcile.py`. The one open question this raises is Q3.

---

## 14. Review record

### 14.1 The cross-model opinion is still owed

**This spec has had a same-model adversarial pass only.** Cross-model review is
unavailable in this worker container, re-verified by running both tools on
2026-08-21 rather than assumed:

```
$ codex exec -s read-only "Reply with the single word OK." < /dev/null
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic
       authentication in header, url: https://api.openai.com/v1/responses

$ aichat "Reply with OK."
(no response; terminated at the 60s bound - no config at
 /home/bobi/.config/aichat/config.yaml)
```

`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `AICHAT_PLATFORM`, and
`ANTHROPIC_API_KEY` are all unset in this container.

The 2026-08-11 draft attributed this gap to "#522". That citation was wrong:
[#522](https://github.com/moda-labs/bobi-agent/pull/522) is a merged PR
("[#479] fix: materialize codex api key auth"), not an open tracking issue. No
open issue tracks the worker-container second-opinion gap. **Do not read the
review below as an adversarial pass having succeeded**; a second model has not
seen this.

### 14.2 What the same-model pass changed

Findings from the premise pass (§4) and from three lenses run over the drafted
design. Each is folded into the body above.

| # | Lens | Finding | Folded into |
|---|---|---|---|
| 1 | Premises | The `/health` `sessions` block carries only ACTIVE sessions projected to three fields, so "the data already exists" is false | P3/P4, §6.1 |
| 2 | Premises | Keying on `TERMINAL_FAILED` alone misses every `connect()`-time auth failure, which lands as `TERMINAL_CRASHED` | P6, §6.3 |
| 3 | Wire contract | Neither existing consumer would show a `failing` status; both render it as a green "running" chip | §5.1, §5.2, Q1 |
| 4 | Correctness | Reading sessions via `list_active()` **writes** (`_reap_if_dead` -> `mark_terminal`), putting the sidecar in the business of mutating the manager's registry | §4.1 F1, §6.3, test 8 |
| 5 | Data | The issue's birth-death gate, tested against §3, opens 42 min in and discards the first three failures | §6.2 |
| 6 | Correctness | **Recovery on any `completed` is wrong.** An overlapping session dispatched pre-incident completes on the old credential and falsely stands the operator down mid-outage | §7.1, test 13 |
| 7 | Correctness | **A manager restart manufactures this incident.** Orphaned in-flight sessions are all reaped `crashed` with one identical string, so a crash loop fires a spurious session-fatal alert on top of itself | §6.2 restart grace, test 10 |
| 8 | Fidelity | The drafted condition dropped the issue's explicit "manager alive AND" conjunct | §6.2, test 9 |
| 9 | Honesty | The drafted alert asserted "cannot think"; the reconciler's deadline path produces the same edge from a different cause | §7.3 |

Findings 6, 7 and 8 are the ones that would have shipped a wrong feature: 6 and
7 are false signals in opposite directions, and 8 is a requirement the draft
lost.

One item was raised and **rejected** rather than adopted: reusing
`launch_admission`'s init-health ledger, recorded with its evidence in §10.

### 14.3 Eng review pass (`/gstack-plan-eng-review`), 2026-08-11

Run against the v2 spec. Five findings, all folded in.

**Step 0 scope challenge - TRIGGERED (10 files > 8).** Answered rather than
waived: this is one feature whose seams are spread across the sidecar (probe
derives, snapshot assembles, telemetry publishes, alerter notifies), not four
features. It is the same axis as Q1, so it folds into that decision rather than
becoming a second question. Load grace, a comparable single sidecar signal,
touched five of the same files plus a new module.

| # | Section | Finding | Conf | Folded into |
|---|---|---|---|---|
| E1 | Architecture | **Two registry scans per poll can disagree.** The heartbeat could publish `ok` in the same poll the alerter opened an incident | 8/10 | §6.4 single-flight; test 10d |
| E2 | Architecture | **`snapshot.py` was missing from the in-scope list.** `build_heartbeat` is the literal payload dict | 9/10 | §10 file table |
| E3 | Tests | **The empty signature matches itself.** `sdk.py:521-523` writes `error` only when truthy | 9/10, P2 | §6.2; tests 10a, 10b |
| E4 | Performance | **The stat sweep is unbounded.** O(dirs) every 30s against a directory nothing prunes | 7/10 | §6.4 hard cap at 500; §10 |
| E5 | Code quality | No ASCII diagrams, on a spec with a real state machine and a two-consumer data flow | 9/10 | §6 |

**What already exists (reused, not rebuilt).** The mtime+cutoff bounded-read
shape (`workflow/state.py:236`, `monitors/scheduler.py:530,579`); the incident
dedup/persist pattern (`alerting.py:108-128`); `atomic_write_json`;
`CompositeObserver`'s fail-open isolation; and, since 2026-08-19,
`brain_availability.py`'s incident/recovery model, which this mirrors rather
than re-invents. The one adjacent mechanism deliberately NOT reused is
`launch_admission`'s init-health ledger, with evidence, in §10.

**Failure modes for the new codepath.** A torn or truncated `state.json` under
a disk-full event: the reader skips unparseable entries exactly as `list_all`
does (`sdk.py:589-592`), so a torn file is invisible rather than fatal. It
cannot fabricate a `completed`, so it can shrink the observed set but never
falsely CLOSE an incident. Every new path has a test and an error path, and
none fails silently.

**Outside voice: NOT RUN.** `CODEX_MODE: not_authed` (401, §14.1). The skill's
fallback is a Claude subagent, which this deployment's operating instructions do
not permit dispatching unasked, and which would not be a cross-model opinion.

### 14.4 Re-validation pass, 2026-08-21

Triggered by review feedback on
[#1008](https://github.com/moda-labs/bobi-agent/pull/1008). Branch merged from
`main` (43 commits, no conflicts); every citation re-read at `ac2471e6`.

| # | Finding | Folded into |
|---|---|---|
| RV1 | #855 shipped and covers two causes on both dispatch drivers. Three gaps remain, each verified by running the code | §1 V1, §2 |
| RV2 | Load grace published an orthogonal additive heartbeat block rather than a new `manager.status` value, on the inverse failure mode | §1 V2, §5.4, Q1 |
| RV3 | The terminal-writer inventory gained four sites; two write identical constant text | §6.3, R3, test 5a |
| RV4 | Registry grew 1376 -> 2125 dirs in ten days; parse cost held at ~12 ms; the empty-signature trap is still latent (0 of 38) | §6.2, §6.4 |
| RV5 | F2's `DEAD_STATES` hazard is narrowed by load grace but not removed: a brainless box consumes no CPU, so grace will not defer it | §4.1 F2 |
| RV6 | #1063/#1068 is a different probe and changes no premise | §13 |
| RV7 | The draft's "#522" citation for the cross-model gap was wrong; #522 is a merged PR. No open issue tracks it | §14.1 |
| RV8 | `reconcile.py:176-188` re-emits terminal sessions whose bus POST never landed, on every wake. Strengthens the registry-over-bus choice | §6.1, §10, §13 |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/gstack-plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 5 issues, 0 critical gaps, all folded in |
| CEO Review | `/gstack-plan-ceo-review` | Scope & strategy | 0 | LENS ONLY | scope challenge answered inline (§14.3 Step 0); full skill not invoked |
| Design Review | `/gstack-plan-design-review` | UI/UX gaps | 0 | NOT RUN | no UI surface in scope; §10 defers the only rendering work |
| Codex Review | `codex exec` | Independent 2nd opinion | 0 | NOT RUN | codex 401 not authed, re-tested 2026-08-21 (§14.1) |
| DX Review | `/gstack-plan-devex-review` | Developer experience gaps | 0 | NOT RUN | not bound for this role |

Only the Eng leg ran as its skill. This role binds a three-leg spec review for
non-plan-born work, so **two legs are outstanding**: the CEO/scope leg got its
substance inline via the Step 0 challenge but not the full skill, and the design
leg was not run. The design gap is defensible on the merits, since the only
rendering work is explicitly deferred in §10, but it is a gap.

**CROSS-MODEL:** none available. Every review leg above is same-model,
disclosed in §14.1.

**VERDICT:** ENG cleared at the spec level; CEO covered by lens only; DESIGN not
applicable. Implementation is NOT authorized: this spec is stopped at Gate 1
pending the Q1 ruling, and the cross-model adversarial leg is still owed.

**UNRESOLVED DECISIONS:**

- **Q1**: shape S, R, or M. Recommend R, now with the `load_grace` precedent
  behind it. Zach's call. No code until it is answered.
- **Q2**: N default of 3 vs 2; alert channel shared with crash-loops or its
  own; publish `session_health: unknown` or omit the block.
- **Q3**: whether this alert should also fire a `system/brain.*`-style topic so
  one subscription covers both signals.
- Two review legs outstanding (§14.3): the CEO/scope skill and the cross-model
  adversarial pass. Neither blocks the Q1 ruling.
