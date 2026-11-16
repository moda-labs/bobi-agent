# Spec: a session re-registers when the event server says its deployment is gone

> Review copy: the description of issue #1160 (Gate 1). `docs/specs/1160-reregister-gone-deployment.md` on the draft PR mirrors it and is updated with any Gate-1 changes before implementation starts.

Status: **draft for Gate 1** (Zach approves; no implementation before approval).
Base: `main` at `5ed76cf4` (0.62.1); every file:line below was read at that commit. Not plan-born.
It reverses a recorded choice: #1081's retain-and-retry, and decision D10 of `plans/2026-10-09-workspace-event-subscriptions.md` (see D1).
The original report is preserved verbatim at the end of this issue.

## Decisions needed at Gate 1

Each has a recommendation. Approving the spec approves the recommendation unless you say otherwise.

| # | Decision | Recommendation |
|---|---|---|
| D1 | Reverse "never re-register a saved deployment" (#1081, plan D10), but only when the server itself says the deployment is gone. | Yes. Every ambiguous failure (timeout, 5xx, a bare 403) keeps #1081's retain-and-retry, so a quick restart still replays unfinished work. |
| D2 | Scope: the three reproduced triggers in one PR. T1 is a restart after a long stop, T2 a shared local server that restarted, T3 a running session offline for over a minute. | One PR: it is one failure, an identity that no longer exists with nothing telling the client. The alternative is T1 and T2 now (boot path only) and T3 next on this ticket. |
| D3 | How long the server remembers why it removed a deployment. | 30 days on the Worker (KV TTL), the newest 10,000 records on the local server. Past that the Worker falls back to today's behavior; the local server still answers `unknown`, so it recovers anyway. |
| D4 | Events that reached the old deployment but were never processed stay lost, as today. | Accept, and report them: the server counts them at eviction and the client logs the count. Keeping them needs a longer-lived deployment or a local journal (see "What this does not close"). |

## What the investigation found

All three triggers reproduced on `main` `5ed76cf4` against a real local event server, no mocks.
The eviction window was shortened to 2 s through the existing `ensure_running(..., extra_env=...)` hook (`tests/integration/test_inbox_transport.py:249`).
Each boot ran in its own process.

- **T1, the reported case: a restart after more than the eviction window, with the server still running.**
  The first boot registered and connected; a normal stop leaves the deployment registered.
  After 4 s the server logged `evicted stale deployment f73ccb3f-... (sess) - disconnected 2s ago`.
  The next boot attached to the same server and failed three times with the report's exact message, keeping the dead id on disk while the server held no deployments.
  Deleting only `state/deployments/sess.json` (bubble and cursor kept) let the following boot register and connect at once.
- **T2: two agents share one local server, and it restarts (reboot or crash).**
  Agents that leave the event server unconfigured all use `localhost:8080` (`bobi/subagent.py:2283-2284`, `bobi/ingress.py:11`), so two of them on one machine share one server.
  The agent that starts the replacement server archives only its own transport state (#1114, `bobi/events/server.py:546`, `:683`) and recovers.
  The other agent attaches and strands with the identical message.
- **T3: a running session loses its connection for longer than the window.**
  The probe closed the socket and made reconnects fail for 4 s inside the running process; the server evicted the deployment.
  Once reconnects were allowed again, every one was rejected.
  The client kept the dead id, logged `Event client unstable: 5 short-lived connections in a row ... check event server / auth / network`, and never re-registered.
  Against the hosted Worker the same code path should do this to an agent whose laptop sleeps or whose network drops for over a minute (inferred, not reproduced against a Worker).

Root cause:

- Both servers delete a deployment and its api key once it has had no connection for 60 s: the local sweep (`event-server/src/local.ts:77`, `:819-850`) and the Worker's Durable Object alarm (`event-server/worker/src/deployment-session.ts:25`, `:245-286`).
  The deployment's buffered events go with it.
- After that, a request with the right key gets the same `403 {"error":"unauthorized"}` as a wrong key (`event-server/core/src/core.ts:783-790`, `:1491-1492`).
  On the local server a WebSocket upgrade is simply dropped (`local.ts:729-733`).
- Boot reuses the saved identity and treats every failure as "keep it and retry" (`bobi/subagent.py:2304-2310`, `:2075-2152`), and the session's background retry repeats that forever (`bobi/session.py:1813-1866`).
  A running client reconnects forever and never re-registers (`bobi/events/client.py:364-396`, `:498-505`).
- A normal stop does not deregister (`bobi/subagent.py:1759-1775`), so any stop or crash longer than 60 s while the server stays up strands the session: managers, and any resumed worker session with saved state.

Today's behavior is deliberate, and the reason shapes the fix.
#1081 (MOD-289, fixes #867) made boot keep the saved identity, because re-registering on every failure threw away unfinished events after a quick restart.
Its PR notes list this gap (R1, R2) and say a replacement is safe only if the server can establish the loss, prove the caller owned the deployment, and prevent competing replacements: "a bare HTTP 403, timeout or eventually consistent storage miss cannot safely authorize replacement."
Plan D10 (`plans/2026-10-09-workspace-event-subscriptions.md:57-58`) kept that for #952, and `docs/EVENT_SERVER.md:476-481` documents it.
A plain "re-register on 403" is unsafe: a registration removes any other deployment with the same name in the bubble (`core.ts:1441-1451`).
A wrong guess would therefore delete a live deployment's buffered events, and two processes with one session name would keep removing each other.

## Solution

The server says when a deployment is gone and why; the client re-registers only on that answer.

- **The server records why it removed a deployment.** When it evicts an idle deployment, or replaces it with a same-name registration, it keeps a small record keyed by a hash of the old api key: the deployment id, the reason, the time, and for an eviction the number of events that were never acknowledged.
- **The old key holder gets `410 Gone` with the reason.** Everyone else still gets the opaque 403.
- **The local server can also answer for ids it does not hold.** It keeps everything in memory in a single process, so an id it does not hold does not exist, and it answers `410` with reason `unknown`. That covers T2, where a restarted server has no records at all. The Worker cannot do this: its storage is eventually consistent, so a miss there proves nothing.
- **The client re-registers only on `evicted` and `unknown`.** On `superseded` (another registration of this session replaced it) it does not, which is what stops two same-name processes from fighting. Any other answer keeps #1081's behavior.
- **One replacement path.** Boot handles the answer directly. A running session whose reconnect gets the `410` hands over to the same boot path, after making sure its old client can no longer write the cursor.

### What an operator sees

A recovered session logs one warning:

```text
WARNING bobi.subagent: Event-server deployment f73ccb3f for session sess was evicted at 2026-11-16T15:02:11Z, after its connection was gone longer than the server's eviction window; registering a new deployment. 3 events that reached the old deployment were never processed and cannot be replayed.
```

After a restarted local server: `... is unknown to the event server (it restarted, or the deployment predates it); registering a new deployment. Events sent to the old deployment cannot be replayed.`

After a replaced registration, with no re-registration: `... was replaced by another registration of session sess at <time>; this process no longer owns it and will not re-register. Stop the duplicate process.`

Every other failure keeps today's message and retry.

## Scope

In:

- S1. Server: removal records for idle eviction and same-name supersede on both backends, through the shared core (`core.ts`) and each storage adapter.
- S2. Server: one decision for a failed deployment lookup, used by `PUT /deployments/{id}/subscriptions` and the WebSocket upgrade on both backends. The local upgrade answers with an HTTP status instead of dropping the socket.
- S3. Client boot: recognize the `410` and re-register through the existing `_register_with_retry` (`bobi/subagent.py:2029`).
- S4. Client running: the event client reports a `410` on reconnect; the session retires the old subscription and re-enters the boot path.
- S5. The manager's `POST /subscriptions/reload` and the CLI name `deployment_gone` instead of answering 503.
- S6. Docs in the same PR: `docs/EVENT_SERVER.md` (the D10 paragraph at `:476-481`, the wire shape, the security model); `docs/SELF_HOSTED_EVENT_SERVER.md:255-266` and `:80-81`, since agents no longer need restarting after a server restart; and a dated amendment under D10 in the plan file.
  The `:260` line also gets corrected: it says the launcher matches `/health`'s `process_id` to its child, which no code does (only test fixtures mention `process_id`).

Out:

- Keeping events across a long outage (a durable deployment or a local journal); see "What this does not close".
- Two processes running one session (#1056). This spec only guarantees they cannot loop.
- Showing a stranded session on the agent page or in `bobi agent status` (the same gap as #1157, by a different mechanism).
- A new operator command for the cases that still strand: `restart --fresh` stays the documented action.
- Changing the 60 s eviction window.

## Technical approach

### Removal records (S1)

A new record type in `core.ts`:

```ts
interface DeploymentTombstone {
  key_hash: string;        // sha256Hex(api_key) (core.ts:503); the lookup key
  deployment_id: string;
  reason: "evicted" | "superseded";
  removed_at: string;      // ISO 8601, UTC
  unacked_events?: number; // evictions only
}
```

Two storage-adapter methods, `putTombstone(record)` and `getTombstone(keyHash)`, follow the hashed ingest-token precedent (`core.ts:308-309`: "the store never sees a plaintext token").

- Worker: KV key `deployment_tombstone:<hash>` with `expirationTtl` 30 days (precedent: `webhook_delivery`, 7 days, `event-server/worker/src/index.ts:206-212`).
- Local: an insertion-ordered `Map` capped at 10,000 entries (oldest out first), also swept for 30-day age by the existing eviction sweep.

Writers:

- Supersede: `handleRegisterDeployment` writes `superseded` for `prior` before removing it (`core.ts:1446-1451`), shared by both backends.
- Local eviction: `evictStaleDeployments` writes `evicted` with `unacked_events = max(0, nextSeq - 1 - lastAckedSeq)`, the figure `/health` already reports as `pending_events` (`local.ts:460-466`).
- Worker eviction: the alarm writes `evicted` before it deletes the KV records, with `unacked_events` from `next_seq` and the stored ACK `cursor` (`deployment-session.ts:213-214`).
- An explicit `DELETE` writes nothing: its owner removed it on purpose, and transient reply channels deregister after every `ask`, so a record per `DELETE` would be churn with no reader.

### One answer for a failed lookup (S2)

`authenticateDeployment` stays as it is. On a miss, a new core function picks the response:

1. A tombstone for `sha256Hex(key)` with a matching `deployment_id`: `410` with its reason.
2. Otherwise, on a storage adapter that declares itself authoritative for absence (only the local in-memory one), an id that is not a live deployment: `410` with reason `unknown`.
3. Otherwise the existing `403 {"error":"unauthorized"}`. A wrong key for a live deployment always ends here.

It is used by `handleUpdateSubscriptions` (`core.ts:1491-1492`), the Worker upgrade (`index.ts:644-646`) and the local upgrade (`local.ts:729-739`).
The local upgrade now writes a real `HTTP/1.1 403` or `410` response with the same JSON body before closing the socket.

Wire shape:

```json
{"error": "deployment_gone", "reason": "evicted", "removed_at": "2026-11-16T15:02:11.000Z", "unacked_events": 3}
{"error": "deployment_gone", "reason": "superseded", "removed_at": "2026-11-16T15:03:40.000Z"}
{"error": "deployment_gone", "reason": "unknown"}
```

- `reason`: `evicted` (idle eviction), `superseded` (replaced by a same-name registration in the same bubble), or `unknown` (an in-memory server with no record of the id).
- `removed_at`: when the server removed the deployment; absent for `unknown`.
- `unacked_events`: events the server had sequenced for the deployment that its client never acknowledged; evictions only.

### Client boot (S3)

`_put_subscriptions` (`bobi/subagent.py:2154`) raises a new `DeploymentGone(reason, removed_at, unacked_events)` on a `410` whose body has `error: deployment_gone`, before `raise_for_status`, the same way it already parses `unauthorized_topics`.
`_sync_saved_deployment` lets it through instead of folding it into "retry pending".
The boot branch (`:2304-2310`, which already holds `_subscription_apply_lock`) then acts on the reason:

- `evicted` or `unknown`: logs the warning above and calls `_register_with_retry`.
  That already joins the bubble or re-mints one the server forgot (`ensure_bubble`, `bobi/events/server.py:984`).
  It also saves the new identity and deletes the old cursor (`bobi/subagent.py:2057`), because a new deployment starts a new sequence.
- `superseded`, or any other reason: raises the usual "retry pending" error with the new wording, so the session's retry loop behaves as today.

### Client running (S4)

- `EventServerClient` (`bobi/events/client.py:188`) takes an `on_identity_gone` callback.
  A handshake can fail with `WebSocketBadStatusException`, status 410, and a `deployment_gone` body whose reason is `evicted` or `unknown`. The client then stops reconnecting and calls the callback once, off its own thread.
  Every other failure keeps today's reconnect loop.
- `Session` passes its handler through `_start_event_subscription`.
  Under `_sub_lock`, unless the session is stopping, the handler takes the current subscription, retires it, stops it, and starts the existing background retry (`bobi/session.py:1813`).
  The retry reaches the boot path, which gets the same `410` from the server and re-registers.
- `retire()` turns the old client's `ack_through` (`client.py:341`) into a no-op, and it happens first.
  Messages already in the inbox still run, but their late acknowledgements belong to the dead deployment's sequence and would otherwise overwrite the new deployment's cursor.
  An acknowledgement that lands before `retire()` is erased when the registration deletes the cursor.
- The running client is never rebuilt for an ambiguous failure.
  A rebuilt client has no memory of what it already queued, so it would replay events still waiting in the inbox (the reason for `_max_enqueued_seq`, `client.py:241-249`).

### Reload route (S5)

`_apply` (`bobi/subagent.py:2194`) maps the `410` to `{"status": "rejected", "error": "deployment_gone", "reason": "<reason>"}`, so `POST /subscriptions/reload` answers 200 with that body instead of 503.
The CLI (`bobi/cli.py:3655-3664` is the 403 counterpart) says the event server no longer holds this session's deployment and gives the reason.
It adds that the change is saved and applies once the session re-registers on its own; for `superseded`, once the duplicate process is stopped.

### Compatibility and rollout

- The new status only appears on requests that already fail, so the protocol version does not change (the compatibility rules are additive, `docs/EVENT_SERVER.md:132`).
- New client, old server: no `410` ever arrives, so behavior is exactly today's.
- Old client, new server: the `410` lands in its "unexpected response, retry pending" branch, which keeps the identity, like today's 403.
- A local event server that is already running keeps its old code until it restarts, because `ensure_running` attaches to any healthy server without checking its release. On a laptop, T1 and T3 recover only after that restart; T2 involves a restart, so it recovers at once.
- The hosted Worker must be redeployed before T1 and T3 recover there.

### Security

- Only a caller presenting the old api key matches a tombstone, and only for that deployment id; everyone else gets the same opaque 403, so a record discloses nothing to a non-owner. A tombstone stores a hash, never the key.
- `unknown` reveals that an id is not live on an in-memory server, which that server's unauthenticated `/health` already shows by listing every live id (`local.ts:460-466`).
- Re-registration goes through the normal path (bubble join, resource-grant checks), so a `410` grants nothing new.
- Tombstones are bounded by a KV TTL on the Worker and a capped map locally, so a flood of short-lived registrations cannot grow storage without limit.

## Edge cases

- E1. Two processes with one session name.
  Only an eviction or an unknown id triggers a re-registration.
  If one process's registration removes the other's deployment, the other gets `superseded` and stops trying, so they cannot keep replacing each other.
  Which one keeps receiving events is the existing #1056 problem.
- E2. Worker storage is eventually consistent.
  Just after an eviction, a request can miss both the deployment and the new tombstone. It gets a 403 and retries, and a later retry sees the `410`.
  A request that still sees the deleted record behaves as today.
- E3. A tombstone that expired, or fell out of the local cap. On the Worker, behavior reverts to today's. Locally the id is still absent, so the answer is `unknown` and the client still recovers.
- E4. A stale bubble after a local server restart. `_register_with_retry` re-mints it through `ensure_bubble`'s compare-and-swap, so all sessions of one agent end up in one new bubble (`bobi/events/server.py:994-998`).
- E5. A deployment deleted with its own key. The local server answers `unknown` (re-register); the Worker answers 403 (keep). Sessions never delete their own deployment, so only tooling that does it deliberately sees this.
- E6. The session stops during a hand-over. The handler checks `_sub_retry_stop` under `_sub_lock`, and the retry loop already discards a subscription that arrives after `stop()` (`bobi/session.py:1846-1852`).
- E7. Nothing new is stored on the client, so existing state and cursor files need no migration.
- E8. A standalone server is single-node by design (`docs/SELF_HOSTED_EVENT_SERVER.md:247`). Run as several replicas behind a load balancer, `unknown` would be wrong; that setup is already unsupported.

## Alternatives considered

- Re-register on any 403. Rejected: a wrong guess deletes a live same-name deployment and its buffered events, and two same-name processes would loop (`core.ts:1441-1451`).
- A client-only check of the local `/health` deployment list. Correct for in-memory servers, but no help on the Worker, and it puts one decision in two places. The server's `unknown` answer is the same check, made once, where the data lives.
- The server re-creates the deployment for the old key holder. It keeps the identity, but the Durable Object's storage is gone after an eviction, resource grants would need re-checking server-side, and a restarted local server has nothing to re-create from.
- A server instance id that the client compares after a 403. It covers T2 only, and `unknown` covers T2 without new client state.
- Deregister on a normal stop. That breaks #1081's quick-restart replay, which needs the deployment to survive.

## What this does not close

- Events that reach a deployment while its session is away are still lost when it is evicted. The fix restores delivery from re-registration onward and reports the loss. Keeping those events needs deployments that outlive a long outage (a server retention policy for memory and Durable Objects) or a local journal of received events; #1081 lists both as follow-up options.
- Sessions still strand when the server cannot answer: an old server, a Worker tombstone older than 30 days, wiped Worker storage, or a superseded duplicate. They keep today's message and retry, and `restart --fresh` stays the recovery.
- A stranded session still reads as running on the agent page.

## Verification plan

Python unit tests:

- `tests/test_subscription_recovery.py` gains `410` cases.
  `evicted` and `unknown` register exactly once, save the new identity, delete the cursor and hand the new id to the client.
  `superseded`, a `410` with any other body, and every existing failure (200, 400, 401, 403, 404, 429, 5xx, timeout, malformed) still never register.
- `tests/test_event_client.py`: a 410 handshake with a gone body calls `on_identity_gone` once and stops; a 403, a refused connection and a 410 with another body keep reconnecting; a retired client's `ack_through` writes no cursor and sends no frame.
- `tests/test_session.py`: the hand-over retires and stops the old subscription and starts the retry; a `stop()` racing it leaves no live client.
- `tests/test_subscription_reload.py` and `tests/test_subscriptions_cli.py`: the reload body and the CLI wording for `deployment_gone`.

Event-server tests (vitest), core and local:

- Supersede writes a tombstone.
  The lookup answers 410 for a tombstoned key and id, 403 for a tombstoned key with another id, 403 for a live id with a wrong key, 410 `unknown` for an absent id on the local adapter, and 403 for an absent id on a non-authoritative adapter.
- Local eviction writes `evicted` with the right `unacked_events`; the upgrade returns real 403 and 410 responses; the cap and the 30-day sweep hold.
- Worker: the alarm writes the tombstone with its TTL before deleting the records, and the routes return 410. These live in the Wrangler suite, which needs Node 22, so PR CI proves them rather than a local run.

Integration tests against a real local server with a short eviction window, no mocks, and no Claude leg (the change is brain-agnostic):

- T1, T2 and T3 as reproduced above. Each ends with an event published to `inbox/<session>` after recovery reaching the session, and the log naming the old id.
- T3 also asserts that an old message finishing after the hand-over leaves the new cursor untouched.
- `tests/integration/test_interrupted_event_recovery.py:201-238` changes on purpose: a wrong key for a live deployment still gets 403 and keeps its state, while an explicitly deleted deployment now answers `unknown` on the local backend (E5).
- `test_interrupted_event_recovery.py:64` (the #1114 archive) and `:123` (an interrupted event completes in the successor) stay green unchanged. They prove #1081's replay still works for a quick restart.

Each new test is shown to fail with the fix reverted.

## Implementation plan

1. Failing tests first: the Python unit cases, the vitest cases and the three integration reproductions.
2. Server core: the tombstone type, the adapter methods, the lookup decision, the supersede writer.
3. Local server: the map adapter, the eviction writer with its unacknowledged count, real HTTP responses on upgrade.
4. Worker: the KV adapter, the alarm writer, the upgrade route.
5. Client boot: `DeploymentGone`, the reason handling, the log wording.
6. Client running: `on_identity_gone`, `retire()`, the Session handler.
7. The reload route and CLI wording.
8. Docs and the dated D10 amendment.

## Review record

To be filled after the spec reviews.
