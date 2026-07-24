# Dead event-transport liveness backstop

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#837 · **Created:** 2026-07-23 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

A production director (moda-baohua) went deaf to its event stream for ~6 hours
and only recovered on a manual Fly restart, while `/health` reported `ok` and
the session `idle` the entire time. This initiative makes that class of
"looks-dead-but-reports-healthy" state **visible** on every operator surface
(health, dashboard, alert) and **diagnosable** (durable deaf-reconnect
telemetry) — and lays the validated signal an automatic restart backstop will
later key off.

**v1 is the visibility + telemetry half, deliberately.** The automatic-restart
half (the issue's headline) is written as a gated lane but **does not dispatch**
until a captured recurrence proves a restart is actually curative — because the
in-client reconnect *and* server-side resubscribe already ran for 6h into the
same Durable Object and did **not** recover; only a machine restart did, for
reasons we cannot yet explain. Automating an unproven remedy risks a
turn-shredding restart loop bounded only by the restart cap. Telemetry (Phase 3)
is what closes that knowledge gap, so it leads, not trails.
*(Decision Q-sequencing, 2026-07-23, Zach.)*

No 10x expansion is taken: brain **failover** on rate-cap is out (surface-only),
and the server-side "draining" signal (a bobi-events Worker high-water on the
pong) is rejected — pong-staleness plus deaf-reconnect trend catches the observed
incident without a third repo / wire-protocol change.

## Problem

Verified against the working tree 2026-07-23:

- **The discriminator exists but is invisible.** `bobi/events/client.py` computes
  `is_live()` (:248), `seconds_since_pong()` (:242), and `_deaf_reconnects`
  (:201). None reach the registry, `/health`, or any operator surface.
- **The in-client self-heal is the only defense, and it failed silently.**
  `_heartbeat` (`client.py:259-287`) pings every 30s and force-reconnects when no
  pong arrives within `_HEARTBEAT_TIMEOUT_S=95`s while the socket still claims
  connected (#425). For baohua it force-reconnected for 6h without restoring
  delivery, and **nothing escalated**. There is no higher-level backstop.
- **Pong-age is reset by the very reconnect loop that signals the wedge.**
  `on_open` sets `_last_pong_at = time.monotonic()` (`client.py:387`) and the
  `connected` frame *also* resets it ("Treat the connect frame as a fresh
  liveness proof", `client.py:402-404`). So during a deaf-reconnect loop the
  socket reopens (~every 95s), the connect frame arrives, pong-age snaps back to
  ~0 → `is_live()` reads `True` → any condition derived from pong-age sawtooths
  healthy and **never sustains**. The only monotonic wedge-tracker is
  `_deaf_reconnects` (incremented at `:271`, never reset).
- **`_last_pong_at` is monotonic — unserializable across a read.** It is
  `time.monotonic()` (`:240`), meaningless in another process. The health server
  and the director are different processes (`manager_health.start` in the service
  process, `service.py:585`; the director in a detached subprocess via
  `spawn_adhoc`), communicating only through the on-disk registry JSON. A
  persisted monotonic delta or a persisted `is_live` bool freezes at its
  last-written value — a dead writer would report `ok` forever.
- **The obvious write path silently breaks the existing supervisor signal.**
  `SessionRegistry.update()` hardcodes `data["last_activity"] = time.time()` on
  every call (`sdk.py:345`). Routing a periodic liveness write through it pins
  `last_activity` to "now", so `idle_seconds` (`time.time() - last_activity`,
  `manager_health.py:116`) — documented as "the progress signal the self-heal
  supervisor sidecar observes" (`manager_health.py:163`) — never climbs.
- **Health can't tell wedged from idle.** `_manager_block_from_registry` (:89)
  derives the health `manager` block purely from the registry `SessionEntry`
  (`status`/`last_activity`/`idle_seconds`); a deaf director and a quiet one both
  present as `idle`. `manager_health.py:57` states the design intent — this block
  is "the input the supervisor sidecar needs to tell a wedged director apart from
  a healthy idle one" — but the discriminator was never wired in.
- **There are TWO manager blocks, not one.** The raw container probe
  `GET /health` builds its block via `_manager_block_from_registry`
  (`manager_health.py:89`, shape `{session,status,last_activity,idle_seconds}`) —
  this is what a Fly/container supervisor curls. The dashboard's
  `GET /api/agents/{name}/health` builds a *different* block in
  `LocalRuntime.health_summary` (`webapp/runtime.py:531`, shape
  `{status,pid,running,healthy,restart_count,...}`), directly from the registry,
  not by calling `/health`. They share no code. A hosted `health_summary` impl
  also exists in bobi-deploy (only the ABC + `LocalRuntime` are in this repo).
- **The restart actor is downstream and private.** `WATCHDOG_MACHINE_RESTART_CAP`
  and the machine-restart machinery live in the private `bobi-deploy` supervisor
  sidecar — verified absent from `bobi/`. It deliberately won't restart on
  `idle_seconds` alone, so absent a transport-liveness signal it cannot fire on a
  wedged-but-idle director.
- **A rate-capped turn is invisible, and the banner isn't in the code.** The
  subscription "session limit"/reset banner arrives only as runtime model output;
  a repo-wide grep for that phrasing returns **zero hits** — `transient.py`'s
  markers (`:29-33`) do **not** match it, so today a capped turn is neither
  classified nor surfaced, and a `wait:true` sender gets silence or the raw text.

## Solution

Emit one transport-liveness signal from wall-clock facts, carry it on the
registry without disturbing `idle_seconds`, derive the condition at **read**
time, and surface + diagnose it. The auto-restart consumes the same condition
later, once telemetry proves it curative.

1. **Signal (bobi-agent).** Persist **absolute wall-clock** facts on the
   director's `SessionEntry`: `last_pong_wall` (= `time.time() -
   client.seconds_since_pong()` at write time), `last_event_wall` (wall time of
   the last *real* delivered event), and `deaf_reconnects` (the monotonic int).
   **Never persist `is_live` or a monotonic delta.** Write them from
   `Session._inbox_loop` (`session.py:1351`) — the one always-on loop that ticks
   every ~2s while idle and already holds `get_registry()`, `self.name`, and
   `self._subscription.client` — throttled, null-guarding `self._subscription`
   (async-assigned, None during boot/retry), and only for the manager/director
   session. Use a **`last_activity`-preserving** registry write (a new
   `update_fields`-style method or direct field write), not `update()`.
2. **Condition, derived at read time (bobi-agent).** A shared pure helper
   (`SessionEntry` liveness fields + `time.time()` → `condition`) is called from
   **both** `_manager_block_from_registry` and `LocalRuntime.health_summary` so
   the `/health` probe and the dashboard cannot disagree. Condition is
   `transport_deaf` when the transport is wedged, `ok` otherwise, kept orthogonal
   to `idle`/`idle_seconds`. The exact discriminator is a composite that survives
   every wedge variant and never false-trips on idle/boot — see Questionable
   Q-signal; the rejected naive form is pong-age-alone. Boot / never-ponged /
   missing-entry ⇒ `ok`/`starting`, **never** `transport_deaf` (extend the
   existing fail-open at `manager_health.py:106`).
3. **Surface it (bobi-agent).** The shared helper's `condition` renders in both
   health blocks; `webapp/static/views/agent.js` gets a distinct chip +
   needs-attention entry; an ops alert fires via the existing durable-bus
   pattern (`emit_*_cap_alert` helpers → `post_event`). Update the abstract
   `health_summary` shape docstring (`runtime.py:167-169`) to include `condition`.
4. **Diagnose it (bobi-agent).** Emit deaf-reconnect telemetry at the
   **detection** site (`_heartbeat`, `client.py:271`) — where `seconds_since_pong`
   and `deaf_reconnects` are both in hand — not (only) the `on_deaf_reconnect`
   recovery hook (`client.py:418`), which fires on *successful* resubscribe and
   would miss a non-recovering wedge. Compose with the existing
   `_resubscribe_on_deaf` occupant of that single callback slot
   (`subagent.py:1432`); do not replace it. Publish via `post_event` (durable
   fleet bus), not `_log_event`/`log_activity` (local JSONL). Plus a periodic
   pong-age line. This is what lets us tell a client-side self-heal gap from a
   server-side DO wedge, and unblocks the restart gate.
5. **Rate-limit surfacing (bobi-agent), gated on a real sample.** A pure
   classifier parses the actual subscription session-limit banner into the same
   `condition` (with `reset_at`); on a `wait:true` inbound the sender gets an
   explicit "rate-limited until HH:MM UTC" reply via `Inbox.respond`
   (`session.py:1234`). **Prerequisite:** capture a verbatim real banner sample
   first — it is not in the repo and the glyph/spacing/time format are unverified,
   so a parser + fixture built off a paraphrase is a guess.
6. **Auto-restart (bobi-deploy), dispatch-gated.** The private supervisor
   consumes `condition == transport_deaf`; when it persists past a threshold
   (above the in-client 95s self-heal window) it restarts, bounded by
   `WATCHDOG_MACHINE_RESTART_CAP` **and** a circuit breaker: if a restart does not
   clear `transport_deaf` within N minutes, stop restarting and page a human
   rather than loop to the cap. **Does not dispatch** until Phase 3 telemetry from
   a real recurrence establishes (a) which restart level (session vs machine)
   restores delivery and (b) that the signal is trustworthy.

**Alternatives considered:**
- *Server-draining signal* (bobi-events Worker stamps its high-water seq on the
  pong). Rejected — third repo + wire-protocol change; the client knows only its
  own cursor (`client.py:225`), and pong-staleness + deaf-reconnect trend already
  catch the observed incident. Deferred follow-up if telemetry shows a
  pongs-alive/delivery-dead variant. *(Decision Q-discriminator, 2026-07-23.)*
- *Public plan + standalone private issue.* Rejected — one cross-repo plan with a
  convergence gate keeps the end-to-end backstop coherent.
  *(Decision Q-topology, 2026-07-23.)*
- *Ship the auto-restart in v1 with a breaker.* Rejected for v1 — the restart's
  curative premise is unproven; surface + telemetry first, gate the restarter on
  evidence. *(Decision Q-sequencing, 2026-07-23.)*

## Relevant files

### Existing (verified 2026-07-23)

- `bobi/events/client.py` — liveness getters (`is_live` :248, `seconds_since_pong`
  :242, `_deaf_reconnects` :201); pong-reset sites (`:387`, `:402-404`) that
  disqualify pong-age-alone; deaf **detection** site (`_heartbeat` :271) for
  telemetry; the recovery-only hook (`:418`).
- `bobi/session.py` — `_inbox_loop` (:1351, the ~2s idle-ticking write seam) holds
  `self._subscription.client` (Subscription stored :1504); `Inbox.respond` call
  site (:1234) for the rate-limit reply.
- `bobi/sdk.py` — `SessionEntry` (:229) gains the additive wall-clock liveness
  fields; needs a **`last_activity`-preserving** write variant (today `update()`
  bumps it, :345); `from_dict` (:269) already tolerates new keys.
- `bobi/manager_health.py` — `_manager_block_from_registry` (:89) calls the shared
  condition helper; fail-open precedent at :106.
- `bobi/webapp/runtime.py` — `LocalRuntime.health_summary` (:531) is the *second*
  block that must also call the helper; abstract shape docstring (:167-169) to
  update. Hosted impl is in bobi-deploy.
- `bobi/webapp/static/views/agent.js` — chip + needs-attention (health poll ~:133).
- `bobi/events/publish.py` (`post_event`) + the `emit_*_cap_alert` helpers
  (`concurrency_semaphore.py:99`, `spend_governor.py:92`) — the alert/telemetry
  emit pattern to mirror.
- `bobi/subagent.py` — `_start_event_subscription`/`Subscription` (:1426/:1461),
  the `on_deaf_reconnect=_resubscribe_on_deaf` slot to compose with (:1432).

### New

- A `SessionRegistry` write that does not bump `last_activity` (small addition to
  `sdk.py`).
- A pure `condition` helper (SessionEntry liveness → condition) shared by both
  health blocks (`sdk.py` or a small `bobi/liveness.py`).
- A pure rate-limit-banner classifier (may share `transient.py`'s fingerprint
  constant; distinct return type `reset_at`, so a separate function/module).
- (private, `bobi-deploy`) supervisor logic consuming `condition` — gated lane.

## Questionables

- **Q-signal (discriminator formula):** What signal robustly separates *wedged*
  from *idle* across all wedge variants? A reconnect-loop climbs `_deaf_reconnects`
  while pong-age sawtooths; a dead-heartbeat-thread wedge grows pong-age with
  `_deaf_reconnects` flat. Neither alone is complete, and neither may false-trip
  on a genuinely-idle or booting director. Proposed composite: `transport_deaf`
  when `_deaf_reconnects` rose over the trailing window **OR** wall-clock pong-age
  exceeds a threshold while the socket claims connected, corroborated by
  `last_event_wall`; boot/never-ponged ⇒ `ok`. Recommendation: ship the composite
  in Phase 1 and let Phase 3 telemetry from a real recurrence validate/tune the
  thresholds before the restart gate (Q-sequencing) opens. Left open: whether the
  composite is sufficient or last-real-event age must be primary — telemetry
  decides.
- **Q-restart-granularity (private lane):** session/process restart (lighter) vs
  full Fly machine restart (what the human did; reuses the cap machinery)?
  Recommendation: session first, escalate to machine if delivery doesn't resume —
  but gated on the supervisor's actual capability and on Phase 3 evidence of which
  level is curative. Resolved by the bobi-deploy lane when it ungates.

## Phases

*(Phases 1–5 are the bobi-agent v1 lane. Phase 6 is the gated cross-repo
bobi-deploy lane. Phases are in-lane checkpoints, not PR boundaries.)*

### Phase 1 — Wall-clock liveness signal on the registry

- [ ] Add additive wall-clock fields to `SessionEntry` (`sdk.py`):
      `last_pong_wall`, `last_event_wall`, `deaf_reconnects`. **No `is_live`, no
      monotonic deltas.**
- [ ] Add a `last_activity`-preserving `SessionRegistry` write.
- [ ] From `Session._inbox_loop` (`session.py:1351`), throttled and
      manager-session-only, write the three fields from
      `self._subscription.client` (null-guarded).
- [ ] Shared pure helper: (SessionEntry liveness fields, `now`) → `condition`
      (`transport_deaf` | `ok`), per Q-signal's composite; boot/never-ponged/
      missing-entry ⇒ `ok`/`starting`.

**Validation gate**

- [ ] `pytest tests/test_manager_health.py -q` — new tests, failing-first: a
      sustained deaf-reconnect *loop* (pong-age sawtooths, `deaf_reconnects`
      climbs) ⇒ `transport_deaf`; a dead-writer (fields frozen, wall-clock ages)
      ⇒ `transport_deaf`; a genuinely-idle director (recent pong, no events) ⇒
      `ok`; a booting/never-ponged director ⇒ `ok`/`starting`.
- [ ] A liveness write does **not** change `idle_seconds` (assert `last_activity`
      untouched).

### Phase 2 — Surface the condition on both blocks + alert

- [ ] Call the shared helper from **both** `_manager_block_from_registry`
      (`/health`) and `LocalRuntime.health_summary` (dashboard); update the
      abstract shape docstring (`runtime.py:167-169`).
- [ ] `agent.js` chip + needs-attention entry for `transport_deaf`.
- [ ] Ops alert via the `emit_*_cap_alert`/`post_event` pattern.

**Validation gate**

- [ ] `pytest tests/test_manager_health.py tests/test_webapp_server.py -q` — both
      blocks emit `condition`; existing key assertions still pass.
- [ ] Frontend QA per `docs/FRONTEND_QA.md` — chip + needs-attention render for a
      `transport_deaf` team, absent for a healthy one.

### Phase 3 — Deaf-reconnect telemetry (leads the restart gate)

- [ ] Emit a structured `post_event` at the **detection** site (`_heartbeat`,
      `client.py:271`) carrying `seconds_since_pong` + `deaf_reconnects`, composing
      with (not replacing) `_resubscribe_on_deaf`; plus a periodic pong-age line.

**Validation gate**

- [ ] `pytest` — a simulated deaf **detection** (not just a successful recovery)
      emits the telemetry event to the durable bus with both fields.

### Phase 4 — Rate-limit surfacing + explicit reply (gated on a real banner sample)

- [ ] **Prerequisite:** capture a verbatim real subscription session-limit banner.
- [ ] Pure classifier: banner → `condition` + `reset_at`; may reuse the
      `transient.py` fingerprint constant but is a separate function (distinct
      return type). Must not regress `is_transient_api_error` retry behavior.
- [ ] `wait:true` capped inbound → "rate-limited until HH:MM UTC" via
      `Inbox.respond` (`session.py:1234`); surface `reset_at` on the same blocks.

**Validation gate**

- [ ] `pytest` failing-first, off the **captured** sample: banner → parsed
      `reset_at`; ordinary answer → no cap; a genuine 429 status → unchanged.
- [ ] The `wait:true` reply path asserted in a session-level test.

### Phase 5 — v1 stub e2e (visibility proven end-to-end)

- [ ] Stub-brain e2e: simulate a stalled transport ⇒ assert `condition` flips to
      `transport_deaf` on `/health` **and** the dashboard block, **and** the
      telemetry event is emitted. No restart asserted in v1 (see Phase 6 gate).

**Validation gate**

- [ ] `pytest tests/e2e/…` (stub leg) green; no real-Claude leg (brain-agnostic
      per CLAUDE.md — the risk is transport/health, not the brain path).

### Phase 6 — Supervisor auto-restart (cross-repo: bobi-deploy) — DISPATCH-GATED

**Gate to open this phase:** Phase 3 telemetry from a real recurrence establishes
which restart level restores delivery and that the signal is trustworthy. Until
then this phase does not dispatch.

- [ ] Supervisor consumes `condition == transport_deaf`; sustained past threshold
      (above the 95s self-heal window) → restart, bounded by
      `WATCHDOG_MACHINE_RESTART_CAP` **and** a circuit breaker (restart doesn't
      clear it within N ⇒ stop + page, don't loop).
- [ ] Resolve Q-restart-granularity against real supervisor capability + evidence.

**Validation gate**

- [ ] bobi-deploy supervisor test: sustained `transport_deaf` → restart issued,
      cap + breaker respected; genuinely-idle (`condition == ok`) → no restart;
      restart-doesn't-clear → pages instead of looping.

## Proof of work

- **Failing-test-first for the bugs:** the condition discriminator (Phase 1) and
  the rate-limit classifier (Phase 4, off a captured sample) each land a failing
  unit test first.
- **Named suites stay green:** `pytest tests/ --ignore=tests/integration/
  --ignore=tests/e2e/ --timeout=30 -q`; `tests/test_manager_health.py` **and**
  `tests/test_webapp_server.py` (the two blocks); frontend QA per
  `docs/FRONTEND_QA.md`.
- **E2E judgement call (per CLAUDE.md):** brain-agnostic transport/health change →
  **stub-brain e2e only** (Phase 5), no real-Claude leg. The full backstop
  (condition → supervisor restart → delivery resumes) is proven by the
  convergence gate once Phase 6 ungates.

## Lane map

{Filled by the Split workflow. Intended decomposition:

- **Lane A — bobi-agent, v1** (Phases 1–5). Marker mode `solo`. Locks the exact
  `condition` contract (endpoint = the raw `GET /health` block from
  `manager_health.py`; JSON path; value enum `transport_deaf`|`ok`; `reset_at`
  placement) and posts a concrete JSON example on Lane B's issue before Lane A's
  PR opens (interface-lock relay).
- **Lane B — bobi-deploy, gated** (Phase 6). Cross-repo → marker mode `concurrent`.
  **Does not dispatch** until the Phase 6 gate opens (real-recurrence telemetry).
  Builds against Lane A's locked `/health` contract; lands after Lane A.}

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| A | #TBD | 1–5 | wall-clock signal + condition on both blocks + alert + telemetry + rate-limit + stub e2e | solo | open |
| B | #TBD (bobi-deploy) | 6 | supervisor consumes condition → gated restart w/ breaker | concurrent | gated |

- [ ] **Convergence gate** (once Phase 6 ungates, run by the session landing the
      last lane): inject a stalled transport → `condition` flips `transport_deaf`
      → supervisor restarts within threshold (cap + breaker respected) → delivery
      resumes. *Fuse-runnable:* the condition-flip + telemetry half runs on a local
      merged bobi-agent preview via the stub e2e. *Deferred:* the
      supervisor-restarts-and-resumes half needs the private supervisor + a real
      restart on staging.

## Amendments

{Append-only after approval.}

## Notes

- **Review pass (2026-07-23):** three adversarial lenses (chaos, staff-eng,
  implementer), all findings verified against the tree, reshaped Phase 1's
  mechanism (wall-clock not monotonic; deaf-reconnect-trend not pong-age-alone;
  `_inbox_loop` write seam; `last_activity`-preserving write; shared two-block
  helper), gated Phase 4 on a captured banner, moved telemetry ahead of the
  restart, and renamed `not_draining` → `transport_deaf`.
- **Decision (2026-07-23, Zach):** discriminator = pong-staleness + deaf-reconnect
  trend, **no** bobi-events Worker change (2 repos).
- **Decision (2026-07-23, Zach):** one cross-repo plan, not plan + separate issue.
- **Decision (2026-07-23, Zach):** v1 = surface + telemetry; auto-restart lane is
  written but dispatch-gated on a real recurrence proving restart is curative.
- **Scope / non-goals (from #837):** brain **failover** on cap is OUT
  (surface-only). The **chronic** rate-cap (baohua hit the cap 6× in 3 days) is a
  **separate** durable fix (plan bump / throttling), tracked apart.
- **References:** #425 (receive-side heartbeat, commit `3fe35cc`); #800 (rotation
  responsiveness / replay unacked); #826/#827 (registry refresh on resume);
  `plans/review-remediation.md` dead-transport family (session/ack layer — a
  different seam from this transport/health layer).
