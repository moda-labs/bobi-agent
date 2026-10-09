# 992 - a session-fatal edge for the supervisor probe

Status: **awaiting Gate 1** (design approval). No code written.
Issue: [#992](https://github.com/moda-labs/bobi-agent/issues/992)
Baseline: every file:line below re-read at `main` @ [`70db2e10`](https://github.com/moda-labs/bobi-agent/commit/70db2e10).
Measurements re-run 2026-10-09 against this deployment's live registry.

Three independent review rounds are committed verbatim beside this file and hold
the full finding lists, the measurement transcripts, and the revision history:
`plans/reviews/2026-10-09-992-review-1.md`, `-2.md`, `-3.md`.
This document states the design, the evidence in brief, and the tests.

**One caveat Gate 1 needs and the reports do not carry:** all three rounds were
same-model passes. `codex` returns 401 with no `~/.codex/auth.json` and `aichat`
has no config in this container, so no cross-model adversarial leg has run.

---

## 1. The gap #855 leaves

`bobi/brain_availability.py::observe_brain_turn` (`:103-195`) already opens a
deduped incident on a durable state file, alerts with the error text, and emits
`system/brain.recovered` with `outage_seconds` when a later turn succeeds.
It runs inside `drain_turn` (`bobi/brain/turns.py:98`), which both dispatch
drivers import (`bobi/subagent.py:30`, `bobi/workflow/orchestrator.py:30`).
That is cause-shaped detection, it is faster than anything here, and it should
stay the primary signal.

Three gaps remain, each verified by running the code rather than argued.

**G1. The classifier does not match this deployment's actual outage.**
`classify_brain_unavailability` (`bobi/brain/base.py:130-145`) matches fixed
substrings, and `_CREDIT_ERROR_TEXT` (`base.py:118-128`) carries
`"you've hit your usage limit"`.
The string that took this box down for 102 minutes says **session** limit:

```
'<no match>'            <- "You've hit your session limit · resets 12:40am (UTC)"
'credits_exhausted'     <- "You've hit your usage limit · resets 12:40am (UTC)"
'<no match>'            <- "agent process died without reporting a terminal status"
'authentication_failed' <- "Failed to authenticate: OAuth session expired"
'<no match>'            <- "turn failed (kind=unknown, api_status=none)"
```

This is deliberate, not a typo to patch.
The classifier's docstring reserves itself for messages saying the operator must
sign in or replenish an account, and leaves rate-limit and overload transient.
Widening it to catch a resetting session limit re-opens the transient/terminal
confusion #855 closed.

**G2. A `connect()`-time death is structurally invisible to it.**
`observe_brain_turn` fires only on a `TurnResult` (`brain/turns.py:96-102`), and
`connect()` is never a turn (`subagent.py:462`, `:480`).
A raise there propagates to `subagent.py:550-553`, which records `crashed` in
the registry and emits no brain-availability alert.
P6 shows `connect()` is one of the two paths a revoked credential takes.

**G3. The alert reaches the bus, not a person.**
`system/brain.auth.failed`, `system/brain.credits.exhausted`, and
`system/brain.recovered` are documented (`docs/EVENT_SERVER.md:368-370`) and
emitted, but `grep -rn "system/brain"` over `agents/`, `bobi/monitors/`, and the
installed `run/package/` returns no subscriber.
Routing them is a deployment change, not this spec's work, but it means the
"a human found it hours later" harm is not closed in practice.

**The residual this spec owns.** An outcome-shaped detector keys on what
happened to dispatched sessions, not on which error text a classifier
recognizes, so it covers G1 and G2 by construction and covers the whole class
the issue names (a bad model pin, a gateway misconfiguration, a quota shape
nobody has seen) without anyone adding a substring.
It is a backstop under #855, not a replacement.

Per the team's fewer-tickets preference this does not absorb another ticket.
#855, #934, and #903 are all closed; there is no open sibling to merge with.

---

## 2. Evidence

### 2.1 The outage this exists for

This deployment's own registry (`<run>/state/sessions/`) holds the failure class
the issue reports second-hand, from a different root cause:

```
window 07-30 22:35:15 .. 07-31 00:17:05 UTC  (101.8 min)
ALL sessions STARTED inside it: 13   Counter({'failed': 13})
  every one:  error = "You've hit your session limit · resets 12:40am (UTC)"
             13 of 13 BYTE-IDENTICAL
  completed sessions ending inside the window: 0
  inter-failure gap: max 25.5 min
  manager: idle and healthy throughout
```

Three consequences for the design.

- The failure class is broader than a revoked token.
  A subscription rate limit produces the identical shape: manager healthy, every
  dispatch dies, nobody told.
- The condition can be self-healing.
  The limit reset at 12:40am on its own, which is why a RECOVERED notice exists
  and why restarting is the wrong reflex.
- "Died within seconds" is the wrong gate.
  §5.2 shows it opens 42 minutes in and discards the first three failures.

### 2.2 The one false positive, and the one line that kills it

The registry also holds a mass-orphan cluster:

```
08-03 21:09:09.201  crashed  wf-issue-lifecycle-eng-team-858
08-03 21:09:09.989  crashed  wf-issue-lifecycle-eng-team-851
08-03 21:09:10.256  crashed  wf-pr-closed-eng-team-adhoc-fa0b931d
08-03 21:09:16.831  crashed  wf-issue-lifecycle-eng-team-884
08-03 21:09:19.289  crashed  wf-issue-lifecycle-eng-team-850
  every one:  error = "agent process died without reporting a terminal status"
  span: 10.1 seconds
```

That is what a manager restart or a host reboot produces, and on signature
alone it is indistinguishable from the condition above.

**It is excluded by name, not by a restart guard.** The string is the named
constant `DIED_WITHOUT_TERMINAL` (`bobi/sdk.py:50`), and it is written by
exactly two sites, both reaps:
`sdk.py:593` (`_reap_if_dead`) passes it unconditionally, and `reconcile.py:132`
falls back to it (`error = entry.error or DIED_WITHOUT_TERMINAL`).
An AST sweep for every `mark_terminal(error=...)` and `update(error=...)` call
in `bobi/` confirms no path writes an `error` onto a session before it reaches a
terminal status, so an orphan never has text for that fallback to prefer:
**every reap orphan carries exactly this one string.**
Its meaning is "this session died and nobody wrote down why", which is the
orphan shape, never the brain-fatal shape, because the dispatch path records
real text on any `connect()`-time raise (`subagent.py:552-555`,
`tool_crash_error`).

The two clusters are also 200x apart on spread, which is independent
corroboration that they are different phenomena:

| Signature | n | Smallest span of any 3 entries |
|---|---|---|
| `agent process died without reporting a terminal status` | 17 | **1.1 s** |
| `you've hit your session limit · resets 12:40am (UTC)` | 13 | **224.7 s** |
| `turn failed (kind=unknown, api_status=none)` | 24 | 86338 s |
| `turn failed` | 4 | 53611 s |

A minimum-spread rule would work too and is strictly the stronger filter, but it
would suppress a real burst dispatch (five PR-close runs all dying at
`connect()` within seconds), which is exactly this condition.
The name exclusion is safer on the true positive and needs no new constant.
Spread stays available as an additive second filter (Q2).

### 2.3 Simulated over the whole registry

The §5.2 condition run over every detector-visible terminal event
(**674** of the 862 parsed entries; the rest are non-terminal, `cancelled`
without a `terminal_at`, or `status="error"`):

```
N=3, 1h lookback, shared non-empty signature, no success interleaved:
  without the name exclusion   holds=14  opens=2   <- the 08-03 reap is open #2
  with the name exclusion      holds=11  opens=1   <- 07-30 23:04:15 only
  with the exclusion, N=2       holds=15  opens=2   <- adds an 08-20 timeout pair
```

**One open in 14 weeks of history, and it is the incident this spec exists for.**
The 11 holds collapse to 1 alert through the dedup latch, which is why the alert
count and the condition count are different numbers.
N=2 reintroduces a second open, which is the argument for keeping N at 3
independent of §5.2's latency table.

Frequency, for scale: entries whose error mentions a session limit, usage limit,
credits, quota, OAuth, authentication, 401, or 403 number 16 in total, 13 of them
inside the July window, the other three isolated singles that form no streak.
**One burst in 14 weeks, 102 minutes long, with nothing told.**
Rarity is the argument for the kill switch and against new machinery, not an
argument against alerting.

---

## 3. Premises, verified

| # | Claim (from the issue) | Verdict | Evidence |
|---|---|---|---|
| P1 | `probe.py::derive_manager_status` fuses three manager-only signals into the verdict | **TRUE** | `bobi/supervisor/probe.py:88-132` |
| P2 | An alive-but-brainless manager reads as `idle` | **TRUE** | `probe.py:118-119` returns the reported status verbatim when a process is alive and not wedged |
| P3 | `/health` already carries a `sessions` block | **TRUE, useless here** | `bobi/manager_health.py:87`, `:169-185`: `_session_status_from_registry` projects `list_active()` to `{name, role, status, inbox}` |
| P4 | "The data already exists" in that block | **FALSE** | It lists only ACTIVE sessions, and all four keys miss: no `error`, no `terminal_at`, no `started_at`. A terminally-failed session is absent by construction |
| P5 | Since #949 a session that dies on startup persists `TERMINAL_FAILED` | **TRUE, narrower than stated** | #949 fixed the unreachable `asyncio.TimeoutError` handler only (`subagent.py:452-460`, `:546-549`). The general error-result persist predates it (`subagent.py:521-545`, #694) |
| P6 | Consecutive birth-deaths are observable in the registry | **TRUE, richer than claimed** | An auth failure lands on one of two paths: a raise at `client.connect()` (`subagent.py:462`, `:480`) to `TERMINAL_CRASHED` (`:550-554`), or an error `TurnResult` to `TERMINAL_FAILED` (`:521-545`). **Key on `FAILED_STATUSES = ("failed", "crashed")`**, not `TERMINAL_FAILED` alone (`sdk.py:44`) |
| P7 | `SessionRegistry` carries what a detector needs | **TRUE** | `SessionEntry` has `status`, `error`, `started_at`, `terminal_at`, `role`, `name` (`sdk.py:279-335`); `mark_terminal` writes `status` + `terminal_at` + `error` durably before any bus POST (`sdk.py:544-567`) |
| P8 | `SlackAlerter` dedups incidents on an on-volume file | **TRUE** | `bobi/supervisor/alerting.py:53`, `:96`, `:108-128`; `STATE_FILE = "supervisor-incident.json"` under `paths.state_path` |
| P9 | This edge must not charge the restart budget | **TRUE, already structurally free** | `derive_manager_status` has exactly one non-test caller, `telemetry.py:141`. `Supervisor._cycle` (`supervision.py:687`) derives restarts from the RAW `/health` body (`:730-731`), never from the derived verdict. See §7 |
| P10 | A session that dies at `connect()` has already left a durable record | **TRUE** | Dispatch registers `state.json` with `status="running"` before the brain is touched: `orchestrator.py:321` vs its `connect()` calls at `:879`, `:1172`, `:1192`, `:1263`. `registry.update` no-ops on a missing file (`sdk.py:377-399`), so registration order is what makes the earliest-dying sessions detectable |

**One fact the issue does not mention: `list_active()` writes.**
It calls `_reap_if_dead` (`sdk.py:579-600`), which calls `mark_terminal` on any
active entry with a dead pid (`sdk.py:592-593`).
A sidecar calling it would mutate the manager's registry from outside and steal
the reconciler's crash-closing branch, which `list_all`'s docstring reserves
(`sdk.py:606-617`).
**The detector must use `list_all(reap_dead=False)` semantics, or read
`state.json` directly.**
Since #1099 `/health` itself calls `list_active()` (`manager_health.py:174`), so
serving the health body now reaps; that is the manager writing its own registry
and is fine, and it is exactly why the detector must not take that route from
outside the process.

---

## 4. Where the verdict lands

### 4.1 Shape S - the issue's sketch: a `failing` value in `manager.status`

`derive_manager_status` gains a `failing` return when the manager is alive and
recent sessions are all dying.

The status vocabulary is a documented closed enum (`docs/ADMIN_PROTOCOL.md:314`,
table at `:338-345`) and the doc pins consumer behaviour (`:347-349`):
"There is no `"healthy"` or `"dead"` status value - switch on `running`/`idle`
and on `down`."
A consumer that followed that instruction classifies `failing` as none of the
three, and both consumers that exist today fail quietly:

- `bobi/webapp/event_bus.py:117-126` - `_is_running` returns True for any status
  outside `(None, "stopped", "exited", "down")`, so a brainless box still
  reports **running**.
- `bobi/webapp/static/shell.js:121-132` - `healthChip` special-cases only
  `"wedged"`, then falls through to that same deny-list's output and renders a
  calm green **running** chip.

They are one deny-list surfaced twice, not two independent surfaces.
Under S the state is computed and published, and the fleet view still lies.

S also mutates three existing derivations rather than adding to them:
`healthy` flips to False (`telemetry.py:162`), `_BAD_STATES` (`telemetry.py:36`,
the sole gate for `probe_failing` at `:196-197`) must gain `failing` or the
episode never opens, and reusing `probe_failing` / `probe_recovered` makes those
events mean both "the manager probe is failing" and "the brain is failing" with
no field distinguishing which.
The compatibility promise (`ADMIN_PROTOCOL.md:27-31`) makes additive changes
free and requires consumers to ignore unknown *fields*; a new inhabitant of an
existing enum is not a new field.

### 4.2 Shape R - recommended: an orthogonal `session_health` block

Keep `manager.status` at its exact six values.
Publish the condition as a new top-level heartbeat key, **null when the
condition does not hold**, exactly the form `load_grace` already ships
(`snapshot.py:168`; `"load_grace": null` at `ADMIN_PROTOCOL.md:323`, described
at `:355` as "an additive block"):

```json
"session_health": null
```

```json
"session_health": {
  "error": "You've hit your session limit · resets 12:40am (UTC)",
  "since": "2026-07-30T22:39:35Z"
}
```

**There is no `state` enum.** A `"ok" | "failing" | "unknown"` triple was
carried and cut: `ok` needed incident state a pure function of the registry
cannot see, which left it with two conflicting definitions and two planned tests
demanding opposite behaviour on one input.
Null-or-block carries the same information with no vocabulary to keep
consistent, and a reader distinguishes "not failing" from "old supervisor" the
way it already does for `load_grace`.

Two fields, each with a named consumer.
`error` is the operator's entire next action and is quoted verbatim in the alert
(§6.3).
`since` is the `terminal_at` of the **oldest failure in the current streak**,
re-derived every poll, and it dates the condition on the dashboard.
It is never a duration source: the alerter persists its own `opened_at` and
every duration it reports comes from that.
On the July incident the two differ by 24.7 minutes, so conflating them would
understate the outage.

`failures` was cut: the condition requires the last N to be failures and N is a
module constant, so the field is always 3 while the block is present.
Session lifetime and `last_ok_at` were cut for the same reason - no consumer,
and `started_at` / `terminal_at` are in the registry for anyone diagnosing by
hand.

**No new lifecycle events.** Nothing in the repo consumes any episode event
(`grep -rn "probe_failing\|load_grace_active"` returns producers in
`telemetry.py` and test assertions only), so load grace having shipped an
unconsumed pair is not an argument for a second one.
Adding one later is two lines in `telemetry.py` plus a doc row, with no
migration and no consumer breakage.

### 4.3 Shape M - the floor

Alerter-only: no probe change, no heartbeat change, no wire change.
An observer reads the registry each poll and posts Slack.

**M is a 4-file saving, not a different design.** The detector, the signature
rule, the incident latch, the state file and the Slack message are identical
under all three shapes.
What M drops is `snapshot.py`, `supervision.py`, `docs/ADMIN_PROTOCOL.md` and
one test, because with a single consumer there is no two-reader desync to
prevent and so no `SupervisorState` field.

### 4.4 Recommendation: R, with the honest ledger

Load grace (#903/#1022) faced this exact fork on the inverse failure mode and
chose R: it publishes `load_grace` as a top-level additive block and did **not**
add a `deferred` value to `manager.status`, even though its whole subject is
which liveness verdict to report.
Same team, same payload, same trade, decided the opposite way from the issue's
sketch.

Two things the earlier draft claimed for R that do not survive checking, stated
here so Gate 1 rules on the real balance:

- **R's block is read by nothing today.** `grep -rn "load_grace"` returns
  producer hits only (`supervision.py`, `telemetry.py`, `config.py`,
  `snapshot.py`) plus tests and the protocol doc.
  The heartbeat's own consumer boundary is narrower than the draft implied:
  `event_bus.py::_card` (`:340-358`) projects exactly seven keys and drops
  everything else, and `shell.js::healthChip` reads two of them.
  Under R, `session_health` is a field no in-repo consumer reads, like the
  precedent it leans on.
- **R's one concrete gain today is the admin surface.**
  `bobi/supervisor/admin.py:280` returns `self.telemetry.last_snapshot` verbatim
  for the admin `status` command, and `telemetry.py:173` assigns that from
  `build_heartbeat`.
  So under R the condition shows up in an operator's `status` reply for one line
  in `snapshot.py` and no rendering code.
  Under M it does not, and it does not reach `fleet/heartbeat` for any
  out-of-repo consumer.

The draft also argued that choosing M "means building a second cause-agnostic
alerter beside a cause-specific one".
That is struck: it describes §6 in full, which S, R and M all build identically,
so it is an argument against #992 entire rather than a discriminator between the
shapes.

**R remains the recommendation**, on three grounds that do survive: a brainless
box has two true facts and only `manager.status=idle` **plus**
`session_health` present states both; S alerts while both fleet-view consumers
keep rendering green; and going M now and R later means a protocol conversation
that going R now does not.
It is Zach's call, and it changes the shape of the code rather than a constant.

---

## 5. The detector

Lives in `bobi/supervisor/probe.py` as a pure function plus a bounded reader,
beside `status_file_age`, which already reads the on-disk registry from outside
the manager.
The supervisor calls it once per cycle and carries the result on
`SupervisorState`, so both observers read one observation.

```
DATA FLOW - one read per poll, two consumers

  <run>/state/sessions/*/state.json    the manager writes these
       | bounded read: scandir + stat, parse only mtime >= now - lookback
       v
  probe.derive_session_health(...)     ONCE per cycle, by the supervisor
       v
  SupervisorState.session_health       a block, or None
       |
       +-------------------------+
       v                         v
  Telemetry.poll()          SlackAlerter.poll()
  -> build_heartbeat        -> open / close / abandon
  -> fleet/heartbeat        -> WATCHDOG_ALERT_CHANNEL

  Nothing here reaches Supervisor._cycle: the restart state machine reads the
  raw /health body only (section 7).
```

```
INCIDENT STATE MACHINE  (the alerter's, not a published vocabulary)

   +----------+   condition holds    +----------+
   |  closed  | -------------------> |   open   |
   +----------+   one SOFT notice    +----------+
        ^          opened_at persisted     |  |
        |                                  |  |
        |  a session SUCCEEDED and         |  |  condition has not held
        |  STARTED AFTER opened_at         |  |  for a full lookback
        |  -> one RECOVERED notice         |  |  -> file cleared, NO notice
        +----------------------------------+  |
        |                                     |
        +-------------------------------------+

   A different-signature failure moves nothing: it is not proof the brain
   works, and it is not the same incident.

   ABANDON is the EXPECTED close path at this deployment's dispatch rate,
   not an edge case: 82.0% of polls see an empty one-hour window. See 6.1.
```

### 5.1 Why the registry, not `/health` or the lifecycle bus

Extending `/health`'s `sessions` block puts the signal behind the very process
whose health is in question, and `_session_status_from_registry`
(`manager_health.py:169-185`) would have to start returning terminal sessions
plus three more fields, changing a payload other consumers read
(`snapshot.py:130`).
The sidecar already reads the registry directly for `status_file_age`, so the
registry path costs no new capability.

Reading the lifecycle bus is worse, and a live defect shows why:
`reconcile.py:176-188` re-emits any terminal entry whose `emit_confirmed` is
still False on **every** reconciler wake, so a box whose bus POSTs are failing
streams `agent/session.completed` for sessions that completed long ago.
A bus-reading detector would see that as proof of health.
The registry is the durable record `mark_terminal` writes before any POST (P7).

### 5.2 The condition

> **The manager reads `running` or `idle`**, AND the last **N** (3, a constant)
> dispatched sessions to reach a terminal outcome, within a lookback window
> (3600s, a constant), are **all** in `FAILED_STATUSES`, **share one signature**,
> and that signature is neither empty nor `DIED_WITHOUT_TERMINAL`, with no
> success interleaved.

Four guards beyond the streak, each closing a measured or verified case.

- **Manager alive.** The issue's own "manager alive AND" conjunct, and what
  makes the edge orthogonal rather than duplicative: a `down` or `wedged`
  manager is already alerted by the crash-loop path, and its orphaned sessions
  would fire this one too.
- **Not `DIED_WITHOUT_TERMINAL`.** §2.2. One comparison against a named
  constant, and it is the only thing standing between the detector and this
  deployment's single measured false positive.
  **The give-up, stated:** a cause that kills the agent process before it can
  write any error at all, an OOM or a SIGKILL, reads as healthy to this
  detector.
  That is a host condition the crash-loop and load-grace paths own, and the
  alternative was three interacting mechanisms that round 3 showed do not work
  (see below).
- **Not empty.** `mark_terminal` writes `error` only when truthy
  (`sdk.py:556-558`), so a terminal failure recorded with no message keeps
  `error == ""`.
  Under a naive equality test all such entries share the empty signature and
  match each other, so three unrelated causes would open an incident quoting
  nothing.
  Latent rather than live: 0 of the 65 failed/crashed entries here have an empty
  error.
- **No success interleaved.** A success inside the streak's span is proof the
  brain works, so it breaks the streak.

**Do not add a restart guard.** Three drafts carried one and all three failed
review, each for a different reason: a predicate on evaluation time left the
orphans in the lookback, `state.last_restart_at` is a scalar `_note_restart`
overwrites (`supervision.py:424`), and `RestartBudget._stamps` (`:114`) is not a
record of restart instants, because `_budget.reset()` empties it on an operator
bounce (`:473`, `:134`) and the non-fast-crash relaunch records no stamp
(`:678-682`).
Retaining `_stamps` for the lookback would also widen `max_restarts` from 30
minutes to an hour, since `count()` prunes and counts in one method
(`:119-126`), which §9 forbids.
The name exclusion closes the same false positive with one comparison, no new
`SupervisorState` field, and no coupling to the restart machine.
`plans/reviews/2026-10-09-992-review-2.md` and `-3.md` carry the full
derivations.

**Why the shared signature is the primary gate, and short lifetime is not.**
The issue proposes gating on "died within seconds of dispatch".
Against §2.1's data that gate actively hurts:

| Gate | Opens at | Latency into the incident | Failures used |
|---|---|---|---|
| N=3 birth-death only (< 10s lifetime) | 23:17:19 | **42.1 min** | discards the first three |
| N=3 shared signature | 23:04:15 | **29.0 min** | uses all |

Both figures are `terminal_at`, the column the detector actually sees.
Three independent long investigations do not fail with byte-identical error
strings; one dead credential does.
The signature is both the sharper discriminator and the thing the operator
needs, since their next action depends entirely on that string.

**Signature normalization.** ONE function in `probe.py`: the first line of
`entry.error`, stripped and lowercased.
That is all.
**Do not add digit collapse or a length truncation.** All 13 of §2.1's errors
are byte-identical, so the justification for collapsing digits
(`resets 12:40am` varies) is false, grouping the whole registry with and without
collapse gives identical results, and collapse is what made `"500"` and `"404"`
normalize to one key.
Truncation only ever made two long distinct errors match on a shared prefix.
Both are false-positive surfaces bought with no measured benefit.
**The give-up:** a provider that embeds a per-occurrence number in one cause
will not group, and the fix is one `re.sub` added back.
The first-line rule is earned by 1 multi-line error in 65; lowercasing is free.

The signature is a **grouping key only and is never shown to anyone**.
Both the published block (§4.2) and the alert (§6.3) carry the error verbatim,
taken from **the most recent entry in the streak**.

**N=3, a constant.** The table above is the argument against 2, not an argument
for a knob, and §2.3 shows N=2 reintroduces a second open.
Promoting a constant to an env knob later is additive and free.

### 5.3 Which sessions count

Only `FAILED_STATUSES` entries carrying a `terminal_at > 0` are candidates, and
only `completed` or `done` count as a success.
Everything else is excluded for free by that rule rather than by a guard:

- **`cancelled`** is an operator close (`webapp/run_actions.py:181`,
  `subagent.py:2675`) and is not in `FAILED_STATUSES`.
  The second site writes no `terminal_at`, which is where the 38 `cancelled`
  entries with `terminal_at == 0.0` come from, so entries without one must be
  **skipped rather than sorted to the epoch**.
- **`status="error"`** (135 entries) is not in `FAILED_STATUSES`.
  It is the **indeterminate** status, not an honest failure:
  `_run_verdict_agent_blocking`'s own docstring calls it "indeterminate, never
  all clear" and it is retried before it is written.
  Six `session.py` sites are the director's own persistent state; two
  (`subagent.py:2394`, `:2419`) are on *dispatched* verdict agents and are
  handled by nothing, which makes bringing them in a defensible future change
  and explicitly **out of scope** (§9): it would pull a 154-entry recurring
  cluster into the candidate set.
- **`done` is a success, explicitly.** It is a legacy alias still in
  `TERMINAL_STATUSES` (`sdk.py:46`) and still live (`mark_done` at `sdk.py:541`,
  called defensively at `orchestrator.py:169`).
  Read literally, a `done` entry would neither break a streak nor close an
  incident.
  Latent today (0 entries) and one word in the success tuple, but a silent hole
  if left unstated.

The ten terminal-status writers are `subagent.py:338`, `:1554`, `:1580`,
`:1631`, `:2102`, `reconcile.py:133`, `:205`, `orchestrator.py:1440`,
`sdk.py:593`, and `run_actions.py:181`.
**Re-derive that list with an AST walk, not a grep**, when the code is written:
`grep "mark_terminal"` misses every status written through `registry.update`,
and `grep "update([^)]*status="` is blind to multi-line calls such as
`session.py:990`.
Both greps were silently incomplete in three drafts running.

**FP1, a correct fire rather than a false positive.** `subagent.py:1554` and
`:2102` write the same constant text (`Workflow '<name>' not found`) for a given
workflow name, so three dispatches naming a missing workflow share a signature
exactly and open an incident.
That is not a brain failure, but it is a true instance of what the alert claims
("dispatching but not producing", §6.3) and the operator's next action is still
the quoted string.
No guard; named so a later reader does not "fix" it.

### 5.4 One bounded read per poll

The registry is unpruned and growing: **1025 session directories, 3728
top-level entries, 862 with `state.json`** on 2026-10-09, against 1376
`listdir` entries and 352 `state.json` at the 2026-08-11 draft.
The sweep is up 171% and the parse set up 145% in nine weeks, and nothing prunes
the tree.

So the reader bounds itself: `os.scandir` + `stat`, parse only entries whose
`state.json` `st_mtime` falls inside the lookback.
`SessionRegistry.update` stamps `last_activity` on every write (`sdk.py:398`),
so mtime tracks terminal writes reliably.
Measured three times on the live tree:

```
full parse        30.5 - 37.8 ms   (862 parses)
bounded (1h cut)   7.7 -  9.2 ms   (862 stats, 9 parses)
```

Absolute numbers move with host load, so read the ratio (3.6x to 4.1x) and the
growth curve, not the milliseconds.
Either way it is well under 0.1% of a 30 s duty cycle.
The mtime-plus-cutoff idiom is in-repo at `monitors/scheduler.py:530` and
`:579`, though both are pruning loops rather than bounded reads, so the idiom
being reused is the cutoff, not the reader.
**Do not add an entry cap.** Selecting the N most-recent entries by mtime
requires statting all of them first, so a cap bounds only parses, which the
lookback already bounds, and the peak terminal events in any one-hour window
here is 19 against a mean of 0.47 dispatches per hour.
Registry pruning is the real fix for the sweep and is out of scope (§9).

**One read per poll, not two.** Two scans against a registry the manager is
concurrently writing can return different answers, so the heartbeat could
publish a null `session_health` in the same poll the alerter opens an incident,
and an operator reconciling Slack against the dashboard would be looking at two
observations of one instant.
The mechanism is the one already in the codebase, not a memo cache:
`CompositeObserver.poll` (`supervision.py:194-209`) fans one `SupervisorState`
out to every observer in one pass and `load_grace` rides on that object
(`supervision.py:169`, published at `snapshot.py:168`).
`session_health` does the same.

---

## 6. The alerting edge

### 6.1 Incident model

Mirrors the crash-loop incident (`alerting.py:170-235`):

- **OPEN**: the condition in §5.2 first holds.
  One SOFT Slack notice carrying the shared error verbatim, the failure count,
  and the affected session names.
  `opened_at` and `signature` are persisted.
- **DEDUP**: at most one notice per incident, marked alerted even on a log-only
  post, the same promise the existing soft alert makes (`alerting.py:176-181`).
- **CLOSE**: the first session that both **started after `opened_at`** and
  reached a success (`completed`, or the `done` alias).
  One RECOVERED notice with the incident duration, measured from `opened_at`.
- **ABANDON**: the condition has not held for a full lookback window (3600s)
  while an incident is open.
  The incident file is cleared with **no** RECOVERED notice.
  The clock is `last_failing_at`, persisted beside `opened_at` and re-stamped on
  every poll where the condition holds.
  It is **initialized to `opened_at` at open time**, so it is never absent and
  the elapsed test never has a null to handle.
- A failure with a *different* signature neither recovers nor re-opens.

**Why recovery tests `started_at`, not just `terminal_at`.** Sessions overlap.
A long investigation dispatched before the credential died can complete twenty
minutes into the incident, its brain turns having happened on the old, working
credential.
Closing on it would post RECOVERED for a box that is still brainless, standing
the operator down mid-outage.
§2.1 shows the overlap window is real: sessions there ran 60-260s while
dispatches arrived every 2-13 minutes.
`brain_availability.py` reaches the same conclusion by a different route,
clearing an incident only on a turn that actually succeeded (`:164-189`), never
on elapsed time.

**Why ABANDON exists, and why it posts nothing.** Recovery requires a session
dispatched after the incident opened, so if dispatch stops, which is the
operator's normal response to this alert, the incident would never close and the
next real one would be deduped away.
No RECOVERED notice on abandon, because nothing proved the brain works.
The next streak opens a fresh incident and alerts again, which is correct for a
condition the operator has not visibly fixed.

**Gate 1 should know this is the expected close path here**, not an edge case.
Measured over 75.5 days at a 30s poll grid, **82.0%** of all polls see an
entirely empty one-hour window, so not-failing is the resting state of this box.
Walked against §2.1's own incident:

```
incident opens (opened_at)              07-30 23:04:15
last session-limit failure terminal_at  07-31 00:17:05
condition last holds                    07-31 00:17:05
abandon fires (+3600s)                  07-31 01:17:05
first success STARTED after the open    07-31 05:45:08  (+400.9 min)
```

So the operator gets one notice at 23:04 and then silence, for an outage that
self-healed at 00:40: RECOVERED is the rarer path at this dispatch rate.
That is the honest trade for not posting a false all-clear.

**Limitation.** The detector observes only what is dispatched.
On a box with no dispatches at all it says nothing, which is honest but does not
catch "brainless *and* completely idle".
Fleet-side silence detection is the tool for that gap and the heartbeat already
carries the `expectations` seam for it (`snapshot.py:95`).
Out of scope; stated so Gate 1 does not assume coverage this does not have.

### 6.2 Where the state lives

**A second state file, `supervisor-session-incident.json`, not the existing
one.**
The issue proposes reusing `supervisor-incident.json`.
That collides: `SlackAlerter._clear()` **unlinks the whole file**
(`alerting.py:122-128`), so closing a session-fatal incident would erase an open
crash-loop incident, including the `exhaust_cycles` counter whose job is
surviving machine restarts.
Restructuring the file into two keyed sub-documents would work but needs a
migration read for the existing flat shape on every deployed volume.

A second file, same directory, same `atomic_write_json`, same fail-open
load/save, costs one constant and needs no migration.
`brain_availability.py:23` took the same route with its own `STATE_FILE`.
It carries three fields: `opened_at`, `signature`, and `last_failing_at`.

[#1097](https://github.com/moda-labs/bobi-agent/pull/1097) corroborates the
separation: it had to move `_save()` ahead of the budget alert
(`alerting.py:192-196`) because persistence order inside that one file is
load-bearing across a process-exit boundary.
Sharing the file would put this spec's close path inside that contract.

### 6.3 Message shape

```
[bobi] moda/eng-team is dispatching but not producing: the last 3 sessions all reached
a terminal failure with the same error, and none completed. Manager is healthy
(status=idle).

  You've hit your session limit · resets 12:40am (UTC)

Not a crash loop, and NOT being restarted: if this is a credential or quota condition,
restarting runs into the same wall, burns the restart budget, and eventually parks the
machine. Act on the error above.
logs: fly logs -a moda-eng-team
```

**No duration at open.** A drafted "none completed in 29m" was a fourth
quantity nothing defined: it is not derivable from `opened_at` (0 at open time),
it is not the published `since` (24.7 min), and the figure a reader takes from
it, time since the last success, is 38.4 min on §2.1's own incident.
The quoted error is the operator's action; the elapsed figure was decoration
with three ways to get it wrong.
RECOVERED keeps its duration, measured from `opened_at`.

**It reports the observation, not a cause.** "Cannot think" over-claims: the
same edge opens if three long runs are closed by the reconciler's deadline path
(`reconcile.py:205-208`), or on FP1.
Both are real "this agent is not producing work" conditions worth alerting on,
and neither is a dead credential.

**It says restart is not coming, and why.** The crash-loop alert promises
escalation in so many words ("Escalates to a machine restart at budget
exhaustion", `alerting.py:275-276`).
An operator reading this one must not wait for an escalation that never arrives.

---

## 7. Fail-open and budget neutrality

Point 3 of the issue asks for two guarantees.
One is already true; the other is one line of discipline.

**Budget neutrality is free.** `derive_manager_status` has exactly one non-test
caller, `telemetry.py:141`, and the restart state machine never consults it:
`Supervisor._cycle` (`supervision.py:687`) reads the raw `/health` body
(`:730-731`).
Re-derived from `grep -rn derive_manager_status bobi/ tests/`: one definition,
one caller, six test call sites, nothing in `supervision.py`.
Nothing added to the derived verdict, in any of the three shapes, can reach a
restart decision.
The name exclusion (§5.2) also means the detector reads nothing from the restart
machine, so there is no coupling in the other direction either.

**Fail-open** follows the established pattern: every new path wrapped, every
exception logged and swallowed, an unreadable registry yielding a **null**
`session_health` and never a block.
`CompositeObserver` already isolates observer failures from the supervisor
(`supervision.py:194-209`), and `Telemetry.poll` / `SlackAlerter.poll` already
swallow (`telemetry.py:111-115`, `alerting.py:142-145`).
The one new rule: **absence of signal is never alertable**, matching
`is_wedged`'s stated discipline (`supervision.py:77`).

---

## 8. Configuration

**One** new `WATCHDOG_*` env knob, matching the existing convention
(`bobi/supervisor/config.py:1-9`, `:57-111`):

| Var | Default | Meaning |
|---|---|---|
| `WATCHDOG_SESSION_FATAL_ENABLED` | `1` | Kill switch |

Everything else is a module constant: N=3 and the lookback (3600s).
Neither has a requester, §5.2 shows the whole N argument is 13 minutes of
latency, and promoting a constant to an env knob later is additive and free.

The kill switch stays because this reads a shared on-disk surface the manager
owns concurrently, and an operator must be able to turn it off without a
rollback.
Load grace shipped the same way (`WATCHDOG_LOAD_GRACE`, `config.py:81`,
`:105`).

---

## 9. Scope

**In** (10 files):

| File | Change |
|---|---|
| `bobi/supervisor/probe.py` | detector, signature normalizer, bounded registry reader |
| `bobi/supervisor/config.py` | the one knob in §8 |
| `bobi/supervisor/supervision.py` | a `session_health` field on `SupervisorState` (`:144-174`), populated once per cycle in `_report` (`:781-795`) |
| `bobi/supervisor/snapshot.py` | `build_heartbeat` carries the `session_health` key, read off `SupervisorState` the way `load_grace` is at `:168` |
| `bobi/supervisor/alerting.py` | the incident edge, its second state file, the abandon clock |
| `docs/ADMIN_PROTOCOL.md` | heartbeat schema, same PR. No lifecycle-table row, since the episode pair was cut |
| `tests/test_supervisor_telemetry.py` | detector tests 1-10, including the published-key plumbing |
| `tests/test_supervisor_alerting.py` | alerter tests 11-15 |
| `tests/test_supervision_restart.py` | integration test 16 |
| `tests/fixtures/supervisor_stub_manager.py` | new `brainless` mode |

`tests/test_supervision.py` is **not** in scope: its only subject was the
restart-stamp retention, which the name exclusion deleted.
The `SupervisorState` field is asserted through the published heartbeat key in
`tests/test_supervisor_telemetry.py`, where `load_grace`'s own published-key
tests live.

No new module: the detector lives in `probe.py` beside `status_file_age`.
Under M (§4.3), `snapshot.py`, `supervision.py`, `ADMIN_PROTOCOL.md` and test 10
come off the list.

For a cost comparison, [#1022](https://github.com/moda-labs/bobi-agent/pull/1022)
(load grace, the closest comparable single sidecar signal) touched `config.py`
(+16), `load.py` (+314, new), `snapshot.py` (+11), `supervision.py` (+181),
`telemetry.py` (+47), `ADMIN_PROTOCOL.md` (+89), the stub fixture (+24), and four
test files.
This spec is a slightly smaller shape with no new module.

**Out, deliberately:**

- **Widening `classify_brain_unavailability`** to catch §2.1's string.
  It is narrow by design (G1); widening it re-opens the transient/terminal
  confusion #855 closed.
- **Routing `system/brain.*` to an operator** (G3).
  A deployment subscription change, not framework work.
- **Registry pruning.** 1025 session directories and 3728 `listdir` entries
  here, the sweep up 171% in nine weeks (§5.4).
  The lookback bounds what this detector parses, so it does not depend on a fix,
  but the unbounded directory is a runtime-wide problem.
  Separate issue.
- **The dashboard chip.** Rendering `session_health` in `healthChip` /
  `_is_running` is a separate UI change with its own design review.
  Called out because §4.1 uses the console's blindness as an argument, and R
  does not by itself fix it.
- **The `DEAD_STATES` crash-loop hazard.** An active director whose brain fails
  fatally sets `status="error"` (`session.py:701`), which `DEAD_STATES`
  (`supervision.py:68`, `:740-745`) restarts with no debounce, and load grace
  does not defer it because a brainless box consumes no CPU (`load.py:1-15`).
  Real, adjacent, different fix; §2.1's reproduction has an idle director, which
  never takes that path.
- **Bringing dispatched `status="error"` into the candidate set** (§5.3).
- **The `reconcile.py:176-188` repeat-emit defect** (§5.1).
  Named because it motivates the registry choice.
- **Fleet-side silence detection** (§6.1).
- **Any change to restart behaviour.** None. Explicitly.
- **`launch_admission`'s init-health ledger** (`bobi/launch_admission.py`).
  Adjacent-looking and ruled out on the evidence: `classify_init_failure`
  (`:388`) matches only an initialize-timeout signature so it would have
  recorded none of §2.1's failures, it is `"enabled": False` by default
  (`:48`), and its reflex is to *block* dispatch, which on a dead credential
  makes the agent quieter rather than louder.

---

## 10. Verification plan

Unit tests alongside the existing supervisor suites
(`tests/test_supervisor_telemetry.py`, 24 tests;
`tests/test_supervisor_alerting.py`, 19; driven by injected doubles and a
`Clock`).
`tests/test_supervisor_load.py` (25 tests, added by #1022) is the closest model
for a new sidecar signal's test shape.

**Detector** (`tests/test_supervisor_telemetry.py`):

1. N same-cause failures -> the block, carrying the error verbatim.
   Fixture: §2.1's 13 real entries, so the test asserts against the incident the
   spec exists for.
   Parametrized with an empty-error variant that must yield null.
2. N failures with *different* signatures -> null. The discriminator is the
   signature, not the count.
3. A success interleaved in the streak -> null. Parametrized over `completed`
   and the `done` alias (§5.3).
4. **Reap orphans never open the edge, at any poll time.** Fixture: §2.2's five
   real `DIED_WITHOUT_TERMINAL` entries. The regression test for the only false
   positive this deployment has produced, and the direct test of §5.2's name
   exclusion.
5. `cancelled` and `status="error"` never count, and a `cancelled` entry written
   with no `terminal_at` (`subagent.py:2675`) is skipped rather than sorted to
   the epoch (§5.3). Parametrized.
6. An unreadable or absent registry -> null, never a block.
7. A `wedged` / `down` manager with N failed sessions -> null (§5.2's
   manager-alive guard).
8. **The detector performs no writes.** Monkeypatch
   `SessionRegistry.list_active` and `SessionRegistry.mark_terminal` to raise,
   and assert a full detection run completes.
   The regression test for §3's `list_active()` finding.
   Assert on the raising API, not on mtimes: `_reap_if_dead` returns early for a
   non-active status and a zero pid (`sdk.py:588-591`), so over a fixture of
   terminal entries an mtime assertion passes even for a detector that wrongly
   calls `list_active()`.
9. Signature normalizer as a unit: a multi-line error takes the first line only;
   an empty or whitespace-only error is not a signature; two errors differing
   only in an embedded number do **not** match, which pins the digit-collapse
   cut so a later reader does not reintroduce it silently.
10. `build_heartbeat` publishes `session_health` as the block while the
    condition holds and as `null` otherwise, read off `SupervisorState`.
    The published-key and plumbing test, modelled on `load_grace`'s.

**Alerter** (`tests/test_supervisor_alerting.py`):

11. One SOFT post at onset; a second poll in the same condition posts nothing.
12. RECOVERED on a success that started after `opened_at`, with the duration
    measured from `opened_at`. Parametrized over `completed` and `done`.
13. **A `completed` session that started BEFORE the incident opened does NOT
    close it.** The regression test for the overlap hole in §6.1.
14. **The two incident files do not erase each other.** Closing a session-fatal
    incident leaves an open crash-loop incident and its `exhaust_cycles` intact,
    and a crash-loop `_clear()` does not erase an open session-fatal incident
    (§6.2). Also covers incident survival across a simulated process restart,
    since it must load and persist both files.
15. **ABANDON.** After a full lookback window in which the condition does not
    hold, the incident file is cleared and NO RECOVERED notice is posted; an
    interleaved holding poll re-stamps the clock rather than letting it expire;
    and `last_failing_at` survives a simulated process restart.

**Integration** (`tests/test_supervision_restart.py`, 5 tests, extending
`tests/fixtures/supervisor_stub_manager.py` with a `brainless` mode beside the
existing `wedge-then-recover` / `always-idle` / `dead-then-recover` /
`busy-wedge-then-recover` at `:43-44`: registers a healthy `idle` director,
serves `/health`, writes N failed session entries):

16. **A real `Supervisor` over a real stub manager in `brainless` mode performs
    ZERO restarts and the restart budget count stays 0**, while the alerter
    posts exactly one notice.
    Point 3 proven end to end rather than argued from §7.

Plus `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ -q`.
Per `CLAUDE.md`, no real-Claude e2e leg: the change is brain-agnostic (it reads
persisted registry state and posts Slack), so the stub path is where the risk
lives.

Four drafted test items are cut with the design they tested: restart-window
clock positions, all-digit signature cases, an `ok`-closes-the-incident
assertion, and a whole-registry acceptance check, which cannot be a test at all
because it reads an absolute path on one Fly volume that no CI runner has.
§2.3 is the right home for that measurement.

**Reporter-side validation, offered and accepted.** The issue closes with an
offer this plan should use: "Happy to test a pre-release against our
deployment - this exact failure mode is reproducible for us on demand (revoke a
token, watch the silence)."
That is the only validation available against a real outage rather than a
fixture, it costs nothing, and test 16's stub mode does not substitute for it.
Take the offer after Gate 1 and before close.

---

## 11. Open questions for Gate 1

### Q1 (the one blocking question): S, R, or M?

Does the session-fatal condition become a new `failing` value in
`manager.status` (**S**, the issue's sketch), a new orthogonal `session_health`
heartbeat block (**R**, recommended), or an alerter-only change with no wire
surface at all (**M**)?

**Recommend R**, on the three grounds in §4.4 that survive checking, and with
the two that did not stated there as well: R's block is read by no in-repo
consumer today, and M is a 4-file saving rather than a different design.
M is the honest floor and is cheaper than §4.3's earlier framing admitted; S is
the outlier, because load grace faced this fork on the inverse failure mode and
chose R.

**This is a yes/no, not a research question.**
Overriding to S or M is Zach's call and changes the shape of the code, not a
constant.
No code until it is answered.

### Q2 (non-blocking)

- **A minimum-spread rule as a second false-positive filter.** §2.2 shows the
  two clusters are 200x apart on spread, so requiring the N streak entries to
  span at least 60s is strictly stronger than the name exclusion and would make
  N=2 viable.
  Not taken, because it would suppress a genuine burst dispatch dying at
  `connect()`.
  Additive later if Gate 1 wants it.
- **The lifecycle episode pair.** `sessions_failing` / `sessions_recovered` was
  cut as unconsumed (§4.2).
  If Gate 1 wants the episode record on `fleet/lifecycle` for symmetry with
  `probe_failing` and `load_grace_active`, it is two lines in `telemetry.py`
  plus a doc row, and `telemetry.py` rejoins §9's list.
- **Alert channel.** Reuses `WATCHDOG_ALERT_CHANNEL` (`alerting.py:70`).
  Same channel as crash-loops, or its own?

### Q3 (non-blocking)

Should this alert also fire `system/brain.*`-style topics so a single
subscription covers both signals, or stay on the supervisor's Slack channel?
G3 means neither reaches an operator by subscription today, so the answer
determines whether one routing change covers both or two are needed.

---

## 12. Adjacent defects: where this sits

| Defect | State | Relation |
|---|---|---|
| [#855](https://github.com/moda-labs/bobi-agent/issues/855) brain auth failure recorded as a successful turn | **closed** ([#1041](https://github.com/moda-labs/bobi-agent/pull/1041)) | **Overlaps, and covers the larger share.** Cause-shaped, fires at the point of failure, names the cause. This spec is the outcome-shaped backstop for its three gaps (§1). Keep both; #855 stays primary |
| [#903](https://github.com/moda-labs/bobi-agent/issues/903) busy worker reads as stalled | **closed** ([#1022](https://github.com/moda-labs/bobi-agent/pull/1022)) | **Inverse, and the design precedent.** A false *negative* verdict on a working box against this spec's false *positive* verdict on a broken one. Its fix chose the additive block R recommends. No code overlap: load grace reads `/proc`, this reads the registry |
| [#1097](https://github.com/moda-labs/bobi-agent/pull/1097) incident not persisted before the budget alert | **merged** | **Corroborating.** It moved `_save()` ahead of a synchronous alert because persistence order inside `supervisor-incident.json` is load-bearing across the exit-70 boundary, which is an independent argument for §6.2's second file |
| `session.completed` fires repeatedly for one session | **no issue filed** | **An argument for §5.1.** `reconcile.py:176-188` re-emits any terminal entry whose `emit_confirmed` is False on every wake, so a box with failing bus POSTs streams stale completions. A lifecycle-bus detector would read that as health |

None of these should be absorbed into #992, and #992 should not be folded into
any of them: three are closed or merged, and the remaining one needs its own fix
in `reconcile.py`.
