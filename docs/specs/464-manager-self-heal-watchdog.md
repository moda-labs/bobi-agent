# Spec — #464: Manager self-heal watchdog (restart a wedged director, defense-in-depth)

- **Issue:** [#464](https://github.com/moda-labs/bobi-agent-team/issues/464)
- **Status:** Draft — awaiting Zach's approval on this PR. **No implementation lands until approved.**
- **Complexity:** Medium/High (infra; spec-first because the restart-authority and stall-signal design questions are open).
- **Build-vs-adopt:** **Build** — there is no off-the-shelf supervisor that understands our `last_activity`/turn-state semantics. The watchdog is ~150 lines on top of the existing health endpoint; the restart authority is the container init we already run under.

This spec is a superset of the issue body and of Zach's R5 review of #460
(comment [4785980738](https://github.com/moda-labs/bobi-agent-team/pull/460#issuecomment-4785980738),
item #4, "Manager self-heal watchdog"), which raised this as an explicit
separate follow-up. It cross-references the #456 spec
(`docs/specs/456-policy-curator.md`, *Out of scope* and *Related*), which
defers the watchdog here by name.

---

## Problem & Solution

### Problem

`stall-recovery` is **director→engineer**: the director notices a stalled
*engineer* session and recovers it. By construction it cannot recover the
**director itself** — when the director wedges, there is nothing above it
inside the application to act. Today that path is **manual**: a human notices
(Slack "thinking…" never resolves), then runs `fly machine restart`. It
happened on prod **twice**, including 2026-06-24 on 0.31.0.

The #456 work (PR #460) bounds the **one known** wedge mechanism — the
unbounded/unrecoverable `_rotate()` reconnect (`session.py` mechanism #3). That
is necessary but not sufficient: it closes an *enumerated* hang. Nothing covers
**unknown** wedge classes — any future codepath where the director's run loop
stops returning to `inbox.recv` while events queue unanswered.

The health endpoint already exists (`manager_health.py`, bound on localhost,
port discoverable via `state/manager-health.port`; the issue refers to it as
`127.0.0.1:45985`). But **nothing observes `last_activity` and nothing acts on
a stall.** The container's Fly healthcheck (`docker/healthcheck.sh`) only probes
that `/health` returns `200` — and a wedged director *still answers 200*,
because the health server is a **daemon thread** independent of the wedged
asyncio event loop (`manager_health.py:_thread`). So the existing liveness probe
is blind to the exact failure we care about: process alive, director dead.

### Solution

A **manager self-heal watchdog**: a small supervisor that lives **below** the
director's event loop, observes the director's `last_activity` **and turn
state** via the health endpoint, and — when the director is stuck *making a
turn* past a bounded threshold — restarts it loudly, with bounded retry and
backoff, no silent park. It is generic by design: it keys off *progress*, not
*cause*, so it covers wedge classes we have not enumerated.

It is **defense-in-depth, not a replacement** for #456: #456 lowers the
probability of the known hang; the watchdog is the catch-all backstop for
everything else.

---

## The core design problem: "stalled" ≠ "wedged"

`last_activity` (the registry field, `sdk.py:200`, bumped by
`SessionRegistry.update`/`record_cost` on every turn event) goes stale in **two**
situations that look identical from the outside:

| Situation | `last_activity` | Director state | Correct action |
|-----------|-----------------|----------------|----------------|
| **Healthy idle** — no events; parked at `inbox.recv` | frozen | `status="idle"` (`session.py:468`) | **do nothing** |
| **Wedged** — stuck inside a turn that never completes | frozen | `status="running"`/`"starting"` | **restart** |

A naïve "restart if `last_activity` older than T" would **kill a perfectly
healthy idle director** the moment the team goes quiet for T. That is the
trap, and it is why this needs a spec rather than a one-liner.

**The discriminator already exists in the session status machine** (verified in
`bobi/session.py`):

- `_drain_turn()` sets `status="running"` on entry (`:249`) and only flips to
  `status="idle"` when a `ResultMessage` arrives (`:299`).
- The main run loop parks on `inbox.recv` with `status="idle"` (`:468`,
  `:358–372`) — this is healthy waiting; `last_activity` legitimately freezes.
- The 2026-06-24 hang is *inside* `_rotate()` → `_drain_turn()` (`:187`): the
  fresh connect turn never yields a `ResultMessage`, so status stays
  `"running"` and `last_activity` never advances. Likewise a boot/connect hang
  leaves `status="starting"` (`:621`).

So the wedge signal is precise:

> **Restart iff the director session is in an _active_ state
> (`status ∈ {starting, running}`) AND `now − last_activity > STALL_THRESHOLD`.**
> An `idle`/`stopped`/`done` director is never restarted for inactivity,
> regardless of how long it has been quiet.

This makes the watchdog *correct* (no false kills of idle directors) and
*generic* (any wedge that leaves the director mid-turn trips it, not just the
rotation reconnect).

---

## Design

### 1. Restart authority: a supervisor **below** the application

The issue requires "a layer below the application that restarts a wedged
manager regardless of why it wedged." Today the container `exec`s the manager
directly as PID 1 (`docker/docker-entrypoint.sh:158`:
`exec gosu … bobi start --foreground "$@"`), so **nothing in-container
parents the manager** — only Fly's machine-level init does, and Fly only acts
on process death, not on a live-but-wedged process.

**Recommended (D1 = A): a thin supervisor parent — `bobi supervise`.**
The entrypoint execs `bobi supervise -- --foreground "$@"` instead of
`bobi start`. The supervisor:

1. Spawns `bobi start --foreground …` as a **child process**.
2. Runs the watchdog **loop in the supervisor process itself** — the supervisor
   never runs the agent event loop, so it *cannot* wedge from the same cause.
3. On a confirmed wedge → terminates the manager child (`SIGTERM`, grace,
   then `SIGKILL`) and **relaunches it** — the fresh manager re-spawns the
   director, which re-registers its event-server deployment and becomes
   **addressable** again (`session.py:508`, `inbox/<self>`).
4. Propagates `SIGTERM` to the child for graceful container shutdown.
5. After exhausting bounded retries, **exits non-zero** so Fly's machine
   restart policy replaces the machine — escalation, *not* a silent park.

This is genuinely "below the application": the supervisor is the **parent**;
the manager (host of the director) is its child; the director (the agent) is a
grandchild. A restart brings the director back to addressable **within one
container life**, which is what makes the acceptance integration test runnable
in CI without Fly.

Process-tree notes (verified against `Dockerfile:163` — "no tini; Fly Machines
inject their own PID-1 init"): the supervisor is the container **entrypoint
process under Fly's injected init**, not literally PID 1, so zombie reaping is
Fly's job — but the supervisor must still `wait()` its manager child and forward
`SIGTERM`/`SIGINT` so a graceful container stop is clean. If the **supervisor
itself** crashes (it runs no agent loop, so it cannot wedge from the agent
cause), its exit propagates to Fly's init → machine restart. That is the outer
backstop for the backstop.

**Why not the alternatives** (recorded, not chosen):

- **Watchdog as a daemon thread inside the manager** — it *would* keep polling
  (threads survive the wedged event loop), but a thread cannot cleanly restart
  its own process "from below"; its only lever is `os.execv`/self-`SIGKILL`,
  which is harder to test and muddier to reason about than a parent. Rejected:
  not "below the application."
- **Fail the Fly healthcheck on stale `last_activity`** (teach
  `healthcheck.sh` to read `last_activity` and exit non-zero) — cheapest, and
  Fly *is* a layer below. Rejected as the *primary* mechanism because (a) it
  cannot distinguish idle from wedged without the status discriminator anyway,
  (b) restart cadence is Fly-config-coupled and slow, and (c) it is **not
  locally testable** (the acceptance test can't drive Fly). We *do* keep the
  healthcheck as the outer backstop (see §Composition) and may additionally
  surface staleness there, but the watchdog owns the decision.
- **Restart only the director _session_ in place** (kill+respawn the `claude`
  subprocess from within the manager) — the wedge lives in the manager's
  asyncio loop; an in-process actor mid-`await` cannot reliably re-`start()` the
  session. Restarting the whole manager process is the robust, cause-agnostic
  reset.

### 2. Health payload: expose the director's progress signal

`manager_health.py` currently returns `{status, pid, project, sessions:[{name,
role, status}]}` — no `last_activity`. The watchdog needs the director's
`last_activity` **and** status. Add a top-level `manager` block computed
server-side (so the watchdog needs no clock-skew handling — same machine, but
returning a server-derived `idle_seconds` keeps the watchdog dumb):

```json
{
  "status": "ok",
  "pid": 11008,
  "project": "project",
  "manager": {
    "session": "moda-manager-project",
    "status": "running",
    "last_activity": 1782319929.6,
    "idle_seconds": 742.1
  },
  "sessions": [ … unchanged … ]
}
```

- The director session is the entry-point session, named by
  `_manager_session_name()` (`cli.py:886` → `moda-<role>-<project>`). The health
  server already knows the project; pass the manager session name into
  `manager_health.start(...)` from `cli.py` so the payload can pick it out of
  the registry (`get_registry().get(name)`), rather than guessing from
  `sessions`.
- `idle_seconds = now − last_activity`, computed in the handler.
- Backward-compatible: purely additive keys; `healthcheck.sh` and any existing
  consumer keep working.

### 3. Thresholds, retry, backoff (loud, bounded, no silent park)

Defaults (all overridable via env / `agent.yaml`; see D2):

| Knob | Default | Rationale |
|------|---------|-----------|
| `WATCHDOG_POLL_INTERVAL` | `30s` | Cheap localhost GET; 30s bounds detection latency without busy-looping. |
| `WATCHDOG_STALL_THRESHOLD` | `600s` (10 min) | A *single turn* (even a long tool-running one) that has made **zero** registry progress for 10 min while `status` is active is pathological — real turns bump `last_activity` via `record_cost`/`update` far more often. Generous enough to never trip a slow-but-live turn. |
| `WATCHDOG_CONFIRM_POLLS` | `2` | Require N consecutive stalled reads before acting — debounces a single slow sample or a `last_activity` write racing the poll. |
| `WATCHDOG_MAX_RESTARTS` | `3` per `WATCHDOG_RESTART_WINDOW` (`1800s`) | Bound restarts so a genuinely-broken build can't restart-loop silently. **One shared budget for _both_ restart paths** (wedge-restart and crash-relaunch — see §6). |
| `WATCHDOG_BACKOFF` | `30s → 60s → 120s` | Space restarts; give a restart time to settle before re-arming. Applies to crash-relaunch too, so a fast crash cannot tight-loop. |
| `WATCHDOG_MIN_HEALTHY_UPTIME` | `120s` | A child that exits/wedges **before** reaching this uptime is a *fast crash* — counted to the budget immediately (no "the build was basically working" credit). See §6. |

Behavior:

- Every decision is **loud** (`log.warning`/`log.error` to stdout, which is the
  container log): the stalled read, the restart attempt, the outcome.
- On each confirmed wedge within budget → restart (SIGTERM→grace→SIGKILL→
  relaunch), then back off before re-arming.
- On **budget exhaustion** → log `error`, exit the supervisor non-zero → Fly
  machine restart policy takes over. **Never** park silently waiting forever.

### 4. Composition with the existing recovery layers

```
Fly Machines init (machine restart policy)        ← outermost backstop
  └─ bobi supervise (NEW: watchdog)          ← restarts the DIRECTOR
       └─ bobi start (manager process)
            └─ director session (claude subprocess)
                 └─ stall-recovery (director→ENGINEER)   ← restarts engineers
```

- **stall-recovery is untouched.** It still runs *inside* the director and
  recovers stalled **engineer** sessions. The watchdog handles the one case it
  structurally cannot: the **director itself**. The two are complementary and
  non-overlapping — the watchdog only ever judges the entry-point session; it
  never touches engineer sessions (those are stall-recovery's domain).
- **#456 is untouched.** #456 bounds the *known* rotation reconnect so it
  recovers *without* a full restart (cheaper, preserves context). The watchdog
  is the backstop for when bounding fails or for an *unknown* hang. Belt and
  suspenders, by design.
- **The Fly healthcheck stays** as the outer process-death backstop. Note the
  two restart authorities can momentarily race — during the watchdog's
  kill→relaunch window `/health` is briefly down, which the Fly probe *could*
  catch and escalate to a machine restart. That is benign (a machine restart is
  a superset reset), but the entrypoint smoke test must confirm a fast
  in-container restart settles inside the healthcheck grace so it does not
  *always* escalate.

### 5. Safety: fail-open, and honest residual gaps

**Fail-open is the prime directive.** A watchdog that false-kills can crash-loop
the entire fleet, so **uncertainty must never trigger a restart** — only a
*positive* wedge signal does. Concretely:

- If the port file never appears, or `/health` is unparseable, or the `manager`
  block is missing/`status` unknown → the watchdog **logs loudly and keeps the
  manager running**. It degrades to a plain pass-through supervisor; it does not
  guess.
- The connection-failure→restart path (§B.3) requires **N consecutive** hard
  failures *and* the child process still being alive (a clean child exit is a
  crash-relaunch, not a wedge-kill), so a single dropped probe never acts.
- **Post-restart settling.** After any restart the watchdog re-reads the port
  file (the fresh manager binds a *new* ephemeral port and rewrites it), waits
  for `/health` to return before resuming evaluation, and **resets the confirm
  counter**. The kill→rebind window is never counted as a new wedge.

**Honest residual gap (covered vs not).** The active-state discriminator catches
any wedge that leaves the director **mid-turn** (`status ∈ {starting,
running}`) — which is the known mechanism #3 and the overwhelming majority of
plausible hangs, since hangs happen *while doing work*. A hypothetical wedge
that froze the loop while `status="idle"` (e.g. stuck *between* `inbox.recv`
returning and `_drain_turn` setting `running`) would be **missed**. Closing that
needs a *pending-work* signal — inbox queue depth / oldest-undelivered-event age
in the payload, restart when work is queued but unconsumed past a threshold.
That is **deferred to v2** (D6) to keep v1 small and false-kill-proof; the
`inbox.recv` timeout (`session.py:372`) makes a pure idle-loop hang unlikely
today. Flag for Zach if he wants it in v1.

### 6. Crash-loop containment (answering Zach's review)

> *"Do we need protection against crash restart loops? Or is it because the
> length is long enough (10+ minutes) crash restarting is acceptable? … If we
> do detect crash/restart, it's not clear what we would do about it."* — R1
> review, [comment 4792397284](https://github.com/moda-labs/bobi-agent-team/pull/476#issuecomment-4792397284)

Good catch — these are **two distinct restart paths**, and only one of them is
gated by the 10-minute stall threshold. The 10-min length makes a *wedge* loop
benign; it does **not** protect the *crash* path. So yes, we need explicit
crash-loop protection, and it's a small addition rather than a redesign:

| Path | Trigger | Time gate | Loop risk |
|------|---------|-----------|-----------|
| **Wedge-restart** (§3, §B.4) | active-state + `idle_seconds > 600s` | **10 min** per restart | **Low** — even a pathological loop restarts at most ~once / (10 min + backoff). The threshold *is* the protection here, so your second guess is right for this path. |
| **Crash-relaunch** (§B.5) | manager child exits on its own | **none** — fires on child death | **High** — a manager that dies at boot (bad config, import error, unbound port) could be relaunched in a tight loop within seconds. |

**So the answer to "do we need protection": yes, for the crash path — the
stall threshold doesn't cover it.** Three mechanisms, all already cheap given
the supervisor design:

1. **Shared bounded budget.** `WATCHDOG_MAX_RESTARTS` (3 / 1800s) counts **both**
   paths against one counter. A crash loop burns the budget just like a wedge
   loop — it cannot get unlimited free relaunches.
2. **Backoff on the crash path too.** Crash-relaunch obeys the same
   `WATCHDOG_BACKOFF` (`30s → 60s → 120s`), so even within budget a fast crash
   can't hammer-relaunch — minimum ~30s between attempts. This is the bit the
   original §B.5 left implicit; it's now explicit.
3. **Fast-crash detection (`WATCHDOG_MIN_HEALTHY_UPTIME`, 120s).** A child that
   exits before it has been *healthy* (reached addressable / `/health` served)
   for 120s is classified a **fast crash** and counted to the budget with **no
   credit** — it never "earned" a fresh window. A child that ran healthy for
   hours and *then* crashes once is a transient and gets a normal relaunch
   without eating into the loop budget (the window naturally ages out).

**"What do we do about it" — the honest part.** A watchdog **cannot fix** a
crash-looping build; restarting a broken binary just produces the same crash.
So the design's job is explicitly **not** "keep trying" — it is **stop masking
the breakage and escalate**:

- On budget exhaustion (whether from wedges or fast crashes) the supervisor
  logs `error` **loudly** with the restart history and **exits non-zero**.
- A non-zero supervisor exit → **Fly machine restart policy** takes over. Fly
  applies its *own* machine-level restart backoff, so the outer loop is slow and
  Fly's crash-loop guard (`max_restarts` / exponential backoff on the machine)
  becomes the final containment — a persistently broken build ends up *parked by
  Fly*, visibly down, rather than silently thrashing.
- **Residual, stated honestly:** the in-container budget window resets per
  container life, so each Fly machine restart hands the fresh supervisor a fresh
  budget. Ultimate containment of a *persistent* crash loop is therefore Fly's
  machine-restart backoff **plus a human** reading the loud logs — which is
  correct, because the only real fix for a crash-looping build is a human
  rollback/redeploy, not another restart. The optional D7 "announce a restart to
  humans" follow-up is the natural place to make that escalation active
  (post to Slack on budget exhaustion) rather than log-only; **recommend pulling
  the budget-exhaustion announcement into v1** on the strength of this review
  (see D7).

Net: the threshold protects the wedge path (your instinct was right there); the
shared budget + crash-path backoff + fast-crash detection protect the crash
path; and on exhaustion we deliberately escalate to Fly + a human instead of
restarting into the same wall.

---

## Open decisions (recommendations inline — for Zach)

- **D1 — Restart authority.** **(A) `bobi supervise` parent (recommended)**
  vs (B) daemon-thread watchdog inside the manager vs (C) healthcheck-only.
  Rec A: genuinely below the app, locally testable, clean restart-to-addressable.
- **D2 — Config surface.** Env vars (recommended, matches `BOBI_*`
  entrypoint knobs) vs `agent.yaml` block vs both. Rec: env vars with
  documented defaults; no per-team config needed for v1.
- **D3 — Threshold value.** `600s` stall / `30s` poll / `2` confirm
  (recommended) vs tighter. Rec as above — bias to **never** false-kill a live
  long turn; detection latency of ≤~11 min is far better than today's "until a
  human notices."
- **D4 — Restart granularity.** Restart the **manager process** (recommended)
  vs attempt session-only restart. Rec: whole process — cause-agnostic, robust.
- **D5 — Scope of the `manager` health block.** Expose only the entry-point
  session (recommended) vs all sessions' `last_activity`. Rec: entry-point only;
  engineer staleness is stall-recovery's concern, not the watchdog's.
- **D6 — Cover `status="idle"` wedges in v1?** No (recommended) — defer the
  pending-work signal to v2 (see §5). Yes only if Zach wants belt-and-suspenders
  now; it adds inbox-queue-depth plumbing to the payload.
- **D7 — Announce a restart to humans?** Two sub-questions after the R1 review:
  - *Routine single restarts* — out of scope for v1 core (recommended):
    stdout/Fly-log only. Optional follow-up — the watchdog drops a marker the
    rebooted director reads to self-announce in Slack ("recovered from a wedge
    at T"), so a human learns *after* recovery instead of *instead of* it.
  - *Budget exhaustion / crash-loop* — **recommend pulling into v1** (changed on
    the strength of the crash-loop review, §6): when the bounded budget is
    exhausted, post a loud Slack message *before* the non-zero exit, so the
    escalation is active rather than buried in Fly logs. This is the concrete
    "what we do about a detected crash loop." Cheap (one message on the
    already-loud error path) and directly answers the reviewer's "it's not clear
    what we'd do about it."
- **D8 — Crash-loop containment shape (NEW, from R1 review).** Shared budget +
  crash-path backoff + `WATCHDOG_MIN_HEALTHY_UPTIME` fast-crash detection
  (recommended, §6) vs a separate crash-only counter vs rely on Fly's
  machine-level backoff alone. Rec: the shared-budget approach — one mental model
  for both loop types, and Fly stays the *outer* backstop rather than the only
  one.

---

## Scope

### In scope

- Additive `manager` block (`session`, `status`, `last_activity`,
  `idle_seconds`) in the `/health` payload; thread the manager session name in
  from `cli.py`.
- New `bobi/watchdog.py` + `bobi supervise` CLI command: spawn-manage
  the manager child, poll health, apply the active-state+stall discriminator,
  bounded restart with backoff and loud logging, SIGTERM propagation, non-zero
  exit on budget exhaustion. Includes **crash-loop containment** (§6): the
  crash-relaunch path shares the bounded budget + backoff and uses
  `WATCHDOG_MIN_HEALTHY_UPTIME` fast-crash classification.
- Entrypoint switch: `exec … bobi supervise -- --foreground "$@"`.
- Tests: the acceptance integration test + unit tests for the discriminator,
  the bounded-retry/backoff state machine, and the payload.
- Docs: a short section in `DESIGN.md`/`CLAUDE.md` on the recovery layering.

### Out of scope

- **No** change to `stall-recovery` (director→engineer) — untouched.
- **No** change to #456's rotation bounding — orthogonal; this is the backstop.
- **No** new metrics/alerting backend. Loud stdout logs only (Fly log drain
  already captures them); a metrics emit can be a follow-up.
- **No** restart of arbitrary non-director sessions — engineers stay with
  stall-recovery.
- **No** distributed/multi-machine coordination — one supervisor per container.
- **No** `VERSION`/`CHANGELOG.md` bump in this PR (bobi release policy —
  versioning happens at release time only).

---

## Technical Approach

### A. Health payload (`bobi/manager_health.py`, `bobi/cli.py`)

1. `manager_health.start(state_dir, project_name, manager_session=None,
   session_status_fn=None)` — accept the entry-point session name. `cli.py`
   already computes `session_name = _manager_session_name(...)` (`cli.py:279`);
   pass it into the `start(...)` call (`cli.py:261`).
2. In the handler, build the `manager` block from
   `get_registry().get(manager_session)`: `status`, `last_activity`, and
   `idle_seconds = now − last_activity`. Guard for a missing/None entry
   (pre-spawn window → `status:"starting"`, `idle_seconds:0`).
3. Keep `sessions` unchanged for backward compatibility.

### B. Watchdog / supervisor (`bobi/watchdog.py`, `bobi/cli.py`)

1. `bobi supervise -- <start-args>`: `subprocess.Popen` the manager
   (`bobi start <start-args>`), inheriting stdio (logs flow straight to the
   container log). The forwarded args **must** include `--foreground` (the
   entrypoint already passes it) so the manager stays a supervisable child and
   does **not** daemonize out from under the supervisor.
2. Discover the health port via `state/manager-health.port` (same mechanism as
   `healthcheck.sh`); retry briefly on boot until the port file appears.
3. Poll loop every `WATCHDOG_POLL_INTERVAL`:
   - `health()` GET `/health` (reuse `manager_health.health()`); on **connection
     failure** N times in a row, treat as process-level wedge → restart (covers
     the case where even the daemon-thread server dies).
   - Read `manager.status` + `manager.idle_seconds`.
   - **Wedge iff** `status ∈ {starting, running}` **and**
     `idle_seconds > WATCHDOG_STALL_THRESHOLD` for `WATCHDOG_CONFIRM_POLLS`
     consecutive reads.
4. Restart: `SIGTERM` child → wait grace (e.g. 10s) → `SIGKILL` if alive →
   relaunch. Increment the windowed restart counter; apply backoff.
5. If the manager child exits on its own (crash), relaunch it too (the
   supervisor doubles as a crash-restarter) — but through the **same** bounded
   budget **and** backoff as the wedge path, with fast-crash classification
   (§6): a child that exits before `WATCHDOG_MIN_HEALTHY_UPTIME` of healthy
   uptime counts to the loop budget; one that ran healthy and then crashed gets
   a normal relaunch. This is what stops a boot-crashing build from
   tight-looping.
6. On `SIGTERM` to the supervisor: forward to child, wait, exit 0.
7. On restart-budget exhaustion: `log.error(...)`, exit non-zero.

### C. Entrypoint (`docker/docker-entrypoint.sh`)

Change the final `exec` to launch the supervisor as PID 1:
`exec gosu … bobi supervise -- --foreground "$@"`. Health/`healthcheck.sh`
unaffected (still reads the port file, written by the manager child).

---

## Verification Plan

### Acceptance integration test (the issue's required test)

`tests/test_watchdog_restart.py` — **real processes, no MagicMock** (the #454
lesson, reinforced by Zach's R5):

1. Start `bobi supervise` against a **stub manager** — a tiny real process
   that starts the health server (`manager_health.start`) and registers an
   entry-point session whose `last_activity` is **frozen** while
   `status="running"` (simulating a wedge inside a turn).
2. Assert the watchdog: (a) detects the stall after threshold+confirm, (b)
   **restarts** the stub (PID changes / a restart sentinel is written), (c) the
   relaunched manager's entry-point session returns to **addressable**
   (registry shows it active again / `status="idle"`), all within a bounded
   wall-clock budget (use small test thresholds, e.g. `STALL_THRESHOLD=2s`,
   `POLL=0.5s`).

### Negative test (the trap — must NOT restart)

3. Same harness, but `status="idle"` with a frozen `last_activity` (healthy idle
   director). Assert the watchdog **does not** restart across several thresholds
   — proves the active-state discriminator prevents false kills.

### Unit tests

4. Discriminator: table-driven over `(status, idle_seconds)` → expected
   wedge/no-wedge.
5. Bounded retry/backoff: drive the restart state machine; assert ≤
   `MAX_RESTARTS` per window, correct backoff sequence, and **non-zero exit**
   on budget exhaustion (no silent park).
6. Health payload: `manager` block present, `idle_seconds` derived correctly,
   `sessions` unchanged, missing-entry guard.
7. Connection-failure path: N failed `/health` probes → restart.
8. **Crash-loop containment** (§6, from the R1 review): a stub manager that
   **exits immediately** on every launch (fast crash, never reaches
   `MIN_HEALTHY_UPTIME`). Assert the supervisor (a) relaunches with backoff (not
   a tight loop — successive launches are spaced), (b) counts each fast crash to
   the shared budget, (c) exits **non-zero** after `MAX_RESTARTS`, and (d) does
   **not** loop forever. Complement: a stub that runs healthy past
   `MIN_HEALTHY_UPTIME` then crashes once is relaunched **without** tripping the
   loop budget.

---

## Implementation Plan

1. **Health payload** (§A) + unit test 6. Smallest, unblocks the watchdog.
2. **Watchdog discriminator + payload client** (`watchdog.py` core) + unit
   tests 4, 7.
3. **Supervisor process management** (spawn/term/relaunch/backoff/exit) +
   unit test 5.
4. **`bobi supervise` CLI** wiring.
5. **Acceptance + negative integration tests** (§Verification 1–3).
6. **Entrypoint switch** (§C) + a smoke check that the container still boots and
   `healthcheck.sh` passes.
7. **Docs**: recovery-layering note in `DESIGN.md`/`CLAUDE.md`.

Gate: **no implementation until Zach's formal approval on this PR.**

---

## Related

- **#456 / PR #460** (`docs/specs/456-policy-curator.md`) — bounds the *known*
  rotation reconnect (mechanism #3) and explicitly defers this watchdog by name
  in its *Out of scope* / *Related* sections. This spec is the deferred
  follow-up; the two compose (bound-the-known + backstop-the-unknown).
- **Zach's R5 review of #460**, comment
  [4785980738](https://github.com/moda-labs/bobi-agent-team/pull/460#issuecomment-4785980738),
  item #4 — origin of this ticket as a separate defense-in-depth follow-up.
- **#454** — rotation metric over-count (mechanism #1); orthogonal.
- **#443** (`session.py:393`) — turn-level API-error clearing; orthogonal to the
  never-receives-a-message hang this backstops.
- **stall-recovery** (director→engineer) — the sibling layer this completes:
  same idea (restart a stalled session) applied to the one session
  stall-recovery cannot reach, the director.

---
