# 992 - a session-fatal edge for the supervisor probe

Status: **awaiting Gate 1** (design approval). No code written.
Issue: [#992](https://github.com/moda-labs/bobi-agent/issues/992)
Author: engineer agent, 2026-08-11.
Re-validated 2026-08-21 against `main` @ [`ac2471e6`](https://github.com/moda-labs/bobi-agent/commit/ac2471e6).
**Re-validated 2026-10-09 against `main` @ [`83bebe49`](https://github.com/moda-labs/bobi-agent/commit/83bebe49).**

Every file:line in this document was re-read at `83bebe49`.
Citations that pointed at `ac2471e6` are corrected in place, not carried forward.

---

## 1. Re-validation verdict

**VALID WITH UPDATES.** Every premise and every design claim survives.
No code on `main` has implemented any part of this, and nothing has invalidated it.
What changed is citations, measurements, and the evidence behind two guards.

### 1.1 2026-10-09 pass (`ac2471e6` -> `83bebe49`, 7 weeks)

| # | What changed | Effect |
|---|---|---|
| W1 | **The whole design surface is byte-identical.** `git diff ac2471e6 83bebe49` is empty for `bobi/supervisor/probe.py`, `snapshot.py`, `telemetry.py`, `bobi/brain_availability.py`, `bobi/brain/`, `bobi/reconcile.py`, `bobi/webapp/event_bus.py`, `bobi/webapp/static/shell.js`, and `docs/ADMIN_PROTOCOL.md` | §5's fork-deciding claims, §2's three gaps, and P1/P2 need no re-argument |
| W2 | One supervisor change: [#1097](https://github.com/moda-labs/bobi-agent/pull/1097) moved `_save()` ahead of the budget alert in `alerting.py::_on_exhausted` | Shifts `alerting.py` cites after `:189` by +4. Changes no claim, and corroborates §7.2: persistence order inside the incident file is load-bearing |
| W3 | [#1099](https://github.com/moda-labs/bobi-agent/pull/1099) added an `inbox` key to `/health`'s sessions projection | **P3 reworded.** The block now carries four keys, still none of the three the detector needs. P4's conclusion is unchanged |
| W4 | `bobi/sdk.py`, `subagent.py`, `orchestrator.py`, `session.py` grew (#1081, #1091, #1095, #1099, #1101, #1105, #1111) | Line cites moved, semantics did not. §6.3's inventory is the same ten writers, re-derived |
| W5 | **The restart-reap false positive has a live instance.** 2026-08-03 21:09:09-19: **five** sessions reaped `crashed` with one byte-identical `DIED_WITHOUT_TERMINAL` inside **10.1 seconds** | §6.2's restart grace and test 10 move from review hypothesis to measured. Round 2 then found the grace as drafted does not close it. See §3.1 and §14.6 F1 |
| W6 | **A new shared-signature cluster exists and never reaches the condition.** 154 `turn failed (kind=unknown, api_status=none)` entries across 154 separate curator dispatches, 2026-08-02 to 2026-10-01 | The 1h lookback alone excludes it: at most **2** land inside any one-hour window. Recorded as FP2 in §6.2, with the credit corrected in round 2 |
| W7 | Registry grew to **1020 session directories** / **3708** top-level entries; `state.json` files 510 -> **857**; full parse 12.6 -> **20.0 ms** | §6.4 re-measured, and the earlier "3684 directories" relabelled: it was an `os.listdir` count, not a directory count. The bounded read is the growth-proofing; the 500-entry cap was cut in round 2 |
| W8 | **Simplicity pass: two config knobs and two spec components cut.** | Smaller surface, same behaviour. See §1.3, then §1.4 for the six more that round 2 cut |

### 1.2 2026-08-21 pass (`7025981` -> `ac2471e6`), unchanged and still true

| # | What changed | Effect |
|---|---|---|
| V1 | [#855](https://github.com/moda-labs/bobi-agent/issues/855) shipped ([#1041](https://github.com/moda-labs/bobi-agent/pull/1041), 2026-08-19): `bobi/brain_availability.py` alerts on brain auth failure and credit exhaustion, deduped, with recovery | Removes roughly half the harm. §2 rebounds the residual |
| V2 | [#903](https://github.com/moda-labs/bobi-agent/issues/903) shipped ([#1022](https://github.com/moda-labs/bobi-agent/pull/1022)): load grace published `load_grace` as an **orthogonal additive heartbeat block**, not a new `manager.status` value | Settles Q1 (the only blocking question) by precedent. See §5.4 |
| V3 | The terminal-writer inventory gained four sites | §6.3 re-derived; one new false-positive surface, FP1 |

### 1.3 What this pass cut

Zach's standing bar is the simplest practical solution with no cruft.
Four things were removed, none of which costs any capability.

| Cut | Was | Why |
|---|---|---|
| `WATCHDOG_SESSION_FAIL_LOOKBACK` | A tunable lookback window | Nobody has asked to tune it, and promoting a constant to a knob later is additive and free |
| `WATCHDOG_SESSION_FAIL_RESTART_GRACE` | A tunable restart grace | It is a correctness guard, not a preference. A knob invites setting it to 0 and getting the §3.1 false positive back |
| Session lifetime in the published block and the alert | "recorded and reported" beside the error | It is not a gate, no consumer reads it, and the registry still has it for anyone diagnosing by hand |
| Test 16 (a raising `post_fn` does not propagate) | A new alerter test | `_post` is shared with the crash-loop path and the existing suite already covers it |

**What Gate 1 is being asked to approve** is narrower than the 2026-08-11
draft: an outcome-shaped backstop plus the fleet-visibility half, not the whole
"nobody is told" problem. §2 bounds it.

### 1.4 2026-10-09 independent review: what it changed

A fresh-context reviewer with no authorship context re-ran every citation and
every empirical number against `main` @ `83bebe49` and the live registry, and
hunted over-design against the same bar.
Its full report is committed verbatim at
`plans/reviews/2026-10-09-992-review-1.md`; the per-finding triage is §14.6.

It found one guard that does not guard, one unspecified state transition, one
false premise under an exclusion, and three headline numbers that do not
reproduce.

| What changed | Where |
|---|---|
| **The restart grace did not close the false positive it exists for.** Drafted as a predicate on evaluation time, it suppressed for 300s while the orphaned entries sat in the 3600s lookback, so the incident opened at restart + 300s. Now an exclusion on the entries | §6.2, §3.1, test 10 |
| **A `failing` incident had no close path if dispatches stop**, which is the operator's normal response to the alert, and the detector would then read `unknown` while the alerter held an open incident indefinitely | §7.1 |
| **An all-digit error matched itself.** `"500"` and `"404"` both normalize to `#` and passed the empty-signature guard, so three unrelated numeric errors could open an incident quoting `#` | §6.2, test 10b |
| **`status="error"` is not written only by the director.** `subagent.py:2394` and `:2419` write it on *dispatched* sessions, so the exclusion's "already handled by `DEAD_STATES`" justification was false | §6.3 |
| **The headline "opens exactly three times" is two.** Independently re-simulated: 2 opens, with the condition holding at 14 moments the dedup latch collapses | §3.2, §11 |
| **The terminal-writer inventory was incomplete.** `mark_done` (`sdk.py:542`) and two `registry.update(status=...)` sites are invisible to the `mark_terminal` grep | §6.3 |
| **Six more components cut:** the 500-entry cap, `WATCHDOG_SESSION_FAIL_STREAK`, `last_ok_at`, the `sessions_failing` / `sessions_recovered` pair, test 10d, and the whole-registry acceptance check | §6.4, §9, §5.2, §11 |
| Roughly fifteen citation and measurement corrections | throughout |

**One judgement call from the previous pass is reversed.** That pass considered
cutting the `sessions_failing` / `sessions_recovered` lifecycle pair, kept it
for consistency with load grace's episode pair, and recorded the reasoning.
Round 2 attacked that reasoning and won: nothing in the repo consumes any
episode event (`grep -rn "probe_failing\|load_grace_active"` returns producers
in `telemetry.py` and test assertions only), so load grace having shipped an
unconsumed pair is not an argument that a second one is free. The pair is cut.
§14.6 records it.

---

## 2. What #855 already closes, and what it cannot

`bobi/brain_availability.py::observe_brain_turn` (`:103-195`) opens a deduped
incident on a durable state file, emits one alert carrying the error text, and
emits `system/brain.recovered` with `outage_seconds` when a later turn
succeeds. It runs on `bobi/brain/turns.py:98`, inside `drain_turn`, which
**both** dispatch drivers import (`bobi/subagent.py:30`,
`bobi/workflow/orchestrator.py:30`).
All four files are byte-identical to the 2026-08-21 baseline (W1).

That is Shape M (§5.3) already built, for two causes. Three gaps remain, each
verified here rather than argued.

**G1. The classifier does not match this deployment's actual outage.**
`classify_brain_unavailability` (`bobi/brain/base.py:130-145`) matches fixed
substrings. Re-run against §3's evidence at `83bebe49`, unchanged, plus the new cluster from §3.2:

```
$ PYTHONPATH=<worktree> python3 -c "from bobi.brain.base import classify_brain_unavailability as c; ..."
'<no match>'            <- "You've hit your session limit · resets 12:40am (UTC)"
'credits_exhausted'     <- "You've hit your usage limit · resets 12:40am (UTC)"
'<no match>'            <- "agent process died without reporting a terminal status"
'authentication_failed' <- "Failed to authenticate: OAuth session expired"
'<no match>'            <- "turn failed (kind=unknown, api_status=none)"
```

`_CREDIT_ERROR_TEXT` (`base.py:118-127`) carries `"you've hit your usage
limit"`. The string that took this box down for 102 minutes says **session**
limit. It does not match, and today's `main` would not alert on it.
The §3.2 cluster, now the largest shared-signature group in the registry, does not match either.

This is deliberate, not a typo to patch: the classifier's docstring reserves
itself for "messages that say the operator must sign in or replenish an
account" and leaves rate-limit and overload transient. Widening it to catch a
resetting session limit would re-open the exact transient/terminal confusion
#855 closed. The classifier is right; it just does not cover this.

**G2. A `connect()`-time death is structurally invisible to it.**
`observe_brain_turn` fires only on a `TurnResult` (`brain/turns.py:96-102`).
`connect()` is never a turn (`subagent.py:462`, `:480`, #1016), and a raise there
propagates to `subagent.py:550-553`, which records `crashed` in the registry
and emits no brain-availability alert. P6 shows `connect()` is one of the two
paths a revoked credential takes.

**G3. The alert reaches the bus, not a person.** `system/brain.auth.failed`,
`system/brain.credits.exhausted`, and `system/brain.recovered` are documented
(`docs/EVENT_SERVER.md:368-370`) and emitted. Nothing in this repo or in this
deployment's installed package subscribes to them (`grep -rn "system/brain"`
over `agents/`, `bobi/monitors/`, and the installed `run/package/` returns no
subscriber, re-run 2026-10-09). Routing
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
cause. Re-run 2026-10-09, unchanged:

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

Two claims in it were re-checked rather than carried forward, because the
detector's streak guard depends on them:

- **Zero `completed` sessions ended inside the window.** Re-measured against
  `terminal_at` (not `started_at`) over all 848 parsed entries: 0. The streak
  is one continuous run of 13, not segments.
- **Opening latency.** The detector sees `terminal_at`, so the N=3 open lands at
  23:04:15, not the 23:03:14 `started_at` of the third failure. §6.2's table
  mixes the two columns: its birth-death row is a `terminal_at` and the other
  two rows are `started_at`. The latency figures are right either way, and the
  table now labels which column each row uses.

### 3.1 The restart-reap false positive, now measured

A review pass predicted it (§14.2 finding 7) and §6.2 guards it. This
deployment's registry contains an instance:

```
08-03 21:09:09.201  crashed  wf-issue-lifecycle-eng-team-858
08-03 21:09:09.989  crashed  wf-issue-lifecycle-eng-team-851
08-03 21:09:10.256  crashed  wf-pr-closed-eng-team-adhoc-fa0b931d
08-03 21:09:16.831  crashed  wf-issue-lifecycle-eng-team-884
08-03 21:09:19.289  crashed  wf-issue-lifecycle-eng-team-850

  every one:  error = "agent process died without reporting a terminal status"
  span: 10.1 seconds
```

Five sessions, one byte-identical signature (`DIED_WITHOUT_TERMINAL`,
`sdk.py:50`), inside ten seconds. That is the mass-orphan shape a manager
restart or a host reboot produces, and it is indistinguishable from this spec's
condition on signature alone.

It is not the only one. The same signature appears 17 times in the registry,
including three further 2-session clusters (08-04 22:46, 08-11 20:55,
08-20 17:18) that would reach the condition at N=2. That is a second argument
for keeping N at 3, independent of §6.2's latency table.

Without a restart guard the detector opens an incident here and the operator
gets a session-fatal alert for a restart that the crash-loop path is already
alerting. The guard is not defensive decoration; it is the difference between
one alert and two contradictory ones.

**Round 2 found that the guard as drafted does not deliver it** (§14.6 F1).
Stated as "no manager restart within the last 300s" it suppresses for 300s
while the reaped entries sit in the 3600s lookback, so the spurious incident
opens at restart + 300s rather than never. Re-simulated against this exact
cluster, the open lands at 21:14:09. §6.2 now states the guard as an exclusion
on the entries, which opens nothing here.

### 3.2 Simulated against the full registry: two opens, both explained

The §6.2 condition was run over every terminal entry in this deployment's
registry. The denominator, stated precisely because the rate depends on it:
**804** entries carry a `terminal_at > 0`, of which **669** are the
`completed` / `failed` / `crashed` events the detector actually sees
(`status="error"` entries carry a `terminal_at` too and are excluded by §6.3).

| Opened | Signature | Verdict |
|---|---|---|
| 07-30 23:04:15 | `you've hit your session limit · resets #:#am (utc)` | **True positive.** The incident this spec exists for |
| 08-03 21:09:10 | `agent process died without reporting a terminal status` | **False positive**, guarded by §6.2's corrected restart exclusion (§3.1) |

**Two opens in 14 weeks of history**, both accounted for, and the only unguarded
one is the condition the spec is for.

The dedup latch is doing visible work here, so the other half of the number
matters: the condition *holds* at **14 distinct moments** (11 across the July
window, 3 on 08-03) and the latch collapses them to 2 incidents. That is the
measurement that justifies §7.1's dedup, and it is why the alert count and the
condition count are different numbers.

An earlier pass reported **three** opens, at 07-30 22:39, 07-30 23:48 and
08-03 21:09. Round 2 re-implemented the condition independently and got two
(§14.6 F3). The 22:39 row is the first failure's `terminal_at`, which is an N=1
moment, and the 23:48 row never materializes: the inter-failure gap in that
window peaks at 25.5 minutes, so the 1h lookback never empties and never
"resets". §3's own correction ("the N=3 open lands at 23:04:15") already
contradicted the old table. The corrected numbers are what §11 and §14.6 use.

The largest shared-signature cluster in the registry does **not** appear above:
`turn failed (kind=unknown, api_status=none)` recurs **154** times between
2026-08-02 and 2026-10-01, 129 as `status="error"`, 24 as `failed`, 1 as
`completed`. These are 154 *separate* registry entries, one per curator
dispatch of a single monitor, so each is its own streak candidate rather than
one long-lived session.

What keeps them out is the **lookback**, not the `status="error"` exclusion: even
counting all 154, at most **2** of them land inside any one-hour window, and the
24 `failed` ones are a minimum of 5.99 hours apart. The earlier pass credited
the status exclusion for this; that credit was wrong and is corrected here
(§14.6 F17). Recorded as FP2 in §6.2, because the cluster shows a single
recurring monitor can mint a durable shared signature.

### 3.3 No new burst since July

Re-checked 2026-10-09 across the whole registry: entries whose error mentions a
session limit, usage limit, credits, quota, OAuth, authentication, 401, or 403
number 16 in total, 13 of them inside the July window. The other three are
isolated singles (2026-08-23, 2026-08-24, 2026-09-21), none of which forms a
streak.

The premise is therefore unchanged and the frequency is now bounded: **one
burst in 14 weeks, 102 minutes long, with nothing told.** That is the harm this
spec addresses, and it is rare rather than routine. Rarity is the argument for
the kill switch and against new machinery, not an argument against alerting.

---

## 4. Premises, re-verified at `83bebe49`

| # | Claim (from the issue) | Verdict | Evidence |
|---|---|---|---|
| P1 | `probe.py::derive_manager_status` fuses three manager-only signals into the verdict | **TRUE** | `bobi/supervisor/probe.py:88-132`. `git diff ac2471e6 83bebe49 -- bobi/supervisor/probe.py` is EMPTY: the file is still byte-identical to the 2026-08-11 baseline |
| P2 | An alive-but-brainless manager reads as `idle` | **TRUE** | `probe.py:118-119` returns the reported status verbatim when a process is alive and not wedged |
| P3 | `manager_health.py`'s `/health` body already carries a `sessions` block | **TRUE, useless here** | `bobi/manager_health.py:87`, `169-185`: `_session_status_from_registry` returns `list_active()` projected to `{name, role, status, inbox}`. [#1099](https://github.com/moda-labs/bobi-agent/pull/1099) added the fourth key; the heartbeat's own copy still projects three (`snapshot.py:126-132`) |
| P4 | "The data already exists" in that block | **FALSE** | It lists only ACTIVE sessions, and all four keys miss: no `error`, no `terminal_at`, no `started_at`. A terminally-failed session is absent by construction. See §6.1 |
| P5 | Since #949 a session that dies on startup persists `TERMINAL_FAILED` | **TRUE, narrower than stated** | #949's D067 fixed the unreachable `asyncio.TimeoutError` handler only (`bobi/subagent.py:452-460`, `546-549`). The general error-result persist predates it (`subagent.py:521-545`, `c2f9956`, #694) |
| P6 | Consecutive birth-deaths are observable in the registry | **TRUE, richer than claimed** | An auth failure lands on one of two paths: a raise at `client.connect()` (`subagent.py:462`, `:480`) -> `TERMINAL_CRASHED` (`subagent.py:550-554`), or an error `TurnResult` -> `TERMINAL_FAILED` (`subagent.py:521-545`). **Key on `FAILED_STATUSES = ("failed", "crashed")`, not `TERMINAL_FAILED` alone** (`bobi/sdk.py:44`) |
| P7 | `SessionRegistry` carries what a detector needs | **TRUE** | `SessionEntry` has `status`, `error`, `started_at`, `terminal_at`, `role`, `name` (`sdk.py:279-335`); `mark_terminal` writes `status` + `terminal_at` + `error` durably before any bus POST (`sdk.py:544-567`) |
| P8 | `SlackAlerter` dedups incidents on an on-volume file | **TRUE** | `bobi/supervisor/alerting.py:53`, `96`, `108-128`; `STATE_FILE = "supervisor-incident.json"` under `paths.state_path` |
| P9 | This edge must not charge the restart budget | **TRUE, already structurally free** | `derive_manager_status` still has exactly one non-test caller, `telemetry.py:141` (re-derived from `grep -rn derive_manager_status bobi/ tests/`: 1 definition, 1 caller, 6 test call sites). `Supervisor._cycle` (`supervision.py:687`) derives restarts from the RAW `/health` body (`supervision.py:730-731`), never from the derived verdict. No new machinery satisfies point 3, in either shape. See §8 |
| P10 | A session that dies at `connect()` has already left a durable record | **TRUE** | Dispatch registers `state.json` with `status="running"` *before* the brain is touched: `orchestrator.py:321` vs its `client.connect()` calls at `:879`, `:1172`, `:1192` and `:1263`, all later in the file and all later at runtime. `registry.update` no-ops on a missing file (`sdk.py:377-399`), so registration order is what makes the earliest-dying sessions detectable |

### 4.1 Two facts the issue does not mention

**F1. `list_active()` writes.** It calls `_reap_if_dead` (`sdk.py:579-600`),
which calls `mark_terminal` on any active entry with a dead pid
(`sdk.py:592-593`). A sidecar calling it would mutate the manager's registry
from outside and steal the reconciler's crash-closing branch, which `list_all`'s
docstring explicitly reserves (`sdk.py:606-617`). **The detector must use
`list_all(reap_dead=False)` semantics, or read `state.json` directly.**

W3 sharpens this: since #1099, `/health` itself calls `list_active()`
(`manager_health.py:174`), so serving the health body now reaps. That is the
manager writing its own registry and is fine. It is also the reason the
detector must not take the same route from outside the process.

**F2. An active director already crash-loops on this class, and does charge
the budget.** If the director takes a turn and its brain fails fatally,
`session.py:701` sets `status="error"`, which the supervisor's
`DEAD_STATES` path (`DEAD_STATES = frozenset({"error"})`, `supervision.py:68`)
restarts with no `confirm_polls` debounce (`supervision.py:740-745`).

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

- `bobi/webapp/event_bus.py:117-126` - `_is_running` returns True for any
  status outside `(None, "stopped", "exited", "down")`, so a brainless box
  still reports **running**.
- `bobi/webapp/static/shell.js:121-132` - `healthChip` special-cases only
  `"wedged"`, then falls through to `a.running`, which is that same deny-list's
  output. The dashboard renders a calm green **running** chip.

Both files are byte-identical to the 2026-08-21 baseline (W1), so the
fork-deciding claim needs no re-argument. They are also not independent: it is
one deny-list surfaced twice.

Under S the state is computed and published, and the fleet view still lies.

S also mutates three existing derivations rather than adding to them:
`healthy = derived in _HEALTHY_STATES` flips to False (`telemetry.py:162`), and
`_BAD_STATES` (`telemetry.py:36`, the sole gate for `probe_failing` at
`telemetry.py:196-197`) must gain `failing` or the episode never opens. Reusing `probe_failing` / `probe_recovered` makes those two events
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
  "since": "2026-07-30T23:04:15Z"
}
```

Three fields beyond `state`, each with a named consumer: `failures` and `error`
are quoted verbatim in the alert (§7.3), and `since` is what the RECOVERED
notice computes its duration from (§7.1).
`last_ok_at` was cut in round 2: nothing reads it, the recovery rule compares
against the incident's own open time rather than against it, and it costs the
detector an extra pass over the lookback to find the most recent success.
`terminal_at` in the registry already answers "when did a dispatch last work",
which is the same argument §1.3 used to cut session lifetime.

A new field on an existing payload: no `SUPERVISOR_VERSION` bump, no consumer
breakage.

It is also the more truthful model. A brainless-but-quiet box has **two** true
facts, and S can report only one, by overwriting the other:

```
manager.status  = "idle"        <- true: the manager loop is fine
session_health  = "failing"     <- true: nothing dispatched can think
```

That pair is the diagnosis. Either alone is not.

**No new lifecycle events.** An earlier draft added a `sessions_failing` /
`sessions_recovered` pair mirroring `probe_failing` / `probe_recovered`, and the
previous pass kept it for consistency with load grace's `load_grace_active` /
`load_grace_cleared`. Round 2 cut it (§14.6 S1): nothing in the repo consumes
any episode event, so a shipped unconsumed pair is not an argument for a second
one, and adding a lifecycle edge later is two lines in `telemetry.py` plus a doc
row with no migration and no consumer breakage. The heartbeat block carries the
state, the Slack notice carries the episode, and the operator has both.
If Gate 1 wants the episode record anyway, it is the cheapest thing in this spec
to add back.

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
the manager (`probe.py:59-77`). The supervisor calls it once per cycle and
carries the result on `SupervisorState`, so both observers read one observation
(§6.4).

```
DATA FLOW - one read per poll, two consumers

  <run>/state/sessions/*/state.json          the manager writes these
            |                                 (mark_terminal: status, terminal_at, error)
            | bounded read: scandir + stat, parse only mtime >= now - lookback
            v
  probe.derive_session_health(...)  <-- computed ONCE per cycle and carried on
            |                            SupervisorState, the way load_grace is,
            |                            so both consumers see ONE observation
            +---------------------------+
            |                           |
            v                           v
   Telemetry.poll()            SlackAlerter.poll()
   heartbeat.session_health    incident open/close/abandon -> Slack
            |                           |
            v                           v
      fleet/heartbeat            WATCHDOG_ALERT_CHANNEL

  No new lifecycle events: the episode pair was cut (section 5.2).

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
        AND no post-restart entry |  (restart-window entries are DISCARDED
                                  |   before the streak is evaluated)
                                  v
   +--------+  streak broken  +-----------+
   |   ok   | <-------------- |  failing  |
   +--------+   by a session  +-----------+
        ^       SUCCEEDED and      |
        |       STARTED AFTER      | one SOFT Slack notice at entry
        |       the incident       | incident persisted to
        |                          | supervisor-session-incident.json
        +--------------------------+
              one RECOVERED notice

   A different-signature failure moves nothing: it is not proof the brain
   works, and it is not the same incident.

   An incident held open with NO dispatches for a whole lookback window is
   ABANDONED, not recovered: state returns to unknown, the incident file is
   cleared, no RECOVERED notice. See section 7.1.
```

### 6.1 Why the registry, not `/health` or the lifecycle bus

Extending `/health`'s `sessions` block puts the signal **behind the very
process whose health is in question**, and
`_session_status_from_registry` (`manager_health.py:169-185`) would need to
start returning terminal sessions plus three more fields, changing a payload
other consumers read (`snapshot.py:130`). The sidecar already reads the
registry directly for
`status_file_age`, so the registry path costs no new capability.

Reading the lifecycle bus instead is worse again, and there is a live defect
that shows why: `reconcile.py:176-188` re-emits any terminal entry whose
`emit_confirmed` is still False on **every** reconciler wake, so a box whose
bus POSTs are failing produces a repeating stream of `agent/session.completed`
for sessions that completed long ago. A bus-reading detector would see that as
proof of health. The registry is the durable record `mark_terminal` writes
before any POST (P7), so it is unaffected.

### 6.2 The opening condition

> **The manager reads `running` or `idle`**, AND the last **N** (3, a constant)
> dispatched sessions to reach a terminal outcome, within a lookback window
> (3600s, a constant), **excluding any entry whose `terminal_at` falls inside
> 300s after the last manager restart**, are **all** in `FAILED_STATUSES` and
> **share one normalized error signature**, with no success interleaved.

The three guards beyond the streak each close a false positive found in review:

- **Manager alive.** The issue's own "manager alive AND" conjunct, and what
  makes the edge orthogonal rather than duplicative: a `down` or `wedged`
  manager is already alerted by the crash-loop path, and its orphaned sessions
  would fire this one too.
- **Restart exclusion.** When the supervisor restarts the manager, every
  in-flight session is orphaned and the next list read reaps them all as
  `crashed` with one identical string, the named constant
  `DIED_WITHOUT_TERMINAL` (`sdk.py:50`, used at `sdk.py:592` and
  `reconcile.py:132`). Three in flight at restart time is ordinary, so without
  this guard a crash loop reliably manufactures a spurious session-fatal
  incident on top of itself. **§3.1 is a measured instance of exactly this.**

  **It must be a predicate on the entries, not on the evaluation time.** The
  draft said "no manager restart occurred within the last 300s", which round 2
  showed buys 300s of silence inside a 3600s exposure window: the reaped
  entries stay in the lookback, so the spurious incident opens at
  restart + 300s instead of never. Re-simulated against §3.1's own cluster the
  bad open lands at 21:14:09 (§14.6 F1). The rule is therefore: **discard any
  terminal entry whose `terminal_at` falls in
  `[last_restart_at, last_restart_at + 300s]`** for as long as that entry is
  inside the lookback. A discarded entry neither opens nor extends a streak,
  and it does not break one either, since a reap is not evidence the brain
  works.

  The input is `state.last_restart_at` (`supervision.py:157`), already on the
  `SupervisorState` handed to every observer and already published in the
  heartbeat (`snapshot.py:159`). Reading it from there rather than from the
  alerter's `manager_restarted` handler (`alerting.py:134`) is what keeps both
  consumers on one observation, per §6.4.
- **No success interleaved.** Below.

**Why the shared error signature is the primary gate, and short lifetime is
not.** The issue proposes gating on "died within seconds of dispatch". The
signature requirement achieves that goal better, and §3's data shows the
lifetime gate actively hurts:

| Gate | Opens at | Column | Latency into the incident | Failures used |
|---|---|---|---|---|
| N=3 birth-death only (< 10s lifetime) | 23:17:19 | `terminal_at` | **42 min** | discards the first three |
| N=3 consecutive failures, shared signature | 23:03:14 | `started_at` | **28 min** | uses all |
| N=2 consecutive failures, shared signature | 22:59:19 | `started_at` | **24 min** | uses all |

The first row is a `terminal_at` and the other two are `started_at`, because a
birth-death gate can only be evaluated once the lifetime is known. Stated in
one column throughout, the N=3 shared-signature open is 23:04:15 (29.0 min) and
the birth-death open is 23:17:19 (42.1 min): the 13-minute penalty is the same
either way.

Three independent long investigations do not fail with byte-identical error
strings. One dead credential does. The signature is both the sharper
discriminator and the thing the operator needs, since their next action depends
entirely on that string.

Lifetime is **not** recorded in the published block or the alert (§1.3 cut).
It is not a gate, no consumer reads it, and `started_at` / `terminal_at` are
already in the registry for anyone diagnosing by hand.

**Signature normalization.** ONE function in `probe.py`, used by both the
detector and the alert message builder so the two cannot drift: first line of
`entry.error`, lowercased, runs of digits collapsed to `#`, truncated to 120
chars. Digits must be collapsed or §3's cluster would not match itself
(`resets 12:40am` varies), and `timeout_error` embeds the timeout value
(`bobi/brain/turns.py:38-41`).

**An empty signature never matches anything.** `mark_terminal` writes `error`
only when truthy:

```python
# bobi/sdk.py:556-558
updates: dict = {"status": status, "pid": 0, "terminal_at": time.time()}
if error:
    updates["error"] = error
```

A terminal failure recorded with no message keeps `error == ""`. Under a naive
equality test all such entries share the empty signature and match *each
other*, so three unrelated causes would open an incident whose alert quotes
nothing.

Digit collapse opens the same hole one step further in, which round 2 found
(§14.6 F6): `"500"` and `"404"` both normalize to `"#"`, match each other, and
pass an empty-only guard. The alert would quote `#`.

The rule therefore covers both: **a normalized signature with no alphabetic
character is not a signature.** That subsumes empty, whitespace-only, and
all-digit errors. A non-signature cannot open a streak, cannot extend one, and
yields `unknown` rather than `failing`.

Still latent rather than live on both halves: of the 65 failed/crashed entries
in this deployment's registry, 0 have an empty error (re-measured 2026-10-09;
was 0 of 38, and 0 of 33 before that) and 0 normalize to a no-alpha signature.
A verified codepath with no live instance, which is why it is a stated rule plus
tests 10a and 10b rather than a guard with its own measurement.

**N=3, a constant.** The table above is the argument against 2, not an argument
for a knob: N=2 buys 4 minutes and doubles the false-positive surface, and
§3.1's three further 2-session reap clusters would all reach the condition at
N=2.

Round 2 cut the knob (§14.6 S3). §1.3's own reasoning for cutting the lookback
knob applies verbatim: nobody has asked to tune it, and promoting a constant to
an env knob later is additive and free. An arguable default is a reason to pick
the default carefully, not a reason to ship a dial, and the entire span of the
argument is four minutes of latency.

**FP2 (new 2026-10-09). A recurring same-signature cluster already exists, and
the lookback is what keeps it out.** 154 entries between 2026-08-02 and
2026-10-01 carry the signature `turn failed (kind=unknown, api_status=none)`,
across 154 separate curator dispatches of one monitor (§3.2). 129 are
`status="error"`, which §6.3 excludes because it is not in `FAILED_STATUSES`;
24 are `failed` and 1 `completed`.

The exclusion is not what saves this, which round 2 corrected (§14.6 F17).
Counting all 154 regardless of status, at most **2** land inside any one-hour
window, and the 24 `failed` ones are a minimum of 5.99 hours apart with
completions interleaved. The lookback alone excludes the cluster, and the §3.2
simulation confirms the detector never opens on it. No guard needed.

Recorded anyway, because a single recurring monitor can mint a durable shared
signature across many dispatches, and any later change that shortened N,
lengthened the lookback, or dropped the interleave rule would start firing on
it.

### 6.3 Which sessions count - mechanical inventory

Every writer of a terminal status at `83bebe49`. Two greps are needed, which
round 2 found the hard way (§14.6 F5): the obvious one misses every status
written through `registry.update`.

```
grep -rn "TERMINAL_FAILED\|TERMINAL_CRASHED\|mark_terminal" bobi/ --include=*.py
grep -rn "update([^)]*status=" bobi/ --include=*.py
```

The first returns 45 hits: the ten writers in the table below, the non-writing
hits listed after it, and the internals and call sites of `mark_terminal` /
`_persist_terminal` themselves. Re-derived 2026-10-09 as **the same ten
writers**, every line moved, no semantics changed.

| Site | Writes | Counts? | Why |
|---|---|---|---|
| `subagent.py:326-338` (`_persist_terminal`) | `failed` / `crashed` / `completed` | **yes** | The one-shot dispatch path. §3's outage is this path |
| `subagent.py:1554` | `failed` | **yes**, see FP1 | `Workflow '<name>' not found`, closing a `starting` corpse (#850) |
| `subagent.py:1580` | `crashed` | **yes** | Wait-mode backstop: `wait-mode run died: <exc>` |
| `subagent.py:1631` | `crashed` | **yes** | Detached launch failed to spawn. A real dispatch death |
| `subagent.py:2102-2105` | `failed` | **yes**, see FP1 | Same `Workflow '<name>' not found` text, from the CLI entry |
| `reconcile.py:133` | `crashed` | **yes** | Dead-man reconciler closing a dead-pid run |
| `reconcile.py:205` | `failed` | **yes** | Reconciler closing a run past its declared deadline |
| `orchestrator.py:1440` | `failed` / `completed` | **yes** | Workflow run outcomes. §3's incident is entirely this path |
| `sdk.py:593` (`_reap_if_dead`) | `crashed` | **yes** | Dead-pid crash marking on a list read. §3.1's cluster is this site |
| `webapp/run_actions.py:181` | `"cancelled"` | **no** | Operator close. Excluded *for free*: `cancelled` is not in `FAILED_STATUSES` |

Non-writing hits from the same grep, all imports, constants, or read-side
classification, none of which can open an incident: `sdk.py:39-55`,
`subagent.py:27,263,456,496-498,541`, `reconcile.py:36-40,136,139,182`,
`orchestrator.py:28`, `launch_lineage.py:27`, `webapp/runtime.py:537-549`,
`webapp/runs.py:107-117`.

**The second grep, and the three writers the first one misses.** 19 hits, every
one classified. Twelve write a non-terminal status (`running`, `idle`,
`waiting`) or the director's own `stopped`, and cannot reach this detector. The
remaining seven matter:

| Site | Writes | Counts? | Why |
|---|---|---|---|
| `sdk.py:542` (`mark_done`) | `done` | **yes, as a SUCCESS** | A legacy success alias, still in `TERMINAL_STATUSES` (`sdk.py:46`) and still live: `orchestrator.py:169` calls it as a defensive fallback when a run was left active |
| `subagent.py:2394` | `error` | **no** | `_run_verdict_agent_blocking` timeout, on a dispatched verdict agent |
| `subagent.py:2419` | `error` | **no** | Same function, attempts exhausted |
| `subagent.py:2675` (`cancel_agent`) | `cancelled` | **no** | Operator cancel. Writes no `terminal_at`, which is where all 38 `terminal_at == 0.0` entries come from |
| `session.py:701`, `:1085`, `:1119`, `:1149`, `:1386`, `:1408`, `:992` | `error` | **no** | The director's own persistent-session state |

**`done` is a success, explicitly.** §6.2's streak rule and §7.1's recovery rule
both say `completed`; read literally, a `done` entry would neither break a
streak nor close an incident. It must count as a success for both. Latent today
(0 `done` entries in the registry) and one line of implementation, but a silent
hole if left unstated.

**`status="error"` (135 entries, re-counted 2026-10-09; was 55) is excluded, on
corrected grounds.** It is not in `FAILED_STATUSES` (`sdk.py:44`), so the
exclusion is free either way. The draft justified it as "the director's own
state, already handled by `DEAD_STATES`" (§4.1 F2). That is false for two of
the writers: `subagent.py:2394` and `:2419` write `error` on **dispatched**
verdict agents, and `DEAD_STATES` only ever restarts on the director's own
`/health` status (`supervision.py:730-731`, `:740-745`), so those are handled by
nothing. 129 of §3.2's 154-entry cluster are exactly this writer.

The honest justification is narrower: `error` is the **indeterminate** status,
not an honest failure. `_run_verdict_agent_blocking`'s own docstring calls it
"indeterminate, never all clear", and it is retried before it is written. An
indeterminate outcome is not evidence the brain is dead, and the six
`session.py` sites really are the director's own state. Bringing dispatched
`error` into scope is a defensible future change and is **out of scope** (§10):
it would pull §3.2's cluster into the candidate set, where only the lookback
keeps it out (FP2).

**FP1 (new since the draft).** `subagent.py:1553-1555` and `:2102-2106` write
the same constant text for a given workflow name. Three dispatches naming a
missing workflow share a signature exactly and open an incident. That is not a
brain failure. It is still a true instance of the condition the alert actually
claims ("dispatching but not producing", §7.3), so it is a correct fire rather
than a false positive, and the operator's next action is still the quoted
string. No guard needed. Named so Gate 1 sees it and test 5a pins it.

(Round 2 corrected the line numbers here: the draft cited `:1492` and `:2023`,
which are a semaphore check and an argument unpack. See §14.6 F8.)

### 6.4 Read cost, re-measured 2026-10-09

Against this deployment's live registry: **1020 session directories, 3708
top-level entries, 857 with `state.json`, 0.66 MB of `state.json`, full parse
20.0 ms**.

Two counts, because they drive different costs and an earlier pass conflated
them (§14.6 F4). `os.scandir` enumerates **3708** entries: 1020 session
directories plus 2688 `.id` / `.model` / `.brain` sidecar files. The parse
driver is the **857** directories that hold a `state.json`.

| Measured | `listdir` entries | Session dirs | `state.json` | Full parse |
|---|---|---|---|---|
| 2026-08-11 | 1376 | not recorded | 352 | 12.1 ms |
| 2026-08-21 | 2125 | not recorded | 510 | 12.6 ms |
| **2026-10-09** | **3708** | **1020** | **857** | **20.0 ms** |

The earlier rows are `listdir` counts too, so the growth trend is internally
consistent even though the label was wrong: the sweep is up 75% in seven weeks
and 169% since the draft, the parse set is up 68% and 143%, and parse cost
tracks the parse set roughly linearly. Nothing prunes the tree. This is the one
number in the spec that is getting worse on its own.

The reader therefore bounds itself: `os.scandir` + `stat`, parse only entries
whose `state.json` `st_mtime` falls inside the lookback window.
`SessionRegistry.update` stamps `last_activity = time.time()` on every write
(`sdk.py:398`), so mtime tracks terminal writes reliably. This is an
established in-repo idiom: the same `stat().st_mtime` + cutoff shape is at
`bobi/monitors/scheduler.py:530` and `:579`. Match those.
(`bobi/workflow/state.py:236` is an mtime *sort*, not a cutoff, and was cited
here in error. See §14.6 F24.)

Cost is O(recent) parses over an O(entries) stat sweep, and the stat sweep is
irreducible: `os.scandir` has to walk the directory to find anything at all.

**The 500-entry cap is cut** (§14.6 F7, S2). It claimed to bound the stat sweep
and could not, because selecting the 500 most-recent entries by mtime requires
statting all of them first, so it only ever bounded parses, which the lookback
already bounds. The headroom it was protecting is not close: measured over 75
days of this registry, the **peak** terminal events in any one-hour window is
**19** and the peak dispatches is **27**, against a mean of 0.47 dispatches per
hour. A 500 cap is 26x the measured peak, so it is a constant, a sort, and a
paragraph that buy nothing. Registry pruning is the real fix for the sweep and
is **out of scope** (§10).

**One read per poll, not two.** An earlier draft had telemetry and the alerter
each call the detector, justified by the codebase's "re-derive rather than
trust a latch" precedent (`alerting.py:200-215`). That precedent is about not
trusting a latch *across* polls, not about deriving the same fact twice
*within* one poll. Two scans against a registry the manager is concurrently
writing can return different answers, so the heartbeat could publish
`session_health: ok` in the same poll the alerter opens an incident, and an
operator reconciling Slack against the dashboard would be looking at two
observations of the same instant.

**The mechanism is the one already in the codebase, not a memo.** A second
draft memoized the detector on the poll timestamp; round 2 pointed out that
`load_grace` already solves this exact problem without a cache (§14.6 S5).
`CompositeObserver.poll` (`supervision.py:194-209`) fans one `SupervisorState`
out to every observer in one pass, and `load_grace` rides on that object
(`supervision.py:169`, published at `snapshot.py:168`). `session_health` does
the same: the supervisor computes the observation once per cycle, attaches it to
`SupervisorState`, and both observers read the same field. No memo cache, no
invalidation question, and `state.last_restart_at` (§6.2) is on the same object.

Cost is ~20 ms per 30 s cycle (0.07% duty) at today's registry size, once rather
than twice, and the bounded read keeps it there as the tree grows.

---

## 7. The alerting edge

### 7.1 Incident model

Mirrors the crash-loop incident (`alerting.py:170-235`):

- **OPEN**: the condition in §6.2 first holds. One SOFT Slack notice carrying
  the shared error string verbatim, the failure count, and the affected session
  names.
- **DEDUP**: at most one notice per incident, marked alerted even on a log-only
  post, the same promise the existing soft alert makes (`alerting.py:176-181`).
- **CLOSE**: the first session that both **started after the incident opened**
  and reached a success (`completed`, or the `done` alias of §6.3). One
  RECOVERED notice with the incident duration.
- **ABANDON**: the detector reports `unknown` for a whole lookback window while
  an incident is open. The incident is cleared with **no** RECOVERED notice.
  See below.
- A failure with a *different* signature neither recovers nor re-opens.

**Why recovery tests `started_at`, not just `terminal_at`.** Sessions overlap.
A long investigation dispatched before the credential died can complete twenty
minutes into the incident, its brain turns having happened on the old, working
credential. Closing on it would post RECOVERED for a box that is still
brainless, standing the operator down mid-outage. Only a session born after the
incident opened proves the brain works now. §3 shows the overlap window is
real: sessions there ran 60-260s while dispatches arrived every 2-13 minutes.

`brain_availability.py` reaches the same conclusion by a different route: it
clears an incident only on a turn that actually succeeded (`:164-189`), never
on elapsed time.

**Why ABANDON exists, and why it posts nothing.** Recovery requires a session
dispatched *after* the incident opened, so if dispatch stops the incident never
closes. That is not a corner case: "act on the error above" plus a quiet box is
the normal response to this alert, and a low-traffic deployment reaches it on
its own. Round 2 found the draft had no transition for it (§14.6 F2), which
left two defects. The alerter would hold an incident open forever, never
posting RECOVERED. Worse, once the failures aged out of the lookback the
detector would publish `session_health: unknown` while that incident was still
open, so the dashboard and Slack would contradict each other permanently, which
is the exact desync §6.4's single read exists to prevent.

The rule: **one full lookback window of `unknown` with an incident open clears
it.** No RECOVERED notice, because nothing proved the brain works. The next
streak opens a fresh incident and alerts again, which is the correct behaviour
for a condition the operator has not visibly fixed. `since` is preserved in the
abandoned record only in the log line, not re-alerted.

**Limitation.** The detector observes only what is dispatched. On a box with no
dispatches at all it stays `unknown` and says nothing. That is honest (it has
observed nothing) but it does not catch "brainless *and* completely idle".
Fleet-side silence detection is the tool for that gap, and the heartbeat already
carries the `expectations` seam for it (`snapshot.py:95`). Out of scope; stated
so Gate 1 does not assume coverage this does not have.

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

[#1097](https://github.com/moda-labs/bobi-agent/pull/1097) corroborates this
since the last pass: it had to move `_save()` ahead of the budget alert
(`alerting.py:192-196`) because persistence order inside that one file is
load-bearing across a process-exit boundary. Sharing the file would put this
spec's close path into that same ordering contract.

### 7.3 Message shape

```
[bobi] moda/eng-team is dispatching but not producing: the last 3 sessions all reached
a terminal failure with the same error, none completed in 29m. Manager is healthy
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
name a missing workflow (FP1). Both are real "this agent is not producing work"
conditions worth alerting on, and neither is a dead credential. The error
string is quoted verbatim and the diagnosis is left to it.

**It says restart is not coming, and why.** The crash-loop alert promises
escalation in so many words: "Escalates to a machine restart at budget
exhaustion" (`alerting.py:275-276`). An operator reading this one must not wait
for an escalation that will never arrive.

---

## 8. Fail-open and budget neutrality

Point 3 of the issue asks for two guarantees. One is already true; the other is
one line of discipline.

**Budget neutrality is free.** `derive_manager_status` has exactly one non-test
caller, `telemetry.py:141`, and the restart state machine never consults it:
`Supervisor._cycle` (`supervision.py:687`) reads the raw `/health` body
(`supervision.py:730-731`). Nothing added to the derived verdict, in either
shape, can reach a restart decision. Re-derived mechanically at `83bebe49` from
`grep -rn derive_manager_status bobi/ tests/`: one definition, one caller, six
test call sites, nothing in `supervision.py`.

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

**One** new `WATCHDOG_*` env knob, matching the existing convention
(`bobi/supervisor/config.py:1-9`, `57-111`):

| Var | Default | Meaning |
|---|---|---|
| `WATCHDOG_SESSION_FATAL_ENABLED` | `1` | Kill switch |

Down from four in the 2026-08-21 draft: §1.3 cut the lookback and restart-grace
knobs, and round 2 cut the streak knob (§14.6 S3). Everything else is a module
constant: N=3, the lookback (3600s), and the restart exclusion window (300s).

- N has no requester, and §6.2 shows the whole argument is four minutes of
  latency. Promoting a constant to an env knob later is additive and free.
- The lookback likewise has no requester.
- The restart exclusion is a correctness guard against a false positive this
  deployment has actually produced (§3.1). Exposing it invites setting it to 0.

The kill switch stays because this reads a shared on-disk surface the manager
owns concurrently; an operator must be able to turn it off without a rollback.
Load grace shipped the same way (`WATCHDOG_LOAD_GRACE`,
`bobi/supervisor/config.py:81`, `:105`).

---

## 10. Scope

**In** (10 files):

| File | Change |
|---|---|
| `bobi/supervisor/probe.py` | detector, signature normalizer, bounded registry reader |
| `bobi/supervisor/config.py` | the one knob in §9 |
| `bobi/supervisor/snapshot.py` | `build_heartbeat` carries the `session_health` key |
| `bobi/supervisor/telemetry.py` | publishes `session_health` from the observation on `SupervisorState` |
| `bobi/supervisor/alerting.py` | the incident edge and its second state file |
| `docs/ADMIN_PROTOCOL.md` | heartbeat schema, same PR. No lifecycle-table row, since the episode pair was cut |
| `tests/test_supervisor_telemetry.py` | detector tests 1-10b |
| `tests/test_supervisor_alerting.py` | alerter tests 11-16 |
| `tests/test_supervision_restart.py` | integration test 17 |
| `tests/fixtures/supervisor_stub_manager.py` | new `brainless` mode |

`snapshot.py` is on this list because `build_heartbeat` (`snapshot.py:136-178`)
is the literal dict the payload is assembled from. Load grace touched the same
five sidecar files plus one new module, which is the closest available estimate
of this shape's real cost.

This spec adds no new module: the detector lives in `probe.py` beside
`status_file_age`, which already does the same kind of bounded registry read
from outside the manager.

**Out, deliberately:**

- **Widening `classify_brain_unavailability`** to catch §3's string. It is
  narrow by design (G1); widening it re-opens the transient/terminal confusion
  #855 closed.
- **Routing `system/brain.*` to an operator** (G3). A deployment
  subscription change, not framework work, and it belongs with whoever owns the
  team's subscription list.
- **Registry pruning.** Nothing prunes `state/sessions/`; **1020** session
  directories and **3708** `listdir` entries here, the sweep up 75% in seven
  weeks (§6.4). The detector's lookback bounds what it parses, so it does not
  depend on a fix, but the unbounded directory is a runtime-wide problem.
  Separate issue.
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
**19** after #1097; driven by injected doubles and a `Clock`).
`tests/test_supervisor_load.py` (25 tests, added by #1022) is the closest model
for a new sidecar signal's test shape. Counts re-derived 2026-10-09 with
`grep -c 'def test_'`.

**Detector** (`tests/test_supervisor_telemetry.py`):

1. N same-cause failures -> `failing`; the shared error string is carried through.
   Fixture: §3's 13 real entries, so the test asserts against the incident the spec exists for.
2. N failures with *different* signatures -> `ok`. The discriminator is the signature, not the count.
3. A success interleaved in the streak -> `ok`. Parametrized over `completed` and the `done` alias (§6.3).
4. `cancelled` (operator close) never counts, and nor does `error` (§6.3). Parametrized over both.
5. `cancelled` written with no `terminal_at` (`subagent.py:2675`) is skipped rather than sorted to the epoch (§6.3).
5a. Three `Workflow '<name>' not found` failures DO open the edge, and the alert quotes that string (FP1). Pins the intended behaviour so a later reader does not "fix" it.
6. An unreadable or absent registry -> `unknown`, never `failing`.
7. Only entries inside the lookback window are considered.
8. **The detector performs no writes.** Assert `state.json` mtimes are unchanged across a detection run. The direct regression test for §4.1 F1.
9. A `wedged` / `down` manager with N failed sessions -> no session-fatal edge (§6.2 manager-alive guard).
10. **Entries reaped inside the post-restart window never open an edge, at any poll time.** The regression test for §6.2's restart exclusion, and the only false positive this deployment has actually produced. Fixture: §3.1's five real entries sharing `DIED_WITHOUT_TERMINAL`. **Asserts at three clock positions: inside the 300s window, at grace + 1s, and at grace + 30 min.** The last two are the ones that fail against the drafted "no restart in the last 300s" guard (§14.6 F1), so a test that only polls immediately after the restart passes the bug.
10a. **N failures whose signatures carry no alphabetic character -> `unknown`, never `failing`.** Parametrized over empty, whitespace-only, and all-digit (`"500"` / `"404"`) errors (§6.2).
10b. Signature normalizer as a unit: multi-line error takes the first line only; >120 chars truncates; digit runs collapse so `resets 12:40am` and `resets 1:05am` match; and `"500"` normalizes to a non-signature rather than a universal match.

**Alerter** (`tests/test_supervisor_alerting.py`):

11. One SOFT post at onset; a second poll in the same condition posts nothing.
12. RECOVERED on a success that started after the incident opened, with duration. Parametrized over `completed` and `done`.
13. **A `completed` session that started BEFORE the incident opened does NOT close it.** The regression test for the overlap hole in §7.1.
14. Incident survives a simulated process restart via the state file.
15. **Closing a session-fatal incident leaves an open crash-loop incident and its `exhaust_cycles` intact** (§7.2). Also asserts the inverse: a crash-loop `_clear()` does not erase an open session-fatal incident.
16. **An incident open with no dispatches for a full lookback window is ABANDONED**: the incident file is cleared, NO RECOVERED notice is posted, and the heartbeat reads `unknown` rather than contradicting an open incident (§7.1). The regression test for §14.6 F2.

Cut from this list: "a raising `post_fn` does not propagate" (§1.3) and "one
scan per poll" (round 2, §14.6 S5). The first is covered by the existing suite
through the shared `_post`. The second has nothing left to assert once the
observation is computed once per cycle and carried on `SupervisorState` rather
than memoized behind a call the test would have to count.

**Integration** (`tests/test_supervision_restart.py`, 5 tests, extending
`tests/fixtures/supervisor_stub_manager.py` with a `brainless` mode beside the
existing `wedge-then-recover` / `always-idle` / `dead-then-recover` /
`busy-wedge-then-recover` (`supervisor_stub_manager.py:43-44`): registers a
healthy `idle` director, serves `/health`, writes N failed session entries):

17. **A real `Supervisor` over a real stub manager in `brainless` mode performs ZERO restarts and the restart budget count stays 0**, while the alerter posts exactly one notice. Point 3 proven end to end rather than argued from §8.

Per `CLAUDE.md`, no real-Claude e2e leg: the change is brain-agnostic (it reads
persisted registry state and posts Slack), so the stub path is where the risk
lives.

Plus `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ -q`.

**The whole-registry acceptance check is cut** (§14.6 F3, F23). A draft proposed
running the detector over this deployment's entire registry and asserting a
fixed number of opens. It cannot be a test: it reads an absolute path on one Fly
volume that no CI runner has, nothing in `tests/` reads that path today, the
registry is unpruned and grew while round 2 was reading it, and the expected
number it pinned was wrong. §3.2 is the right home for that measurement, as a
recorded number in the design doc. Tests 1 and 10 keep its value by building
synthetic fixtures out of the real entries.

**Reporter-side validation, offered and accepted.** The issue closes with an
offer this plan should use: "Happy to test a pre-release against our
deployment - this exact failure mode is reproducible for us on demand (revoke a
token, watch the silence)." That is the only validation available against a real
outage rather than a fixture, it costs nothing, and the `brainless` stub mode
(test 17) does not substitute for it. Take the offer after Gate 1 and before
close: ship a pre-release, have the reporter revoke a token, and confirm one
SOFT notice with the right quoted string, zero restarts, and one RECOVERED on
re-mint.

---

## 12. Open questions for Gate 1

### Q1 (the one blocking question): S, R, or M?

**The question, stated once:** does the session-fatal condition become a new
`failing` value in `manager.status` (**S**, the issue's sketch), a new
orthogonal `session_health` heartbeat block (**R**, recommended), or an
alerter-only change with no wire surface at all (**M**)?

**Recommend R**, unchanged and now with more behind it:

1. Both consumers of the `manager.status` enum render an unknown value as a
   green **running** chip, so S alerts while the fleet view keeps lying. Both
   files are byte-identical to the last pass (W1), and they are one deny-list
   surfaced twice rather than two independent surfaces.
2. The protocol's compatibility promise covers new *fields*, not new
   inhabitants of a documented enum whose table it publishes
   (`ADMIN_PROTOCOL.md:27-31`, `:314`, `:338-345`), and the doc tells consumers
   to "switch on `running`/`idle` and on `down`" (`:347-349`).
3. A brainless-but-quiet box has two true facts. `manager.status=idle` **and**
   `session_health=failing` is the diagnosis; either alone is not.
4. Load grace faced this exact fork on the inverse failure mode and chose R
   (§5.4), publishing both an additive block and its own episode pair
   (`ADMIN_PROTOCOL.md:376-377`).

**This is a yes/no, not a research question.** Overriding to S or M is Zach's
call and changes the shape of the code, not a constant. Nothing in this pass
moved the balance; it only removed reasons to re-derive it.

### Q2 (non-blocking)

- **N value.** 3, now a constant rather than a knob (§9, §14.6 S3). §6.2's table
  makes 2 defensible on latency (4 minutes earlier), but §3.1's three further
  2-session reap clusters would all reach the condition at N=2, so 3 is the
  conservative read of a shared on-disk surface. Changing it is a one-line
  constant edit, not a design change.
- **The lifecycle episode pair.** Round 2 cut `sessions_failing` /
  `sessions_recovered` as unconsumed (§5.2, §14.6 S1). If Gate 1 wants the
  episode record on `fleet/lifecycle` for symmetry with `probe_failing` and
  `load_grace_active`, it is two lines in `telemetry.py` plus a doc row. Say so
  and it goes back in.
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
| [#1063](https://github.com/moda-labs/bobi-agent/issues/1063) otel tool probe basename fallback resolves the wrong agent | **closed** ([#1068](https://github.com/moda-labs/bobi-agent/pull/1068), 2026-08-21) | **Unrelated. Different "probe".** #1068 touched `bobi/tool_library/otel/tool.yaml`, `paths.agent_name`, and the dep-bootstrap preflight. `git show --stat 4309bb6d` contains no `bobi/supervisor/` file, and `bobi/supervisor/probe.py` is still byte-identical to this spec's original baseline. Changes no premise here |
| [#1097](https://github.com/moda-labs/bobi-agent/pull/1097) incident not persisted before the budget alert | **merged** 2026-09-24 | **Adjacent, and corroborating.** The only supervisor change since the last pass. It moved `_save()` ahead of the synchronous alert in `_on_exhausted` because persistence order inside `supervisor-incident.json` is load-bearing across the exit-70 boundary. That is an independent argument for §7.2's second state file: sharing the file would put this spec's close path inside that contract |
| `session.completed` fires repeatedly for one session | **no issue filed** | **Adjacent, and an argument for §6.1.** `reconcile.py:176-188` re-emits any terminal entry whose `emit_confirmed` is False on every wake, so a box with failing bus POSTs streams stale completions. A lifecycle-bus detector would read that as health; the registry-reading detector is immune. Out of scope (§10), named because it motivates the read-source choice |

None of these should be absorbed into #992, and #992 should not be folded into
any of them: four are closed or merged, and the remaining one needs its own fix
in `reconcile.py`. The one open question this raises is Q3.

---

## 14. Review record

### 14.1 The cross-model opinion is still owed

**This spec has had a same-model adversarial pass only.** Cross-model review is
unavailable in this worker container, re-verified by running both tools on
2026-10-09 rather than assumed:

```
$ codex exec -s read-only "Reply with the single word OK." < /dev/null
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic
       authentication in header, url: https://api.openai.com/v1/responses
$ ls /home/bobi/.codex/auth.json
ls: cannot access '/home/bobi/.codex/auth.json': No such file or directory

$ aichat "Reply with OK."
Error: Failed to load config at '/home/bobi/.config/aichat/config.yaml'
Caused by: No such file or directory (os error 2)
```

`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `AICHAT_PLATFORM`,
`AICHAT_OPENROUTER_API_KEY`, and `ANTHROPIC_API_KEY` are all unset in this
container. Unchanged across three passes (2026-08-11, 2026-08-21, 2026-10-09).

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
| E1 | Architecture | **Two registry scans per poll can disagree.** The heartbeat could publish `ok` in the same poll the alerter opened an incident | 8/10 | §6.4 one read per poll. The memo and its test 10d were later replaced by carrying the observation on `SupervisorState` (§14.6 S5) |
| E2 | Architecture | **`snapshot.py` was missing from the in-scope list.** `build_heartbeat` is the literal payload dict | 9/10 | §10 file table |
| E3 | Tests | **The empty signature matches itself.** `sdk.py:556-558` writes `error` only when truthy (the draft cited `:521-523`, which is cost-usage code) | high | §6.2; tests 10a, 10b |
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
does (`sdk.py:624-627`; the draft cited `:589-592`, which is `_reap_if_dead`),
so a torn file is invisible rather than fatal. It
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
| RV3 | The terminal-writer inventory gained four sites; two write identical constant text | §6.3, FP1, test 5a |
| RV4 | Registry grew 1376 -> 2125 dirs in ten days; parse cost held at ~12 ms; the empty-signature trap is still latent (0 of 38) | §6.2, §6.4 |
| RV5 | F2's `DEAD_STATES` hazard is narrowed by load grace but not removed: a brainless box consumes no CPU, so grace will not defer it | §4.1 F2 |
| RV6 | #1063/#1068 is a different probe and changes no premise | §13 |
| RV7 | The draft's "#522" citation for the cross-model gap was wrong; #522 is a merged PR. No open issue tracks it | §14.1 |
| RV8 | `reconcile.py:176-188` re-emits terminal sessions whose bus POST never landed, on every wake. Strengthens the registry-over-bus choice | §6.1, §10, §13 |

### 14.5 Re-validation pass, 2026-10-09

Triggered by Zach on Slack: "can you recheck through PR #1008 and see if it also
needs updating? Looks like a bug we should fix." Branch merged from `main`
(`83bebe49`, no conflicts); every citation re-read, every behavioural claim
re-run.

**Verdict: VALID WITH UPDATES.** No claim fell. Nothing on `main` implemented
any part of it. The design surface is byte-identical.

| # | Finding | Folded into |
|---|---|---|
| X1 | `git diff ac2471e6 83bebe49` is EMPTY for probe.py, snapshot.py, telemetry.py, brain_availability.py, brain/, reconcile.py, event_bus.py, shell.js, ADMIN_PROTOCOL.md. The fork-deciding claims and the three #855 gaps need no re-argument | §1 W1, §2, §5.1, P1 |
| X2 | #1099 added an `inbox` key to `/health`'s sessions projection: four keys now, still none of the three the detector needs | §1 W3, P3, P4 |
| X3 | #1097 moved `_save()` ahead of the budget alert. Independent evidence that ordering inside `supervisor-incident.json` is load-bearing, which argues for §7.2's separate file | §1 W2, §7.2, §13 |
| X4 | **The restart-reap false positive is real, not hypothetical.** 2026-08-03 21:09:09-10: three sessions, one `DIED_WITHOUT_TERMINAL` signature, one second apart | §3.1, §6.2, test 10 |
| X5 | The detector simulated over the registry's terminal entries opens three times, all explained | §3.2. **SUPERSEDED by §14.6 F3/F11:** independently re-derived as **two** opens over **669** detector-visible events, and the acceptance check this fed is cut |
| X6 | A new 154-entry shared-signature cluster exists across 154 curator dispatches. The lookback excludes it outright, at most 2 inside any hour (credit corrected in round 2) | §3.2, §6.2 FP2 |
| X7 | Registry sweep up 75% in seven weeks (2125 -> 3708 `listdir` entries), parse set 510 -> 857, parse 12.6 -> 20.0 ms. Round 2 relabelled the count and cut the 500 cap | §6.4, §10, §14.6 F4/F7 |
| X8 | No new session-limit or auth burst since 2026-07-31. Three isolated singles, no streak. One burst in 14 weeks | §3.3 |
| X9 | Terminal-writer inventory re-derived: same ten writers, every line moved, no semantics changed | §6.3 |
| X10 | **Simplicity pass:** two knobs and two spec components cut, no capability lost | §1.3, §9, §6.2, §11. Round 2 cut six more (§14.6 S1-S6) |

**Not folded, and why.** This pass found nothing that required a design change.
Its one judgement call was whether to cut the `sessions_failing` /
`sessions_recovered` lifecycle pair as unconsumed, and it **kept** the pair:
load grace ships its own documented episode pair
(`ADMIN_PROTOCOL.md:376-377`) under the same precedent §5.4 leans on, so cutting
it would make this the only sidecar signal without an episode record.

**Both halves of that are now superseded.** §14.6 found a blocker-class design
defect this pass missed (the restart guard does not guard), so "nothing required
a design change" was wrong. And §14.6 S1 reverses the keep: no episode event in
the repo has a consumer, so load grace having shipped an unconsumed pair is not
an argument for a second one. §5.2 records the cut and Q2 re-poses it for Gate 1.

### 14.6 Independent review round, 2026-10-09

A fresh-context subagent with no authorship context, read-only, re-ran every
citation and every empirical number and hunted over-design. Its report is
committed verbatim at `plans/reviews/2026-10-09-992-review-1.md`.
It verified 96 of roughly 100 citations by running a command and listed the
other 4 as unverified.

Every finding below was re-triaged against the code and the registry before
folding, with the command re-run rather than taken on trust.

**Verdict it returned:** citations near-perfect, Shape R and the Q1 argument
intact, design not shippable as written.

| # | Sev | Finding | Disposition |
|---|---|---|---|
| F1 | BLOCKER | **The restart grace does not close the false positive it exists for.** Stated as "no restart within the last 300s" it suppresses for 300s while the reaped entries sit in the 3600s lookback. Re-simulated against §3.1's cluster, the spurious incident opens at restart + 300s | **FOLDED.** §6.2 restates it as an exclusion on the entries; §3.1 records the hole; test 10 now asserts at three clock positions, two of which fail the drafted guard |
| F2 | MAJOR | **A `failing` incident has no close path if dispatches stop**, and once the failures age out the detector publishes `unknown` while the alerter holds an open incident, permanently | **FOLDED.** §7.1 gains an ABANDON transition; the §6 diagram and the §7.1 Limitation paragraph are corrected; test 16 pins it |
| F3 | MAJOR | **"Opens exactly three times" does not reproduce: it is two.** 22:39 is an N=1 moment and 23:48 never materializes, because the inter-failure gap peaks at 25.5 min so the lookback never empties | **FOLDED.** Re-derived independently: 2 opens, condition holds at 14 moments. §3.2 rewritten; §11's acceptance check cut |
| F4 | MAJOR | **"3684 session directories" is an `os.listdir` count.** 1020 directories plus 2688 `.id` / `.model` / `.brain` sidecars | **FOLDED.** §6.4 reports both counts under their real names and restates the growth against each |
| F5 | MAJOR | **The terminal-writer inventory is incomplete** and the `status="error"` exclusion rests on a false premise: `subagent.py:2394` / `:2419` write `error` on *dispatched* sessions, so `DEAD_STATES` does not handle them; `sdk.py:542` writes the `done` success alias | **FOLDED, with one part declined.** §6.3 adds the second grep, the three writers, and a `done`-is-a-success rule, and re-justifies the exclusion honestly. Bringing dispatched `error` into the candidate set is **declined** and recorded in §10: it is the indeterminate status, retried before it is written, and admitting it pulls §3.2's 154-entry cluster into the candidate set where only the lookback keeps it out |
| F6 | MAJOR | **An all-digit error matches itself.** `"500"` and `"404"` both normalize to `"#"` and pass an empty-only guard | **FOLDED.** §6.2's rule becomes "no alphabetic character is not a signature", which subsumes empty, whitespace, and all-digit. Tests 10a and 10b parametrized |
| F7 | MAJOR | **The 500-entry cap does not bound the stat sweep** it claims to, since selecting the 500 most-recent by mtime requires statting all of them | **FOLDED by cutting the cap.** It only ever bounded parses, which the lookback already bounds, at 26x the measured peak hourly rate |
| F8 | MINOR | FP1 cited `subagent.py:1492` and `:2023`, a semaphore check and an argument unpack, contradicting §6.3's own table | **FOLDED.** Corrected to `:1553-1555` and `:2102-2106` |
| F9 | MINOR | The cluster is 154 *distinct* curator dispatches, not "one monitor session", so each is its own streak candidate | **FOLDED** in §3.2 and FP2 |
| F10 | MINOR | §3.1's reap is **five** sessions over **10.1s**, not three over one second, and three further 2-session clusters of the same signature exist | **FOLDED.** §3.1 quotes all five and uses the near-misses as a second argument for N=3 |
| F11 | MINOR | "662 entries with `terminal_at > 0`" is 804 as stated, or 669 for the subset the detector sees | **FOLDED.** §3.2 states both and says which one the rate is computed over |
| F12 | MINOR | §10 scoped "alerter tests 11-16" after §1.3 cut test 16, leaving a numbering hole | **FOLDED.** The hole is filled by the new abandon test (16) and §10's ranges corrected |
| F13 | MINOR | Dangling cross-reference to §14.6, the only one in the document | **FOLDED.** This section is it |
| F14 | MINOR | §14.3 cited `sdk.py:521-523` (cost-usage code) for the truthy-error write and `:589-592` (`_reap_if_dead`) for the skip-unparseable branch | **FOLDED.** Corrected to `:556-558` and `:624-627`, with the old cites named |
| F15 | MINOR | §6.2's gate table mixes `terminal_at` and `started_at` while §3 asserts it is all `started_at` | **FOLDED.** The table labels its column per row and restates both opens in one column |
| F16 | MINOR | "roughly 100x the real hourly rate" matches neither the mean (1058x) nor the peak (26x) | **FOLDED with F7**, which removed the claim along with the cap |
| F17 | MINOR | The `status="error"` exclusion is not what keeps the cluster out: even counting all 154, at most 2 land inside any one-hour window | **FOLDED.** §3.2 and FP2 move the credit to the lookback and label the old claim as corrected |
| F18 | MINOR | Sourcing the restart grace from the alerter's `manager_restarted` handler gives the two observers different views, which is the defect §6.4 exists to prevent | **FOLDED with F1.** The input is `state.last_restart_at` (`supervision.py:157`) |
| F19 | NIT | Q1 cited `ADMIN_PROTOCOL.md:348-350`; line 350 is blank | **FOLDED** to `:347-349` |
| F20 | NIT | `build_heartbeat` is `snapshot.py:136-178`, not `:136-176` | **FOLDED** |
| F21 | NIT | The "re-derive rather than trust a latch" precedent is `alerting.py:200-215`; `:196-198` is #1097's `_save()` pair | **FOLDED** |
| F22 | NIT | `R3` / `R4` had no `R1` / `R2` and collided with Shape R; `P2` meant both a premise and a severity | **FOLDED.** Renamed FP1 / FP2; the severity use of P2 dropped |
| F23 | NIT | The whole-registry acceptance check reads an absolute Fly-volume path no CI runner has, against a registry that grew during the review | **FOLDED by cutting it.** §3.2 keeps the measurement; tests 1 and 10 keep its value as fixtures |
| F24 | NIT | `workflow/state.py:236` is an mtime *sort*, not a cutoff | **FOLDED.** Cite dropped to the two real cutoffs with the error named |
| F25 | NIT | P10's "first `client.connect()`" is wrong: `orchestrator.py:879` precedes `:1172` | **FOLDED.** All four call sites named; the ordering conclusion is unaffected |
| F26 | NIT | The issue offers a reporter-side validation ("revoke a token, watch the silence") that §11 silently dropped | **FOLDED.** §11 accepts it as a post-Gate-1, pre-close step |

**Simplicity pass.** The reviewer's KEEP/CUT recommendations, each with the lost
capability named. Six cuts taken, four keeps confirmed.

| # | Item | Disposition |
|---|---|---|
| S1 | `sessions_failing` / `sessions_recovered` | **CUT** (§5.2). Nothing in the repo consumes any episode event, so load grace having shipped an unconsumed pair is not an argument for a second one. Adding a lifecycle edge later is two lines plus a doc row. **This reverses the previous pass's judgement call**, which kept the pair for consistency with that precedent. Re-posed as a Q2 item so Gate 1 can put it back |
| S2 | The 500-entry cap | **CUT** (§6.4, F7) |
| S3 | `WATCHDOG_SESSION_FAIL_STREAK` | **CUT** (§9). §1.3's own argument for cutting the lookback knob applies verbatim, and the whole span of the N argument is four minutes |
| S4 | `last_ok_at` | **CUT** (§5.2). No consumer reads it, the recovery rule compares against the incident's open time, and `terminal_at` already answers it. Same argument §1.3 used to cut session lifetime |
| S5 | The poll-timestamp memo | **CUT, mechanism replaced** (§6.4). `load_grace` already rides on `SupervisorState`, which `CompositeObserver` fans out in one pass. Compute once per cycle and carry it there: no memo, no invalidation question, and test 10d loses its subject |
| S6 | The whole-registry acceptance check | **CUT** (§11, F23) |
| S7 | `supervisor-session-incident.json` | **KEEP.** `_clear()` really unlinks the whole file (`alerting.py:122-128`), and `_check_recovery` calls it before posting RECOVERED, so a shared file loses an open crash-loop incident's `exhaust_cycles`. The one extra component that pays for itself |
| S8 | `WATCHDOG_SESSION_FATAL_ENABLED` | **KEEP.** First sidecar feature reading a surface the manager writes concurrently; without it a rollback is the only remedy. `WATCHDOG_LOAD_GRACE` is the exact precedent |
| S9 | `state: "unknown"` | **KEEP.** An absent key is indistinguishable from an old supervisor, and `load_grace: null` is the precedent. F2's fix is the ABANDON transition, not deleting the state |
| S10 | The 10-file scope, `failures`, `since` | **KEEP.** Every file is load-bearing under R, and both fields have a named consumer in the alert and the RECOVERED duration |

**What this round could not verify.** Carried from the reviewer's own section 6,
and not papered over:

- The 2026-08-11 and 2026-08-21 rows in §6.4's table. Historical; the registry
  keeps no snapshot to re-derive them from. If they were `listdir` counts too,
  which their magnitudes suggest, the growth trend is internally consistent.
- Whether §3.1's reap was a *manager restart* specifically rather than a host
  reboot or an OOM kill. The first reap write was used as the restart proxy. F1's
  conclusion does not depend on which it was.
- Whether the existing 24 / 19 / 25 / 5 supervisor tests currently pass. The
  counts were verified with `grep -c 'def test_'`, not by execution.
- A full-tree diff for W1. Each of the nine paths was confirmed empty
  individually, so a rename into one of them would not show.

**Still same-model.** This round was a fresh-context subagent on the same model
family, not a cross-model opinion. §14.1 stands unchanged.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/gstack-plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 5 issues, 0 critical gaps, all folded in |
| CEO Review | `/gstack-plan-ceo-review` | Scope & strategy | 0 | LENS ONLY | scope challenge answered inline (§14.3 Step 0); full skill not invoked |
| Design Review | `/gstack-plan-design-review` | UI/UX gaps | 0 | NOT RUN | no UI surface in scope; §10 defers the only rendering work |
| Codex Review | `codex exec` | Independent 2nd opinion | 0 | NOT RUN | codex 401 not authed, re-tested 2026-10-09 (§14.1) |
| Independent citation check | fresh-context subagent | Verify every cite by running greps on current `main`; hunt over-design | 2 | FOLDED | Round 1: 26 findings (1 blocker, 6 major, 10 minor, 9 nit) + 10 simplicity calls, all triaged in §14.6. Round 2 re-reviewed the revision, see §14.7. Read-only, same model, no authorship context |
| DX Review | `/gstack-plan-devex-review` | Developer experience gaps | 0 | NOT RUN | not bound for this role |

Only the Eng leg ran as its skill. This role binds a three-leg spec review for
non-plan-born work, so **two legs are outstanding**: the CEO/scope leg got its
substance inline via the Step 0 challenge but not the full skill, and the design
leg was not run. The design gap is defensible on the merits, since the only
rendering work is explicitly deferred in §10, but it is a gap.

**CROSS-MODEL:** none available. Every review leg above is same-model,
disclosed in §14.1.

**VERDICT:** ENG cleared at the spec level; CEO covered by lens only; DESIGN not
applicable. Two independent review rounds folded (§14.6, §14.7), including one
blocker-class design defect in the restart guard. Implementation is NOT
authorized: this spec is stopped at Gate 1 pending the Q1 ruling, and the
cross-model adversarial leg is still owed.

**UNRESOLVED DECISIONS:**

- **Q1**: shape S, R, or M. Recommend R, with the `load_grace` precedent behind
  it and both consumer surfaces re-verified byte-identical. Zach's call. No code
  until it is answered.
- **Q2**: N of 3 vs 2 (now a constant, not a knob); the lifecycle episode pair,
  cut in round 2 and cheap to restore; alert channel shared with crash-loops or
  its own; publish `session_health: unknown` or omit the block.
- **Q3**: whether this alert should also fire a `system/brain.*`-style topic so
  one subscription covers both signals.
- Two review legs outstanding: the CEO/scope skill (substance covered inline by
  §14.3's Step 0 challenge) and the cross-model adversarial pass (§14.1).
  Neither blocks the Q1 ruling.
