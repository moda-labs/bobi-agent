# Dead event-transport liveness backstop

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#837 · **Created:** 2026-07-23 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

A production director (moda-baohua) went deaf to its event stream for ~6 hours
and only recovered on a manual Fly restart, while `/health` reported `ok` and
the session `idle` the entire time. This initiative turns that class of outage
into a sub-minute *automatic* recovery and makes "looks-dead-but-reports-healthy"
states visible on every operator surface (health, heartbeat, dashboard, alert).

No 10x expansion is taken: brain **failover** on rate-cap (subscription → API
key) is deliberately out of scope (surface-only for now), and the "pongs-alive
but delivery-wedged" second failure variant is not engineered against until
telemetry proves it real — both rejected here as gold-plating, see Solution.

## Problem

Verified against the working tree 2026-07-23:

- **The discriminator exists but is invisible.** `bobi/events/client.py` already
  computes `is_live()` (:248 — "receive path verified live, not merely
  connected"), `seconds_since_pong()` (:242), and `_deaf_reconnects` (:201).
  None of these reach `/health`, the registry, or any operator surface.
- **The in-client self-heal is the only line of defense.** `_heartbeat`
  (`client.py:259-287`) pings every `_HEARTBEAT_INTERVAL_S=30`s and
  force-reconnects when no pong arrives within `_HEARTBEAT_TIMEOUT_S=95`s while
  the socket still claims connected (#425). When that reconnect fails to restore
  delivery — as it did for baohua for 6h — **nothing escalates**. There is no
  higher-level backstop.
- **Health can't tell wedged from idle.** `bobi/manager_health.py`
  `_manager_block_from_registry` (:89) derives the health `manager` block purely
  from the `SessionEntry` registry record — `status`, `last_activity`,
  server-derived `idle_seconds` (:116). A deaf director (events undelivered) and
  a quiet director (nothing to deliver) both present as `idle`. The block carries
  no transport-liveness field. `manager_health.py:57` states the design intent
  outright: this block is "the input the supervisor sidecar needs to tell a
  wedged director apart from a healthy idle one" — but the discriminator was
  never wired in.
- **The supervisor is downstream and blind.** The restart actor
  (`WATCHDOG_MACHINE_RESTART_CAP`, the machine-restart machinery) lives in the
  private `bobi-deploy` supervisor sidecar — it does **not** exist in this repo
  (verified: no `WATCHDOG_MACHINE_RESTART_CAP`, no Fly-restart code under
  `bobi/`). It deliberately will not restart on `idle_seconds` alone (a
  long-quiet director is legitimate), so absent a transport-liveness signal it
  cannot fire on a wedged-but-idle director.
- **A rate-capped turn looks like a normal answer.** The subscription
  "session limit · resets <time>" banner arrives as ordinary assistant text.
  Today `bobi/transient.py` (`is_transient_api_error`, markers at :29-33) and
  `bobi/session.py` (`_is_transient_turn_error`, :376) sniff that phrasing
  *only* to decide retry — never to surface the cap. A `wait:true` sender gets
  the raw banner or silence, and the cap is invisible on every health surface.

## Solution

Add one transport-liveness signal, source it from the code that already knows,
carry it on the registry, and wire it into both **recovery** (the private
supervisor) and **surfacing** (health/dashboard/alert). Shape is additive —
new fields behind existing seams, no load-bearing rewrite.

1. **Liveness → registry → health `condition` (bobi-agent).** The event client's
   liveness getters are already public; the seam is a periodic write of
   `seconds_since_pong` / `deaf_reconnects` / `is_live` into the director's
   `SessionEntry` via `SessionRegistry.update` (`sdk.py:324`). New `SessionEntry`
   fields are additive and `from_dict` (`sdk.py:269`) already drops unknown keys,
   so old state files stay readable. `_manager_block_from_registry` folds them
   into a **`condition`**: `ok` when the receive path is live, `not_draining`
   (a.k.a. `degraded`) when a stale pong with a still-connected socket says the
   transport is deaf — distinct from `idle`, which stays what it is.
2. **Supervisor backstop restart (bobi-deploy, cross-repo lane).** The private
   supervisor consumes the new `condition`; when `not_draining` persists past a
   threshold (chosen so the in-client 95s self-heal gets first crack — see
   Decision), it restarts, bounded by the existing `WATCHDOG_MACHINE_RESTART_CAP`.
3. **Surface it (bobi-agent).** `bobi/webapp/runtime.py` `health_summary` emits
   the `condition` in the manager block; `bobi/webapp/static/views/agent.js`
   renders a distinct chip + needs-attention entry; an ops alert fires — so a
   stall is never invisible again.
4. **Rate-limit enrichment (bobi-agent).** A pure classifier parses the
   "session limit · resets <time>" banner into the same `condition` (with a
   `reset_at`); on a `wait:true` inbound the sender gets an explicit
   "rate-limited until HH:MM UTC" reply instead of the raw banner or silence.
5. **Deaf-reconnect telemetry (bobi-agent).** Structured logging of
   deaf-reconnect events (via the existing `on_deaf_reconnect` hook,
   `client.py:206`; wired for subagents at `subagent.py:1432`) plus a periodic
   pong-age line, emitted to the fleet event stream so the *next* occurrence is
   diagnosable from durable logs rather than lost stdout — and so we can finally
   tell a client-side self-heal gap from a server-side DO wedge. Mechanism-
   independent; does not block the backstop.

**Alternatives considered:**
- *Server-draining signal* (bobi-events Worker stamps its high-water seq on the
  pong reply, so the client can flag a non-advancing cursor against server
  backlog). Rejected for this initiative — the client knows only its own cursor
  (`_max_enqueued_seq`, `client.py:225`), so this needs a third repo + a
  wire-protocol change, and pong-staleness alone already catches the observed
  incident (both directions dead → ping never round-trips → no pong → `is_live`
  False). Kept as a deferred follow-up *if* telemetry (step 5) shows a
  pongs-alive/delivery-dead variant. *(Decision Q-discriminator, 2026-07-23.)*
- *Split into a public plan + a standalone private issue.* Rejected — the
  backstop's entire value is the end-to-end path (stall → health flips →
  supervisor restarts → delivery resumes), which no single-repo unit can prove.
  One cross-repo plan with a convergence gate keeps the initiative coherent.
  *(Decision Q-topology, 2026-07-23.)*

## Relevant files

### Existing (verified 2026-07-23)

- `bobi/events/client.py` — already exposes `is_live()` (:248),
  `seconds_since_pong()` (:242), `_deaf_reconnects` (:201), and the
  `on_deaf_reconnect` hook (:206). Source of both the liveness signal (Phase 1)
  and the telemetry (Phase 4).
- `bobi/sdk.py` — `SessionEntry` (:229) gains the additive liveness fields;
  `SessionRegistry.update` (:324) is the write path; `from_dict` (:269) already
  tolerates new keys (backward-compatible).
- `bobi/manager_health.py` — `_manager_block_from_registry` (:89) folds the
  liveness fields into the health `manager` block as `condition`; `_health_body`
  (:50) and `_ready_body` (:66) already read the manager block.
- `bobi/transient.py` / `bobi/session.py` — current rate-cap classification is
  retry-only (`transient.py:29-33`, `session.py:376`); extend to also surface
  the cap + `reset_at` (Phase 3).
- `bobi/webapp/runtime.py` — `health_summary` manager block (the documented
  shape at :158) renders `condition` (Phase 2).
- `bobi/webapp/static/views/agent.js` — dashboard chip + needs-attention entry
  (Phase 2).
- `bobi/subagent.py` — `EventServerClient` construction + `on_deaf_reconnect`
  wiring (:1176, :1432) — reference for the manager's own main-subscription
  client seam.

### New

- A pure rate-limit-banner classifier (small; may live in `bobi/transient.py`
  beside the existing markers or as a new `bobi/ratelimit.py` — builder's call,
  additive either way). Pure so any path can call it and it unit-tests trivially.
- (private, `bobi-deploy`) supervisor logic that consumes `condition` and
  restarts past threshold — lands in that repo, not here.

## Questionables

- **Q (restart granularity):** When `not_draining` persists, does the supervisor
  restart the **director session/process** in place (lighter; keeps the
  machine's other state) or the **whole Fly machine** (what the human did at
  16:00Z; reuses the existing `WATCHDOG_MACHINE_RESTART_CAP` machinery)?
  Options: (a) session/process restart first, escalate to machine restart only
  if delivery does not resume; (b) machine restart outright. Recommendation: (a)
  — lighter and faster — but this is a **private-lane** decision gated on the
  supervisor's *actual* current capability (session-level restart may not be
  wired in the sidecar today), so the bobi-deploy lane builder confirms against
  the real supervisor code before committing. Left open: it is the one fork whose
  answer depends on code not in this repo.

