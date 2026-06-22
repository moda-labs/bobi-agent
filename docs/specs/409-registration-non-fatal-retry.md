# Spec — #409: Make event-server registration non-fatal (boot + background retry)

- **Issue:** [moda-labs/modastack#409](https://github.com/moda-labs/modastack/issues/409)
- **Type:** bug (runtime / boot-path; design-sensitive)
- **Status:** DRAFT — held for Zach's approval. No implementation until this spec is approved.
- **Branch:** `spec/409-registration-non-fatal`

> This spec is a superset of the issue body. It restates the diagnosis, then
> specifies the exact behavior change, the worker-side investigation, the
> regression test, and the trade-offs/risks that make this design-sensitive.

---

## 1. Problem

Coordinator sessions (project leads + the manager, plus sub-agents that
subscribe to external topics) **die during initialization** when the
registration handshake with the cloud event server
(`modastack-events.modalabs.workers.dev`) times out.

Observed 2026-06-21/22: two project leads (~22:49, ~01:00) and several
sub-agents (the #329 spec draft-PR attempts) died at init, blocking dispatch.

`.modastack/state/manager.log`:
```
Event server registration failed (attempt 1/3): The read operation timed out — retrying in 2s
Event server registration failed (attempt 2/3): The read operation timed out — retrying in 4s
```
After 3 attempts registration gives up and raises; this surfaces to the agent
runtime as `Control request timeout: initialize` → the session goes to `error`
and dies.

**Nature: intermittent, cloud-side.** Direct probes of the worker are healthy
(5/5 ~40ms), so this is episodic latency / cold-start on the registration path,
not a hard outage — which is why some agents start fine while others die at init.

## 2. Root cause (code path)

Registration is performed **synchronously and treated as fatal** for any
session that subscribes to a topic beyond its own inbox.

1. `Session.start()` calls `_start_subscription()` **before** the session is
   marked ready — `modastack/session.py:528-568`.
2. `_start_subscription()` calls `_start_event_subscription(...)`. For a
   **coordinator** (`has_external = any(not k.startswith("inbox/") ...)`) any
   exception is **re-raised** — `modastack/session.py:493-526` (the
   `if has_external: raise` at line 520). The inline rationale is deliberate:
   *"Don't let a coordinator advertise itself as alive while deaf."*
3. `_start_event_subscription()` runs `_register_with_retry(url, attempts=3)`
   inline — `modastack/subagent.py:896-933`. On exhaustion it raises
   `RuntimeError("Could not register ... after 3 attempts")`.
   - Backoff: `delay = 2 ** (attempt + 1)` → **2s then 4s** (total ~6s of sleep
     plus the HTTP timeouts), then give up.
4. The registering HTTP call `POST /deployments` uses a **15s** read timeout —
   `modastack/events/server.py:181-206` (`_post_register`, `timeout=15.0`); the
   shared client default is `httpx.Timeout(10.0, connect=5.0)` —
   `modastack/http.py:33`.
5. The re-raised exception aborts `Session.start()`; the session never sets
   `_ready` and is marked `error`.

**Why it should not be fatal:** events are per-deployment **queued, sequenced,
and resumable**. The worker buffers events in KV for 48h
(`EVENT_BUFFER_TTL = 48*60*60`, `event-server/src/deployment-session.ts:10`) and
replays from a persisted cursor on reconnect (`last_seen` query param on
`/deployments/{id}/subscribe`, `modastack/events/client.py:280-285`;
`replayEvents()` in `deployment-session.ts:127-138`). A transient handshake
timeout therefore need not be terminal — a late registration can resume the
stream.

## 3. Goals / non-goals

**Goals**
- A transient registration timeout no longer kills a coordinator session.
- The agent boots and retries registration in the background until it succeeds.
- Repeated registration failures are logged (with backoff) but never terminate
  the process.
- Larger read-timeout + more retries + backoff make the *initial* attempt more
  likely to succeed before falling back to background retry.
- Investigate and characterize the worker-side slow path so we know whether to
  also fix the server.
- A regression test proves: simulated registration read-timeout → session
  survives and retries.

**Non-goals**
- No change to event semantics, the cursor/replay protocol, or the bubble
  auth/mint/join flow.
- No change to the inbox-only worker path (already non-fatal — `session.py:523`).
- Not a worker rewrite. Worker work in this ticket is **investigation +
  lightweight instrumentation**; any server-side fix is scoped from findings
  (may spin out a follow-up issue).

## 4. Proposed design

### 4.1 Make registration non-fatal (boot + background retry)

Restructure `_start_event_subscription` so a coordinator can boot **before**
registration succeeds:

- **Try once, fast, inline.** Attempt registration inline with a short, bounded
  budget (a couple of attempts). If it succeeds, behave exactly as today
  (construct `EventServerClient`, start drain) — the happy path is unchanged.
- **On failure, do not raise.** Return a `Subscription` in a
  **pending/unregistered** state and spawn a **background registration thread**
  (daemon) that keeps retrying with capped backoff. The session proceeds to
  ready and is runnable.
- **On eventual success in the background:** save deployment state, construct
  and start the `EventServerClient` + drain loop, and attach them to the live
  `Subscription` so the event stream resumes. Because the client connects with
  the persisted `last_seen` cursor, a registered-then-disconnected deployment
  resumes with **no event loss** (see §7 for the fresh-deployment caveat).
- **Update `session.py:519-526`:** for `has_external`, **stop re-raising** on a
  registration-timeout class of failure. Instead, log and let the background
  retry own reconnection. Genuinely fatal, non-transient errors (e.g. malformed
  config / unrecoverable auth rejection that is not `BubbleRejected`) may still
  surface — see §7 open question O3 on how finely to distinguish.

> Honoring the original "don't advertise alive while deaf" rationale: §4.4 adds
> an explicit **registration status** so a not-yet-registered coordinator is
> visibly `connecting/degraded`, not silently presented as healthy.

### 4.2 Increase read-timeout + backoff + more retries

- Raise the `POST /deployments` read timeout from **15s → 30s** (connect stays
  short, ~5s) — `_post_register`, `server.py:202`. Cold-start latency lives in
  the *read* phase, not connect.
- Inline attempts: keep a small, fast budget (≈2–3) so boot isn't delayed long.
- Background retry: **capped exponential backoff with jitter** (e.g. 2s → 4s →
  8s → … capped at ~60s), retrying for a long horizon (well within the 48h
  event buffer) rather than a fixed 3-strikes-and-die.
- Add jitter to avoid a thundering-herd re-register when many agents recover at
  once (the incident took out multiple sessions simultaneously).
- Exact constants (inline attempt count, cap, total horizon) are listed as
  decision **O1** below for Zach to confirm.

### 4.3 Logging of repeated failures (no termination)

- Log the first background failure at WARNING with the error and next-retry
  delay; throttle subsequent identical failures (e.g. WARNING every Nth attempt
  or on backoff-cap) to avoid log spam, and log at INFO on eventual success
  ("registration recovered after N attempts / Ms").
- Never call anything that terminates the process from the retry path.

### 4.4 Observability — don't be silently deaf

- Add a registration/connection status to the session registry entry
  (`SessionEntry`, set in `Session.start()` — `session.py:544-554`), e.g.
  `event_status: registered | connecting | degraded`.
- Surface it where session health is read (`modastack status` / `modastack
  doctor`) so a coordinator stuck in `connecting` is visible rather than
  masquerading as fully `idle`/healthy. This is the explicit replacement for the
  old fatal-fast behavior.

### 4.5 Worker-side cold-start / slow-path investigation

Goal: determine whether the registration endpoint itself contributes to the
episodic timeouts, and whether a server-side fix is warranted.

`POST /deployments` → `handleRegisterDeployment()`
(`event-server/src/core.ts:518-593`) does, in sequence: optional bubble mint +
`storage.putBubble`, signature verify (JOIN), deployment record + subscription
index writes, and `storage.initDeploymentSession(...)` which **initializes the
Durable Object** for the deployment (`core.ts:582`).

Investigate:
- **Durable Object cold-start**: `initDeploymentSession` / first DO access
  latency, especially when many deployments init concurrently.
- **KV write latency / contention**: bubble put, deployment + subscription
  index, `next_seq` init.
- **Cloudflare limits**: subrequest count, CPU time, and any rate-limiting on
  the worker around the two failure windows (~22:49 and ~01:00).
- Pull Worker logs / analytics (wrangler tail or dashboard) for those windows
  and correlate with the client-side timeouts.

Deliverable: add **server-side timing logs** around mint / verify / DO-init in
`handleRegisterDeployment` so future episodes are diagnosable, and a short
findings note. If a concrete server hotspot is found, file a scoped follow-up
(do not expand this ticket into a worker rewrite).

## 5. Verification plan

- Unit/regression test in §6 (the gating proof).
- Manual: point a session at a stub event server that delays `POST /deployments`
  past the inline budget, confirm the session boots, stays alive, logs retries,
  and connects once the stub responds — with the cursor resuming the stream.
- `pytest tests/ --ignore=tests/integration/` green (~30s); full `pytest tests/`
  before PR.
- Confirm `modastack status`/`doctor` shows the new `connecting`/`degraded`
  state during the outage window.

## 6. Regression test approach

Per CLAUDE.md ("CI failure or production bug = integration test gap. … The test
must fail first, then the fix makes it pass."). Model on the existing mocking
style in `tests/test_bubble.py`, which already monkeypatches `_post_register`.

**Test 1 — registration timeout is non-fatal (the core proof).**
- Monkeypatch the registration call (`register` / `_post_register`, or the
  underlying `pooled.post`) to raise `httpx.ReadTimeout("read operation timed
  out")` for the first *K* attempts, then succeed.
- Start a coordinator-style session (subscribe list includes a non-inbox topic
  so `has_external` is true).
- **Assert (fails on current code, passes after fix):**
  1. `Session.start(...)` returns `True` / the session is **not** marked
     `error` — today it raises and dies.
  2. The background retry eventually registers; the `EventServerClient` gets a
     deployment id + key and starts.
  3. Repeated failures are logged (assert via `caplog`) and the process is
     still alive throughout.

**Test 2 — exhausted inline attempts fall through to background, not death.**
- Make registration time out for *all* inline attempts; assert the session still
  boots and a background retry thread is scheduled (no `RuntimeError` escapes
  `_start_subscription`).

**Test 3 — eventual success resumes cleanly.**
- After background success, assert deployment state is saved and (for a session
  with pre-existing saved deployment) the cursor is preserved so replay resumes
  rather than resetting.

Testability notes (implementation will need to make this observable): the
background retry should be injectable/short-circuitable in tests — e.g. an
overridable backoff/clock and a way to await/observe the retry thread — so tests
don't sleep real seconds. Listed as decision O2.

## 7. Design trade-offs & risks

- **R1 — Deaf-but-alive coordinator.** The original code is fatal *on purpose*:
  a manager that's up but unregistered silently swallows events. Mitigation:
  §4.4 status surfacing + loud (throttled) logging, and the background retry
  keeps trying. Trade-off accepted because death-then-relaunch is strictly worse
  for dispatch availability.
- **R2 — Fresh-deployment event gap (the real nuance behind "events are
  resumable").** Resume/replay only covers a deployment that **already exists**
  server-side: its events are buffered (48h) and replayed by cursor. A
  **brand-new** session (no saved `deployment_id`, e.g. the #329 sub-agents)
  that hasn't registered yet has **no deployment for the server to queue events
  against** — events matching its subscriptions during the pre-registration
  window are not routed to it. So:
  - For **persistent coordinators with saved deployment state** → late
    registration resumes with **zero loss** (best case; matches the issue's
    framing).
  - For **fresh sessions** → background retry shrinks but does not fully
    eliminate a startup gap. This is still far better than dying, and for most
    fresh sub-agents the relevant work is delivered after they're connected.
    Flagged so Zach can weigh it. (Decision O4: accept the gap, or block
    *first-ever* registration while making *re*-registration non-fatal?)
- **R3 — Thundering herd.** Simultaneous recovery of many sessions could spike
  the worker. Mitigation: jittered backoff (§4.2).
- **R4 — Longer/again-failing inline attempt delays boot.** Keep the inline
  budget small; push persistence to the background thread.
- **R5 — Masking a real outage.** If the worker is genuinely down, sessions now
  run degraded indefinitely instead of failing fast. Mitigation: §4.4
  status + a future alert threshold (e.g. degraded > X min) — out of scope here
  but enabled by the status field.

## 8. Open decisions (for Zach)

- **O1 — Timeout/retry constants.** Confirm: read-timeout 15s → **30s**; inline
  attempts **2–3**; background backoff **2s→…→cap 60s + jitter**, long horizon
  (hours, within 48h buffer). Adjust?
- **O2 — Test seams.** OK to add an injectable clock/backoff + observable retry
  thread purely to make the regression test deterministic?
- **O3 — Which failures stay fatal?** Make *timeout/connection* failures
  non-fatal but keep *non-transient* errors (bad config, hard auth rejection
  that isn't `BubbleRejected`) fatal-fast? Or make **all** registration failures
  non-fatal and rely solely on the degraded status?
- **O4 — Fresh-deployment gap (R2).** Accept the small startup gap for
  brand-new sessions, or require a successful *first* registration before ready
  (non-fatal only for *re*-registration)?
- **O5 — Worker fix scope.** Keep this ticket to investigation + timing logs and
  spin server-side fixes into a follow-up, or fold an identified worker fix in
  here?

## 9. Implementation plan (post-approval — do NOT start yet)

1. Write failing regression tests (§6) — prove current code dies.
2. Refactor `_start_event_subscription` (`subagent.py:859-1018`): split
   register-and-build into (a) fast inline attempt, (b) background retry thread
   that builds + attaches the client on success.
3. Update `_start_subscription` (`session.py:493-526`) to not re-raise on the
   transient class for `has_external`.
4. Bump `_post_register` timeout + add jittered capped backoff
   (`server.py:202`, `subagent.py:896-933`).
5. Add `event_status` to `SessionEntry` + surface in `status`/`doctor` (§4.4).
6. Add throttled failure logging + recovery log (§4.3).
7. Worker: add timing logs in `handleRegisterDeployment` (`core.ts`), pull logs
   for the failure windows, write findings note; file follow-up if needed.
8. `/review`, run tests, then PR.

## 10. Acceptance criteria

- [ ] A transient registration timeout no longer kills a (coordinator) session;
      the agent boots and reconnects in the background.
- [ ] Repeated registration failures are logged with backoff and do **not**
      terminate the process.
- [ ] Regression test simulating a registration read-timeout → session survives
      and retries (fails on current code, passes after fix).
- [ ] Read-timeout increased and background retry uses capped backoff + jitter
      with a long horizon.
- [ ] A not-yet-registered coordinator is visible as `connecting`/`degraded`
      (not silently healthy).
- [ ] Worker registration path investigated; timing logs added; findings noted
      (server-side fix scoped or deferred per O5).
