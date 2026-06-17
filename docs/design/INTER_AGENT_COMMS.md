# Inter-Agent Comms — Unifying Agent Messaging onto the Event Server

Status: **design draft (2026-06-16).** First write-up of the "move inter-worker
communication onto the event server" project. Ready for a holes review, then
ticketing via `/build-plan`.

Related designs:
- `docs/design/AUTH.md` — bubbles. **Load-bearing dependency for cloud mode:**
  this design's "cone of silence" (an instance's internal chatter never reaches
  another instance) is *enforced* by bubble scoping (#240) when comms rides the
  remote Worker. In local-server mode the boundary is loopback instead.
- `docs/design/EVENT_CONTRACT_V2.md` (#177–#181, shipped) — the v2 envelope,
  `topics[]` routing, `delivery` class, and `run_key` correlation this design
  builds on directly. Inter-agent messages are just v2 events.
- `docs/design/CONTAINERIZED_INSTANCES.md` — the local-vs-Cloudflare event
  server is a per-deployment *packaging* choice (`event_server_url`); this
  design is deliberately agnostic to it (§2, §7).

Settled framing (decided 2026-06-16, in conversation):
- **No inter-instance communication.** Each instance is its own *cone of
  silence*; agents talk only to other agents in the same deployment. The
  meta-manager / cross-instance vision is explicitly deferred. (Memory:
  `project_cone_of_silence`.)
- **One transport per deployment**, chosen at setup via `event_server_url`
  (local Node server *or* Cloudflare Worker). Comms rides whichever one is
  configured — same code path, only the URL differs. Local-vs-Cloudflare,
  Node-in-image-or-not, and tunnel-for-webhooks are all deployment choices,
  not framework rules.

Resolved in the holes review (2026-06-16):
- **Topology: Option A, fully symmetric** (§4). *Every* session subscribes and
  is inbox-aware — not just persistent ones. A fire-and-forget subagent can
  still receive a human steering update mid-task. This is the symmetric-nodes
  principle applied to comms (memory: `project_symmetric_nodes`): no
  manager/subagent distinction in who can be messaged. The cost (a connection
  per live session; churn in cloud mode) is accepted by design; a per-deployment
  multiplexer is the future optimization if it ever bites.
- **`ask` latency: accepted, no special-case** (§6). Inter-agent messages go
  through the normal ~2 s drain batch. An `ask` reply waits on the target's
  full LLM turn (seconds–minutes) anyway, so the batch delay is noise — not
  worth a second delivery cadence in the drain (the v2 work just deleted the
  last such special-case).

---

## 1. Why

Inter-agent messaging is the one core transport that does **not** go through
the event server. It's a second, parallel delivery mechanism — per-session
HTTP servers on `127.0.0.1:<random>` that `deliver()` POSTs to directly
(`inbox.py`). This split is the problem:

1. **Two transports, two failure modes.** External events flow event-server →
   WS → drain → inbox. Direct messages flow `deliver()` → inbox HTTP. They
   converge at the inbox but share nothing upstream — two code paths, two sets
   of bugs, two things to secure.
2. **Localhost-only by construction.** `deliver()` resolves a target's
   `inbox_port` from the registry and POSTs to `127.0.0.1`. It works only
   because all sessions are co-located in one installation on one host. It is
   not a transport so much as an implementation detail of "same box."
3. **No durability, no observability.** The inbox queue is in-memory
   (`queue.SimpleQueue`); a session restart drops anything queued. Direct
   messages never appear in `modastack events` or `events.jsonl` — only
   event-server traffic does. Agent↔agent chatter is invisible.
4. **It's the odd one out.** Lifecycle `agent/*` events already publish through
   the event server (`events/publish.py:post_event` → `/events/{topic}` →
   deployment WS → drain → inbox) and render in `modastack events`. Direct
   messaging is the only sibling that didn't make the move. This design
   finishes the job.

Unifying onto the event server gives one transport, one contract (v2 envelope),
one security boundary (bubbles / loopback), durability (the server can persist +
replay via cursor), and observability for free. It is the **low-level
building block** the auth-bubble and containerized-deployment work both sit on
top of — which is why it's sequenced first.

## 2. Current architecture (precise)

**Inbound external events — one connection per *deployment*:**
- `EventServerClient` (`events/client.py`) opens **one** WS per deployment to
  `/deployments/{deployment_id}/subscribe?last_seen=<cursor>`. The cursor is
  **per-deployment** (`cursor.json`) — "sessions must not share a cursor"
  (`client.py:150`).
- Received events land on a single process-global `event_queue`.
- `drain_loop` (`events/drain.py`) batches the queue, runs auto-dispatch
  (`EventReactor`), groups by `delivery` class (bulk before chat), formats, and
  `deliver()`s the batch to **one** session — the manager.

**Internal direct messaging — localhost HTTP, per session:**
- Every `Session` owns an `Inbox` (`session.py:45`) that starts an HTTP server
  on `127.0.0.1:0` (`inbox.py:128`) and registers its `inbox_port` in the
  session registry (`session.py:298`).
- `deliver(to, text, wait, timeout)` (`inbox.py:176`) looks up the target's
  `inbox_port` and POSTs to `http://127.0.0.1:<port>/inbox`. Subagents are
  separate `claude` processes, so this HTTP hop is the **cross-process**
  delivery mechanism on one host.
- **Synchronous ask** (`wait=True`): the handler holds the HTTP connection open
  on a `_PendingReply` (`threading.Event`) until the session calls
  `respond(msg_id, ...)` or it times out. Used by `modastack message --wait`,
  `modastack ask`, and `agents launch --wait`.

**Already through the event server:** `agent/*` lifecycle events
(`subagent.py:_emit_lifecycle_event` → `post_event`). These return to the
manager over the same single deployment WS. `agents launch --subscribe` /
`--persistent` already hint at per-session participation.

So: **inbound events fan in to the manager; internal messages fan out over
localhost HTTP.** The asymmetry is what we remove.

## 3. Target model

> Inter-agent messages become **v2 events on the configured event server**,
> addressed to a session, delivered over the same subscribe/drain path
> lifecycle events already use. The per-session inbox HTTP transport is
> retired. `ask` becomes async request/reply correlated on an id.

Concretely:
- **Send** = publish an `inbox/<session>` event, replacing `deliver()`'s HTTP
  POST. Reuses the `publish.py` path lifecycle events use — **but note**
  `post_event` today splits the event type on the first `/` into source/type
  and posts to `/events/{type}`; the inbox routing key it produces and the
  subscription key the target registers **must be the exact same string**
  (the server matches subscription keys exactly). Aligning these is the key
  integration seam — see T1.
- **Routing** = a per-session topic so the message reaches only that session
  (see §5).
- **Receive** = the session's subscription delivers it; the drain routes it to
  the in-process queue the session's run loop reads. The `Inbox` *queue* stays;
  its *HTTP server* goes away.
- **Boundary** = loopback (local mode) or bubble (Cloudflare mode). Either way,
  an instance's `inbox/*` events never reach another instance.

## 4. Core design decision — subscription topology [CALL]

This is the crux and the thing the review must pressure-test. For a session to
*receive* a published message it must be subscribed. Today only the deployment
(manager) subscribes. Two shapes:

**Option A — per-session subscription (DECIDED).** Each session opens its
own WS and subscribes to `inbox/<session>` plus any shared topics. Delivery is
the server fanning out to that session's connection; the drain writes to the
session's own queue. The inbox HTTP server is fully retired.
- *Pro:* one transport, truly. Matches AUTH.md, which already says "each agent
  session joins [the bubble] by signing its registration with the stored key" —
  per-session WS is the *implied* model. `agents launch --subscribe` already
  exists.
- *Pro:* subagent→subagent needs no manager hop.
- *Con:* a connection per live session (in **Cloudflare mode**, WS to the Worker
  + churn as short-lived subagents come and go). Accepted by design — see below.
- *Note:* cursors are already per-session (deployment == session); no cursor
  rework needed — only "every session subscribes" instead of opt-in.

**Option B — manager-routes (rejected).** Manager stays the sole subscriber;
it receives `inbox/<session>` events on its one WS and forwards to the target's
inbox HTTP server. One connection per deployment, cursor model unchanged — but
it does **not** retire the localhost transport (the stated goal) and routes
every subagent→subagent message through the manager. Half a migration.

**DECIDED: Option A, fully symmetric.** *Every* session subscribes and is
inbox-aware — **not** scoped to persistent sessions (the review's first instinct,
overruled 2026-06-16). A fire-and-forget subagent can receive a human steering
update mid-task, and there's no manager/subagent distinction in who is reachable
(`project_symmetric_nodes`). Cost: a connection per live session and, in cloud
mode, churn from short-lived subagents — accepted; the per-deployment
multiplexer is an additive optimization for later, not a contract change.

**The machinery already exists — this generalizes it (verified against code,
2026-06-16).** One deployment on the event server **== one session** by design:
each subscribed session calls `register(url, session_name, subscribe)` and gets
its own `deployment_id` + `api_key` + cursor + `EventServerClient` + `drain_loop`
(`subagent.py:_start_event_subscription`). The `--subscribe` flag already does
exactly this. So "every session subscribes" is **flipping an opt-in to
always-on**, not new architecture.

There is **one** per-session deployment carrying a *set* of subscription keys —
not two separate subscriptions:
- **Manager:** external resource topics (`github:org/repo`, `slack:…`) **plus**
  `inbox/manager`. One deployment, one cursor.
- **Every other session:** just `inbox/<self>` (+ any shared topics it opts
  into). One deployment, one cursor.
The server delivers a topic to the deployment(s) subscribed to it; since
deployment == session, `inbox/<session>` reaches exactly that session. No
server-side per-connection granularity is needed.

## 5. Addressing

- Per-session inbox topic: **`inbox/<session_name>`**. Send =
  `post_event("inbox/<target>", {sender, text, ...})`. Session names are
  already the registry key (`{role}-{run_key}[-{phase}]`, `eng-42-implement`),
  so addressing reuses existing identity — no new namespace.
- The manager's well-known name (`manager`) makes `modastack ask` /
  `message --to manager` route unchanged at the API surface.
- Shared/broadcast topics (e.g. "all sessions") remain ordinary v2 topics; a
  session subscribes to the set it cares about. No wildcard matching is
  introduced (the v2 contract is exact-key — DECIDED in EVENT_CONTRACT_V2).
- `delivery` class: inter-agent messages set `delivery: "chat"`? **[CALL]** —
  leaning **no**; they are directed agent work, not human conversation. Give
  them the default (`bulk`) ordering, or a new `direct` class if review finds an
  ordering need. Auto-dispatch (`EventReactor`) should **skip** `inbox/*`
  events — they're already addressed; don't re-route them.

## 6. Async ask (request/reply)

Pub/sub is fire-and-forget; the synchronous `wait=True` path must become
request/reply correlated on an id (reuse `msg_id`, or `run_key` where present):

1. Sender publishes `inbox/<target>` with `{reply_to: "inbox/<sender>",
   corr_id}`.
2. Target processes, publishes `inbox/<sender>` with `{corr_id, response}`.
3. Sender matches `corr_id`, returns, drops the rest.

**The hard case — the CLI is a one-shot process.** `modastack ask "q" --wait`
isn't a long-lived subscriber; today it just holds an HTTP connection open.
Over pub/sub it must: open a WS, subscribe to a transient reply topic
(`reply/<uuid>`), publish the request, await the correlated reply, close. That's
a real new code path. **[CALL]** Alternatives to weigh in review:
- (a) Transient reply-topic subscription from the CLI (above) — pure pub/sub,
  works identically in local and Cloudflare mode.
- (b) Keep a *synchronous* request/reply HTTP endpoint on the **event server**
  (`POST /ask/<session>` that blocks server-side) — simpler CLI, but adds a
  blocking endpoint to the server contract.
- (c) Local mode keeps a thin synchronous path; cloud mode uses (a). Rejected
  on principle — that's two transports again.

**DECIDED: (a)** — transient reply-topic subscription. One transport in both
modes; it's the most involved single piece, so it's its own ticket (T2).
**Latency: accepted, no fast-path.** Replies traverse the normal ~2 s drain
batch on each hop; an `ask` waits on the target's full LLM turn anyway, so the
batch delay is noise (no second drain cadence — §settled decisions).

## 7. Interaction with the two server modes

Same code, different boundary — no branching in the comms layer:
- **Local mode** (`event_server_url` = local Node server): `inbox/*` events
  stay on the box. Loopback bind (#241) is the cone of silence; bubbles not
  required for isolation. Needs Node in the image (a deployment packaging
  variant, not a framework rule — see CONTAINERIZED §amend).
- **Cloudflare mode**: `inbox/*` events ride the same WSS as webhooks.
  **Hard security gate on bubbles (#240) — not just a leak.** Publishing to
  `inbox/<session>` writes text straight into an agent running
  `bypassPermissions`. Pre-bubbles, `POST /events/{topic}` is unauthenticated
  and global, so without #240 *anyone who knows the URL could inject
  instructions into any agent* — a prompt-injection vector, not merely a
  cross-tenant cone-of-silence leak. So in cloud mode this design **must not
  ship before #240**. Internal chatter round-trips to Cloudflare (tens of ms —
  negligible at agent timescales) and depends on connectivity (a dropped WSS
  stops internal comms too — accepted cost of choosing cloud mode).

## 8. What gets retired / changed

- **Retired:** the inbox HTTP server (`inbox.py` `_InboxHandler`,
  `_ThreadedHTTPServer`, the `do_POST /inbox` path), `inbox_port` in the
  registry, and `deliver()`'s `urllib` POST. The `Inbox` *queue* + `recv` stay;
  it's fed by the drain instead of by HTTP.
- **Changed:** `deliver()` becomes a publish to `inbox/<target>`;
  `drain_loop` routes per-target (not always to the manager); each session gets
  its own subscription + cursor (Option A); `ask`/`--wait` become async
  request/reply.
- **Unchanged:** the v2 envelope, `post_event`, `format_event_for_manager`,
  auto-dispatch (except: skip `inbox/*`), the `Session` run loop's `recv`.

## 9. Migration

Hard cutover, consistent with EVENT_CONTRACT_V2's precedent (no external
installs; every consumer is a deployment we control). `deliver()` keeps its
signature `(to, text, wait, timeout) -> (ok, response)` so call sites
(`cli.py message/ask`, subagent handoffs, monitors) don't change — only its
*implementation* swaps from HTTP POST to publish/await. The inbox-HTTP removal
and per-session subscription land together so the repo is never half-migrated
(the #163/#166 cross-runtime-drift lesson). Verification: existing inbox /
messaging integration tests must pass against the new transport; add one
asserting a subagent→subagent message and a `--wait` ask both round-trip over
the event server (local mode), and one asserting cone-of-silence (two
deployments, same session name, no cross-delivery) in Cloudflare mode with
bubbles.

## 10. Open questions (resolved items struck; the rest are ticket inputs)

*Resolved 2026-06-16:* topology (Option A, fully symmetric — §4); `ask`
mechanism + latency ((a), accept latency — §6); connection churn (accepted by
design, multiplexer deferred — §4).

Remaining, to settle inside the relevant ticket:

1. **Cursor & late subscribers (§6, T1)** — per-session cursors mean a session
   not yet subscribed when a message is published must still receive it on
   connect (server replay from cursor). Does a freshly-spawned subagent have a
   durable subscription identity *before* it connects? The race to close — and
   the reason the per-session cursor (not just a connection) is load-bearing.
2. **Loop safety (#215, T4)** — `inbox/*` is *legitimate* agent↔agent traffic;
   the planned delivery-path circuit breaker must not trip on it. Coordinate the
   breaker's keying so internal comms is exempt (or counted differently).
3. **Failure semantics (T1)** — `deliver()` today returns "session not found /
   dead / no inbox". Over pub/sub, publish succeeds even if no one is
   subscribed. How does a sender learn the target is gone? (Presence check via
   registry before publish, or accept best-effort + `--wait` timeout.)
4. **Ordering / delivery class (§5, T1)** — do directed messages need their own
   ordering relative to bulk/chat, or is `bulk` fine? (Lean `bulk`.)
5. **Persistence scope (T4)** — log `inbox/*` to `events.jsonl` like everything
   else (observability win) or is that noisy? (Lean: log it.)

## 11. Work breakdown (sketch — `/build-plan` produces the real tickets)

Rough shape; dependency-ordered. The contract/transport change is likely one
atomic ticket (splitting leaves the repo half-migrated):

- **T1 — comms-over-event-server core.** `deliver()` → publish `inbox/<target>`;
  per-session subscription (Option A) + per-session cursor; drain routes
  per-target; retire inbox HTTP server + `inbox_port`. Skip `inbox/*` in
  auto-dispatch. *Gate:* messaging integration tests green over the new
  transport (local mode).
- **T2 — async ask / request-reply.** `wait=True` → correlated request/reply;
  CLI reply-topic subscription (§6). *Gate:* `ask`/`--wait` round-trip green.
- **T3 — cloud-mode cone of silence.** Verify/enforce bubble scoping for
  `inbox/*`; depends on AUTH.md #240. *Gate:* two-deployment no-cross-delivery
  test.
- **T4 — observability + loop-safety coordination.** `inbox/*` in
  `modastack events`; exempt from / aligned with #215 circuit breaker. *Gate:*
  events surfaced; breaker doesn't trip on legitimate comms.

#215 (loop safety) and #240 (bubbles) are the cross-project dependencies to
sequence against.