## Phases

*(Phases 1–4 are the bobi-agent lane; Phase 5 is the cross-repo bobi-deploy lane.
Phases are in-lane checkpoints, not PR boundaries.)*

### Phase 1 — Transport-liveness signal in the health block

- [ ] Add additive liveness fields to `SessionEntry` (`sdk.py`) — the last
      `seconds_since_pong`, `deaf_reconnects`, and a derived `is_live`.
- [ ] Pin the director's main-subscription `EventServerClient` write site and
      have it write those getters into its own `SessionEntry` on a cadence
      (via `SessionRegistry.update`); confirm the exact site during this phase.
- [ ] Fold the fields into a `condition` (`ok` | `not_draining`) in
      `_manager_block_from_registry`, kept orthogonal to `idle_seconds`.

**Validation gate**

- [ ] `pytest tests/test_manager_health.py -q` (or the nearest existing suite) —
      new unit tests, written failing-first: stale pong + connected socket ⇒
      `condition == not_draining`; recent pong ⇒ `ok`; a genuinely-idle director
      (recent pong, no events) ⇒ `ok`/`idle`, **never** `not_draining`.
- [ ] `GET /health` on a locally-run director shows the `condition` field in the
      manager block.

### Phase 2 — Dashboard + alert surfacing

- [ ] `webapp/runtime.py` `health_summary` emits `condition` in the manager block.
- [ ] `agent.js` renders a distinct chip + a needs-attention entry for
      `not_draining`.
- [ ] An ops alert fires on the condition (same path as existing operator alerts).

**Validation gate**

- [ ] Frontend QA per `docs/FRONTEND_QA.md` — the chip + needs-attention render
      for a `not_draining` team and do not render for a healthy one.
- [ ] `pytest tests/` webapp/runtime unit coverage stays green.

### Phase 3 — Rate-limit banner detection + explicit reply

- [ ] Pure classifier parses "session limit · resets <time>" → `condition`
      (with `reset_at`); reuse/extend the existing markers rather than forking a
      second copy of the transient-text logic.
- [ ] On a `wait:true` inbound while capped, reply "rate-limited until HH:MM UTC"
      instead of the raw banner or silence; surface the cap + `reset_at` on the
      same health/dashboard surfaces as Phase 1–2.

**Validation gate**

- [ ] `pytest` new unit tests, failing-first: banner text → parsed `reset_at`;
      an ordinary successful answer → no cap condition; a genuine 429 status →
      classified as before (no regression to retry behavior).
- [ ] The `wait:true` explicit-reply path is asserted in a session-level test.

### Phase 4 — Deaf-reconnect telemetry

- [ ] Emit a structured event on every deaf reconnect (via `on_deaf_reconnect`)
      + a periodic pong-age line, to the fleet event stream (durable, not stdout).

**Validation gate**

- [ ] `pytest` — a simulated deaf reconnect emits the telemetry event with
      `seconds_since_pong` + `deaf_reconnects`.

### Phase 5 — Supervisor backstop restart (cross-repo: bobi-deploy)

- [ ] The private supervisor consumes `condition == not_draining` from the
      health block; when it persists past the threshold (see Decision), it
      restarts, bounded by `WATCHDOG_MACHINE_RESTART_CAP`.
- [ ] Resolve Q (restart granularity) against the real supervisor capability.

**Validation gate**

- [ ] bobi-deploy supervisor test: sustained `not_draining` → restart issued,
      cap respected; a genuinely-idle director (`condition == ok`) → **no**
      restart.

## Proof of work

- **Failing-test-first for the bugs:** the liveness discriminator (Phase 1) and
  the rate-limit classifier (Phase 3) each land a failing unit test first.
- **Named suites stay green:** `pytest tests/ --ignore=tests/integration/
  --ignore=tests/e2e/ --timeout=30 -q` (unit) and the webapp/runtime coverage;
  frontend QA per `docs/FRONTEND_QA.md` for Phase 2.
- **E2E judgement call (per CLAUDE.md):** this is a brain-agnostic
  process-lifecycle / transport / health change — the risk is **not** in the
  brain path — so the bar is a **stub-brain e2e**, no real-Claude leg. The stub
  e2e simulates a stalled transport and asserts the health `condition` flips to
  `not_draining` (bobi-agent lane). The full backstop (health flips → supervisor
  restarts → delivery resumes) is proven by the convergence gate once the
  bobi-deploy lane lands.

## Lane map

{Filled by the Split workflow. Intended decomposition (for Split):

- **Lane A — bobi-agent** (Phases 1–4 + the stub e2e). Marker mode: `solo`.
  Locks the health-block `condition` contract (field name, values, shape) and
  posts it on Lane B's dispatch issue before Lane A's PR opens (interface-lock
  relay).
- **Lane B — bobi-deploy** (Phase 5). Cross-repo → marker mode `concurrent`.
  Builds against Lane A's locked `condition` post; **lands after** Lane A (it
  consumes a signal Lane A must ship first). Separate repo forces a separate PR.

Same-repo parallelism is **not** used — Lane B is a different repo, and Phases
1–4 are one coherent bobi-agent unit, so the bobi-agent side is a single lane.}

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| A | #TBD | 1–4 | health signal + condition + dashboard/alert + rate-limit + telemetry | solo | open |
| B | #TBD (bobi-deploy) | 5 | supervisor consumes condition → threshold restart | concurrent | open |

- [ ] **Convergence gate** (run by the session that lands the *last* lane):
      inject a stalled transport → assert health `condition` flips to
      `not_draining` → supervisor restarts within threshold (cap respected) →
      delivery resumes. *Fuse-runnable portion:* the health-flip half runs on a
      local merged bobi-agent preview via the stub e2e. *Deferred portion:* the
      supervisor-restarts-and-delivery-resumes half needs the private supervisor
      + a real restart, so it runs on staging post-merge, not in the fuse.

## Amendments

{Append-only. Every post-approval change lands here with a dated entry.}

## Notes

- **Decision (2026-07-23, Zach via /plan):** discriminator = **pong-staleness
  only** (no bobi-events Worker / wire-protocol change). Keeps the initiative to
  two repos. See Solution "Alternatives considered."
- **Decision (2026-07-23, Zach via /plan):** track as **one cross-repo plan**
  (this file), not a public plan + separate private issue — so a single unit
  proves the backstop end-to-end via the convergence gate.
- **Decision (2026-07-23, recommended — confirm in Review):** backstop threshold
  is set **above** the in-client 95s self-heal window (a few minutes) so the
  supervisor fires only on self-heal *failures*, never on the routine
  deaf-reconnects #425 already handles. Exact value tuned in Phase 5.
- **Scope / non-goals (from #837):** brain **failover** (subscription → API key)
  when capped is OUT (surface-only). The **chronic** rate-cap (baohua's director
  hit the cap 6× in 3 days — under-provisioned plan / too-chatty director) is a
  **separate** durable fix (plan bump / throttling), tracked apart; surfacing
  only makes it visible.
- **References:** #425 (receive-side liveness heartbeat, commit `3fe35cc`); #800
  (keep rotation responsive / replay unacked); #826/#827 (registry refresh on
  workflow resume); the `plans/review-remediation.md` dead-transport family
  (session/ack layer — a different seam from this transport/health layer).
