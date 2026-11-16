# Spec: a session registers again when the event server says its registration is gone

> Review copy: the description of issue #1160 (Gate 1). `docs/specs/1160-reregister-gone-deployment.md` on the draft PR mirrors it and is updated with any Gate-1 changes before implementation starts.

Status: **draft for Gate 1** (Zach approves; no implementation before approval).
Base: `main` at `5ed76cf4` (0.62.1); every file:line below was read at that commit. Not plan-born.
It reverses a recorded choice: #1081's retain-and-retry, and decision D10 of `plans/2026-10-09-workspace-event-subscriptions.md` (see D1).
The original report is preserved verbatim at the end of this issue.

A *deployment* is the event server's registration of one session: an id and an api key, plus the server's buffer of events for it.

## Decisions needed at Gate 1

Each has a recommendation. Approving the spec approves the recommendation unless you say otherwise.

| # | Decision | Recommendation |
|---|---|---|
| D1 | Reverse "never re-register a saved deployment" (#1081, plan D10), but only when the server itself says the deployment is gone. | Yes. Every ambiguous failure (timeout, 5xx, a bare 403) keeps #1081's retain-and-retry, so a quick restart still replays unfinished work. A process told that another registration replaced its own stops listening instead of fighting back. |
| D2 | Scope, in one PR: T1 (restart after a long stop), T2 (a shared local server restarts, including sessions running at the time), T3 (a running session offline for over a minute), and the supervisor's admin listener, which has the same failure. | One PR: it is one failure, an identity that no longer exists with nothing telling its holder. The alternative, the boot path only, keeps the rule "after restarting the event server, restart every agent", and needs a second PR on this ticket (`docs/TICKETING_POLICY.md:105-106` prefers one). |
| D3 | How long the server remembers why it removed a deployment. | 1 year on the Worker (a KV TTL; about 200 bytes each, written only on an eviction or a replacement). Locally, the newest 10,000 of each kind. Past that, the Worker falls back to today's behavior; the local server answers `unknown`, so it recovers anyway. |
| D4 | Events that reached the old deployment but were never processed stay lost, as today. | Accept, and report honestly: "up to N events lost", from the server's last sequence number minus what this session had already processed or queued. Add a plain statement that events sent after the removal time were not delivered and are not counted. |
| D5 | Whether long-lived sessions get a longer eviction window than 60 s. | Not in this PR. A longer window would have made the reported 71 s gap lossless. But it needs a per-session registration flag (finished worker sessions never deregister, so a global change would keep every finished worker's buffer for the whole window), one constant per backend, and a change to #279's leak backstop. The `410` path is needed either way. If you want it, it fits this PR. |
| D6 | Where a session that still strands becomes visible (agent page, `bobi agent status`). | No status surface here. Add "event subscription unavailable since T (reason)" as a second cause to the degraded-health mechanism #1157 and #1154 are building, and note that on #1157 at approval. |

## What the investigation found

All three triggers reproduced on `main` `5ed76cf4` against a real local event server.
The eviction window was shortened to 2 s through the existing `ensure_running(..., extra_env=...)` hook (`tests/integration/test_inbox_transport.py:249`).

- **T1, the reported case: a restart after more than the eviction window, with the server still running.**
  Each boot ran in its own process, with no mocks.
  The first boot registered and connected; a normal stop leaves the deployment registered.
  After 4 s the server logged `evicted stale deployment f73ccb3f-... (sess) - disconnected 2s ago`.
  The next boot attached to the same server and failed three times with the report's exact message, keeping the dead id on disk while the server held no deployments.
  Deleting only `state/deployments/sess.json` (bubble and cursor kept) let the following boot register and connect at once.
- **T2: two agents share one local server, and it restarts (reboot or crash).**
  Each boot ran in its own process, with no mocks.
  Agents that leave the event server unconfigured all use `localhost:8080` (`bobi/subagent.py:2283-2284`, `bobi/ingress.py:11`), so two of them on one machine share one server.
  The agent that starts the replacement server archives only its own saved credentials and cursors (#1114, `bobi/events/server.py:546`, `:683`) and recovers at boot.
  The other agent attaches and strands with the identical message.
  Sessions that are running when the server restarts strand too, the relaunching agent's own included: the archive only moves files, and a running client reconnects forever with the id it holds in memory (`bobi/events/server.py:546-572`, `bobi/events/client.py:364-396`; inferred from code).
- **T3: a running session loses its connection for longer than the window.**
  The probe closed the socket and made reconnects fail for 4 s inside the running process (the outage was simulated in-process; server and protocol were real). The server evicted the deployment.
  Once reconnects were allowed again, every one was rejected.
  The client kept the dead id, logged `Event client unstable: 5 short-lived connections in a row ... check event server / auth / network`, and never re-registered.
  Against the hosted Worker the same code path should do this to an agent whose laptop sleeps, or whose network drops, for over a minute (inferred, not reproduced against a Worker).
- **The supervisor's admin listener**, the fleet's channel for operator commands, registers once and never recovers (`bobi/supervisor/admin.py:212-227`).
  After the same outage it stays deaf to operator commands until it restarts.

Root cause:

- Both servers delete a deployment and its api key once it has had no connection for 60 s: the local sweep (`event-server/src/local.ts:77`, `:819-850`) and the Worker's Durable Object alarm (`event-server/worker/src/deployment-session.ts:25`, `:245-286`).
  The deployment's buffered events go with it.
- After that, a request with the right key gets the same `403 {"error":"unauthorized"}` as a wrong key (`event-server/core/src/core.ts:783-790`, `:1491-1492`).
  On the local server a WebSocket upgrade is simply dropped (`local.ts:729-739`).
- Boot reuses the saved identity and treats every failure as "keep it and retry" (`bobi/subagent.py:2304-2310`, `:2075-2152`), and the session's background retry repeats that forever (`bobi/session.py:1813-1866`).
  A running client reconnects forever and never re-registers (`bobi/events/client.py:364-396`, `:498-505`).
- A normal stop does not deregister (`bobi/subagent.py:1759-1775`), so any stop or crash longer than 60 s while the server stays up strands the session: managers, and any resumed worker session with saved state.

Today's behavior is deliberate, and the reason shapes the fix.
#1081 (MOD-289, fixes #867) made boot keep the saved identity, because re-registering on every failure threw away unfinished events after a quick restart.
Its PR notes list this gap (R1, R2) and say a replacement is safe only if the server can establish the loss, prove the caller owned the deployment, and prevent competing replacements: "a bare HTTP 403, timeout or eventually consistent storage miss cannot safely authorize replacement."
Plan D10 (`plans/2026-10-09-workspace-event-subscriptions.md:57-58`) kept that for #952, and `docs/EVENT_SERVER.md:476-481` documents it.
A plain "re-register on 403" is unsafe: a registration removes any other deployment with the same name in the bubble (`core.ts:1441-1451`).
A wrong guess would therefore delete a live deployment's buffered events, and two processes holding one session would keep removing each other's registration.
Sharing one registration is no better: a new connection to a deployment closes the existing one (`local.ts:748-751`), so they would keep knocking each other off.

## Solution

The server says when a deployment is gone and why; the client registers again only on that answer.

- **The server records why it removed a deployment.** On an idle eviction, or when a same-name registration replaces it, it keeps a small *removal record* keyed by a hash of the old api key: the deployment id, the reason, the time, and for an eviction the last sequence number it assigned.
- **The old key holder gets `410 Gone` with the reason.** Everyone else still gets the opaque 403.
- **The local server can also answer for ids it does not hold.** It keeps everything in memory in a single process, so an id it does not hold does not exist, and it answers `410` with reason `unknown`. That covers T2, where a restarted server has no records at all. The Worker cannot do this: its storage is eventually consistent, so a miss there proves nothing.
- **Both servers advertise the capability in `/health`.** A client can then tell an old server, which cannot answer, from a new server whose 403 is genuinely ambiguous, and point the operator at the right fix.
- **The client registers again only on `evicted` and `unknown`.** On `superseded` (another registration of this session replaced it) the process stops listening and says so, so two processes cannot fight over one session. Any other answer keeps #1081's behavior.
- **One replacement path.** Boot acts on the answer directly. A running client that gets the `410` hands over to the same boot path, after making sure its old client can no longer write the cursor. The hand-over never adopts an identity another process saved.
- **Reconnects retry often enough to beat eviction.** The reconnect delay is capped at 10 s instead of 60 s, so an outage shorter than about 50 s reconnects before the server evicts.

### What an operator sees

| Situation | What happens | Logged once | Operator action |
|---|---|---|---|
| The server evicted the registration (boot or running) | Registers again | WARNING: old and new ids, the time, up to N events lost, and that later events were not counted | None. Check the sources (GitHub, Slack, Linear) for anything sent to the agent after that time. |
| The server has no record of it (`unknown`) | Registers again | WARNING: no record; old and new ids | The same |
| Another process registered this session (`superseded`, or the saved file now names another registration) | Stops this process's event subscription; no retry | ERROR: pid, host, time | Keep one process: stop this one, or stop the other one and then restart this one. |
| Registration fails right after the `410` | Retries with backoff | The usual retry warning per attempt; the recovery message once it succeeds | None unless it persists |
| Worker storage lag just after an eviction | A 403, then a `410` on a later retry | One "retry pending" warning, then the recovery message | None |
| The server runs code from before this change | Retries, as today | WARNING naming the fix | Restart the local event server, or redeploy the Worker. The session then recovers on its own. |
| A new server still answers a bare 403 (Worker record older than a year, wiped storage, a corrupted credential) | Retries, as today | WARNING naming the narrow reset | The narrow reset, below |
| An agent without this change, on a new server | Retries, as today | `unexpected subscription response (HTTP 410); check server configuration` | Upgrade the agent |
| `subscriptions reload` while evicted or unknown | Saved; applies once the session registers again | CLI text, exit 0 | Optionally confirm with `subscriptions list` |
| `subscriptions reload` while superseded | Saved, not applied | CLI text, exit 1 | Keep one process |

The narrow reset replaces `restart --fresh` as the documented last resort.
Stop the agent, delete the session's file under `state/deployments/` (the manager's is `bobi-<agent>-<role>.json`), and start the agent again.
The session registers again and keeps its conversation; events the old registration still held are abandoned.
`restart --fresh` also does this, but it also clears the manager's conversation, the trust bubble, and every other session's registration and cursor (`bobi/service.py:147-157`).

Messages, with full ids so one search covers the client and server logs:

```text
WARNING bobi.subagent: Event server dropped the registration for session sess (deployment f73ccb3f-b002-43b1-8c42-a63e96839a2f) at 2026-11-16T15:02:11Z because the session was disconnected longer than the server allows (60 s by default); registered again as deployment 15231296-fffe-4cb6-b55d-aa48fa66b59f. Up to 3 events the old registration held were never processed by this session and are lost. Events sent to this session after 15:02:11Z were not delivered and are not counted.

WARNING bobi.subagent: Event server has no record of the registration for session sess (deployment f73ccb3f-b002-43b1-8c42-a63e96839a2f): it restarted since this session last connected, or dropped the registration too long ago to remember it. Registered again as deployment 15231296-fffe-4cb6-b55d-aa48fa66b59f. Events this session had not processed are lost; the server cannot say how many.

ERROR bobi.subagent: Session sess was registered again by another process at 2026-11-16T15:03:40Z, replacing this process's registration (deployment f73ccb3f-b002-43b1-8c42-a63e96839a2f). This process (pid 4242 on host-a) no longer receives events for sess and will not register again. Keep one process: stop this one, or stop the other one and then restart this one.

WARNING bobi.subagent: The event server at http://localhost:8080 rejected session sess's saved registration (deployment f73ccb3f-b002-43b1-8c42-a63e96839a2f) and runs code from before automatic re-registration, so it cannot say why. Restart it (stop pid 51234, the process_id in its /health, then start the agent) and this session recovers on its own. Retrying.
```

The last message names `npx wrangler deploy` instead when the server is a Worker.
When nothing was lost, the eviction message drops the "Up to N" sentence and keeps the last one; "1 event" is singular.

## Scope

In:

- S1. Server: removal records for idle eviction and same-name replacement on both backends, through the shared core (`core.ts`) and each storage adapter. Supersedes are logged on the server like evictions are.
- S2. Server: one decision for a failed deployment lookup, used by `PUT /deployments/{id}/subscriptions` and the WebSocket upgrade on both backends. The local upgrade answers with an HTTP status instead of dropping the socket. Both servers add `capabilities` to `/health`.
- S3. Client boot: read the answer, register again through the existing `_register_with_retry` (`bobi/subagent.py:2029`), stand down on `superseded`, and name the fix for an old server.
- S4. Client running: the event client reports a `410` on reconnect and caps its reconnect delay at 10 s; the session retires the old subscription and re-enters the boot path.
- S5. The supervisor's admin listener recovers the same way (`bobi/supervisor/admin.py:212-252`).
- S6. The manager's `POST /subscriptions/reload` and the CLI report `deployment_gone`, and the CLI's 403 text is rewritten.
- S7. Docs, in the same PR:
  - `docs/EVENT_SERVER.md`: the D10 paragraph (`:476-481`), the reload vocabulary (`:464-468`), the wire shape, the security model, and what a laptop that sleeps for over a minute now does.
  - `docs/SELF_HOSTED_EVENT_SERVER.md:255-266`: a restarted server no longer requires restarting agents that run this change. Older agents still do. The `:260` line also gets corrected: `/health` does emit `process_id` (`local.ts:451`), but no launcher code matches it to a child (only `tests/test_event_server_launch.py:171,199` read it).
  - `skills/bobi.md:79-83` (restart and replay guidance) plus a rollout note in its upgrade section.
  - A dated amendment under D10 in the plan file, covering its restatements at `:397`, `:439`, `:477-478`, `:579` and `:675`. Plan test 6 (`:579`) was never built.

Out:

- Keeping events across a long outage: a longer window (D5), or a local journal of received events.
- A status surface for sessions that still strand (D6).
- Changing `event-server restart` so it can stop a shared server another agent started; the rollout note documents the workaround.
- Telling the old Durable Object when the Worker replaces its deployment (see E10).
- The Slack Socket Mode rollback guidance (`docs/SELF_HOSTED_EVENT_SERVER.md:80-81`, `docs/EVENT_SERVER.md:359`, `skills/slack-setup.md:154`) keeps "restart every agent": that step exists because a running agent still holds the revoked app token in memory, not because of registration.

## Technical approach

### Removal records (S1)

A new record type in `core.ts`:

```ts
interface RemovalRecord {
  key_hash: string;      // sha256Hex(api_key) (core.ts:503); the lookup key
  deployment_id: string;
  reason: "evicted" | "superseded";
  removed_at: string;    // ISO 8601, UTC
  last_seq?: number;     // evictions only: highest sequence number assigned
}
```

- Two storage-adapter methods, `putRemovalRecord(record)` and `getRemovalRecord(keyHash)`, follow the hashed ingest-token precedent (`core.ts:308-309`: "the store never sees a plaintext token").
- `key_hash` is computed once, at registration, before the supersede block, and stored on the deployment record.
  Today that block is atomic on the local server, because every adapter call there resolves without yielding to I/O; a WebCrypto digest inside it would yield and let a concurrent same-name registration interleave.
  Worker records created before the upgrade lack the field, and the Worker computes it on demand.
- Worker: one KV key per reason, `removal:superseded:<hash>` and `removal:evicted:<hash>`, with a 1-year `expirationTtl` (precedent: `webhook_delivery`, 7 days, `event-server/worker/src/index.ts:206-212`).
  A lookup reads `superseded` first: a replacement is final, and a later eviction record cannot override it.
- Local: two insertion-ordered maps, one per reason, each capped at 10,000 with the oldest dropped first.
  There is no age sweep: an absent id answers `unknown` anyway.

Writers:

- Replacement: `handleRegisterDeployment` writes `superseded` for `prior` before removing it (`core.ts:1446-1451`), shared by both backends.
  The local server logs `superseded deployment <old id> (<name>) by <new id>`, as it logs evictions (`local.ts:849`).
- Local eviction: `evictStaleDeployments` re-checks, right before removing each one, that the deployment still exists and is still disconnected.
  It writes `evicted` with `last_seq = nextSeq - 1`.
- Worker eviction: the alarm re-checks `getWebSockets().length` immediately before writing and deleting, since an upgrade can be accepted while it awaits KV (`deployment-session.ts:246-286`).
  It writes `evicted` with `last_seq = next_seq - 1`.
  It deletes `deployment_name:` only if that record still names this id: today the delete is unconditional (`:283`), so it can drop a newer same-name registration's index.
- An explicit `DELETE` writes nothing: its owner removed it on purpose, and transient reply channels deregister after every `ask`, so a record per `DELETE` would be churn with no reader.

### One answer for a failed lookup (S2)

`authenticateDeployment` stays as it is. On a miss, a new core function picks the response:

1. A removal record for `sha256Hex(key)` with a matching `deployment_id` (`superseded` first): `410` with its reason.
2. Otherwise, on a storage adapter that declares itself authoritative for absence (only the local in-memory one), an id that is not a live deployment: `410` with reason `unknown`.
3. Otherwise the existing `403 {"error":"unauthorized"}`. A wrong key for a live deployment always ends here.

It is used by `handleUpdateSubscriptions` (`core.ts:1491-1492`), the Worker upgrade (`index.ts:644-646`) and the local upgrade (`local.ts:729-739`).
Every `410` carries the reason twice: in a `Bobi-Deployment-Gone: <reason>` header and in the JSON body, always with `Content-Length`.
websocket-client reads a rejected handshake's body only when `Content-Length` is present, and parses its headers always (`websocket/_handshake.py:144-157`, the same in 1.9.0 and the 1.7.0 floor).
The local upgrade writes a complete `HTTP/1.1 403` or `410` response with `Content-Length` and `Connection: close`, then ends the socket.

Wire shape:

```json
{"error": "deployment_gone", "reason": "evicted", "removed_at": "2026-11-16T15:02:11.000Z", "last_seq": 57}
{"error": "deployment_gone", "reason": "superseded", "removed_at": "2026-11-16T15:03:40.000Z"}
{"error": "deployment_gone", "reason": "unknown"}
```

- `reason`: `evicted` (idle eviction), `superseded` (replaced by a same-name registration in the same bubble), or `unknown` (an in-memory server with no record of the id).
- `removed_at`: when the server removed the deployment; absent for `unknown`.
- `last_seq`: the highest sequence number the server assigned to the old deployment; evictions only.

Both servers' `GET /health` gain `"capabilities": ["deployment_gone"]`.

### Client boot (S3)

- One parser in `bobi/events/protocol.py` reads a `410` (header first, then body) for both `_put_subscriptions` (`bobi/subagent.py:2154`) and the handshake. A reason it cannot read counts as ambiguous.
- `_put_subscriptions` raises `DeploymentGone(reason, removed_at, last_seq)` before `raise_for_status`, the way it already parses `unauthorized_topics`, and `_sync_saved_deployment` lets it through.
- `_start_event_subscription` gains `replacing=None`, the gone identity handed over by a running session (below). Under `_subscription_apply_lock`, which the boot branch already holds (`:2304-2310`), it then:
  - **No saved identity:** registers, as today.
  - **`replacing` given, and the saved file names a different deployment:** another process registered this session after this one. It stands down: no PUT, no registration, no adoption, and an ERROR once.
  - **Otherwise:** it syncs the saved identity:
    - `evicted` or `unknown`: `_register_with_retry`. That already joins the bubble, or re-mints one the server forgot (`ensure_bubble`, `bobi/events/server.py:984`, `:994-998`). It saves the new identity and deletes the old cursor (`bobi/subagent.py:2057`), because a new deployment starts a new sequence.
      The lost count is `max(0, last_seq - max(saved cursor read before deletion, replacing.enqueued_floor or 0))`.
      The recovery message is logged once, after registration succeeds, with both ids.
    - `superseded`: stands down, raising `SubscriptionSuperseded`. `Session` treats that as terminal and starts no background retry.
    - A 403: one `GET /health`. With no `deployment_gone` capability, the old-server warning; otherwise the narrow-reset warning. It then retries as today.
    - Anything else: as today.

### Client running (S4)

- **The event client** (`bobi/events/client.py:188`):
  - A handshake rejected with a `deployment_gone` answer makes it stop reconnecting. It records the answer with `enqueued_floor = _max_enqueued_seq`, then calls its `on_identity_gone` callback once, on a new thread.
  - A client with no callback keeps today's loop: the reply channels and the login probe, both short-lived.
  - The reconnect delay is capped at 10 s with ±20% jitter, instead of 60 s (`client.py:394-396`). Today an outage that ends at 35 s is not retried until about 63 s, after the Worker's 60 s alarm.
- **`Session`** attaches its handler under `_sub_lock` after it publishes the subscription.
  Attaching to a client that is already gone fires at once: the deployment can vanish between the boot PUT and the handshake.
  The handler ignores any client that is not the current subscription's.
  - `evicted` or `unknown`: retire the subscription, stop it, and start the existing background retry (`bobi/session.py:1813`) with `replacing` set.
  - `superseded`: retire, stop, ERROR once, no retry.
- **`retire()`** comes first. It turns the old client's `ack_through` (`client.py:341`) and the old drain's watermark reporting (`bobi/events/drain.py:171-174`) into no-ops.
  Messages already in the inbox still run, but their late acknowledgements belong to the dead deployment's sequence and would otherwise overwrite the new deployment's cursor.
  An acknowledgement that landed before `retire()` is erased when the registration deletes the cursor.
  This assumes an acknowledgement already past the retired check finishes within milliseconds, well before the retry's first 2 s wait (`bobi/session.py:159`).
- **`Subscription.stop()`** removes the reload controller only if the registered controller is still its own. Today it pops by session name (`bobi/subagent.py:1763-1764`, `:1803-1805`), so a late stop of the old subscription would remove the new controller.
- **The running client is never rebuilt for an ambiguous failure.** A rebuilt client has no memory of what it already queued, so it would replay events still waiting in the inbox (the reason for `_max_enqueued_seq`, `client.py:241-249`).
- **Accepted:** the hand-over builds a new auto-dispatch reactor, so its cooldowns reset, exactly as they do on a process restart today.

### Supervisor admin listener (S5)

The listener passes `on_identity_gone`.
On `evicted` or `unknown` it repeats its own registration, which always registers fresh, and builds a new client; on `superseded` it logs an ERROR once and stops.
`start()` now deletes `admin-cursor.json` before registering.
Every start registers a new deployment with a new sequence, so today a cursor left from the previous one can suppress replay after the first reconnect.

### Reload route and CLI (S6)

- `_apply` (`bobi/subagent.py:2194`) maps the `410` to `{"status": "rejected", "error": "deployment_gone", "reason": "<reason>", "removed_at": "<time>"}`.
  `POST /subscriptions/reload` then answers 200 with that body instead of 503.
- CLI, `evicted`, exit 0:
  "Saved. The event server dropped this session's registration at 15:02:11Z because the session was disconnected for more than 60 s. The session registers again on its own when it reconnects, within about a minute, and applies this change then. Check with `bobi agent <name> subscriptions list`."
  For `unknown`, the second sentence becomes "The event server has no record of this session's registration (it restarted, or dropped the registration too long ago to remember it)."
- CLI, `superseded`, exit 1:
  "Saved, but not applied: another process registered this session at 15:03:40Z and replaced this manager's registration, so this manager no longer receives events. Keep one process: stop this manager, or stop the other one and then run `bobi agent <name> restart`."
- The 403 text (`bobi/cli.py:3655-3664`) is rewritten.
  A current server reports a dropped registration as such, so a bare 403 now means an older server, a Worker record older than a year, wiped storage, or a wrong credential.
  The new text names the matching fix and the narrow reset.

### Compatibility and rollout

- No protocol version change.
  The new status appears only on requests that already fail, and both client generations treat it as a failure.
  The new `/health` and body fields are additive (`docs/EVENT_SERVER.md:146-147`).
- New client, old server: behavior is today's, plus the old-server warning that names the fix.
- Old client, new server: the `410` lands in "unexpected subscription response (HTTP 410); check server configuration", and the identity is kept, as today.
  Its reload answers 503 instead of the 403 text. The fix is to upgrade the agent.
- A local event server that is already running keeps its old code until it restarts, because `ensure_running` attaches to any healthy server without checking its release (`bobi/events/server.py:617-618`).
  The rollout note says to restart the local event server after upgrading. On a shared server, do it from the agent that started it, or stop the pid in its `/health` and start an agent.
  `event-server restart` run from another agent finds no pid file of its own, so its stop half does nothing and its start half re-attaches (`bobi/cli.py:3937-3950`; inferred from code).
- The hosted Worker must be redeployed (`npx wrangler deploy`) before T1 and T3 recover there. Whoever runs a Worker does this at release, and the PR description carries the same note for the changelog.

### Security

- Only a caller presenting the old api key matches a removal record, and only for that deployment id. Everyone else gets the same opaque 403, so a record discloses nothing to a non-owner. A record stores a hash, never the key.
- `unknown` reveals that an id is not live on an in-memory server. That server's unauthenticated `/health` already shows this, since it lists every live id (`local.ts:460-466`).
- Re-registration goes through the normal path (bubble join, resource-grant checks), so a `410` grants nothing new.
- Records are bounded by a KV TTL on the Worker and by per-reason caps locally, so a flood of short-lived registrations cannot grow storage without limit, and cannot push `superseded` records out with `evicted` churn.

## Edge cases

- E1. Two processes holding one session.
  - Who it reaches: #1102's per-runtime lease stops two managers in one runtime. Duplicates remain possible across hosts that share state, and for worker sessions.
  - Guarantee: neither process registers again on `superseded`, and a hand-over never adopts an identity another process saved. So through this change they can neither replace each other in a loop nor end up sharing one registration.
  - Serialization: competing replacements are serialized within a process (`_subscription_apply_lock`). Across processes they converge after one wasted registration, and the loser gets `superseded` and stops.
  - A narrow race remains on the shared state file, where the last writer wins. It can leave the survivor's file naming the superseded id; its next boot stands down with the ERROR, and the narrow reset recovers it.
- E2. Worker storage is eventually consistent.
  Just after an eviction, a request can miss both the deployment and the new record. It gets a 403 and retries, and a later retry sees the `410`.
  A request that still sees the deleted deployment behaves as today.
- E3. A record past its TTL, or out of the local cap.
  The Worker reverts to today's behavior.
  Locally, the id is still absent, so the answer is `unknown` and the client still recovers. `superseded` records have their own cap, so ordinary eviction churn cannot push them out.
- E4. A stale bubble after a local server restart. `_register_with_retry` re-mints it through `ensure_bubble`'s compare-and-swap, so all sessions of one agent end up in one new bubble (`bobi/events/server.py:994-998`).
- E5. A deployment deleted with its own key. The local server answers `unknown` (register again); the Worker answers 403 (keep). Only reply channels and the login probe send `DELETE` (`bobi/inbox.py:306`, `bobi/auth_bootstrap.py:442`), never a session.
- E6. The session stops during a hand-over. The handler checks `_sub_retry_stop` under `_sub_lock`, and the retry loop already discards a subscription that arrives after `stop()` (`bobi/session.py:1846-1852`).
- E7. The deployment disappears before the session holds its subscription. The client records the answer, and attaching the handler fires it.
- E8. Client state needs no migration: state and cursor files keep their format.
- E9. A standalone server is single-node by design (`docs/SELF_HOSTED_EVENT_SERVER.md:247`). Run as several replicas behind a load balancer, `unknown` would be wrong; that setup is already unsupported.
- E10. When the Worker replaces a deployment, the old Durable Object is not told, so the replaced client stays connected and receives nothing until Cloudflare next cycles the socket. It then gets `superseded`. This is pre-existing; the local server closes the old socket at once (`local.ts:253-262`).

## Alternatives considered

- Re-register on any 403. Rejected: a wrong guess deletes a live same-name deployment and its buffered events, and two same-name processes would loop.
- A client-only check of the local `/health` deployment list. Correct for in-memory servers, but no help on the Worker, and it puts one decision in two places. The server's `unknown` answer is the same check, made once, where the data lives.
- The server re-creates the deployment for the old key holder. That keeps the identity, but the Durable Object's storage is gone after an eviction. Resource grants would also need re-checking server-side, and a restarted local server has nothing to re-create from.
- Tell the old Durable Object about a replacement through an internal call, so it records the reason and closes its socket at once. It would fix E10 and make the Worker's records strongly consistent, at the cost of new Worker surface. The per-reason keys already make `superseded` final.
- Tag the cursor file with its deployment id. `retire()` and the cursor deletion already close the in-process hazard, and the cross-process case is E1.
- Deregister on a normal stop. That breaks #1081's quick-restart replay, which needs the deployment to survive.

## What this does not close

- Events that reach a deployment while its session is away are still lost when it is evicted, and events sent after the eviction reach no one. The fix restores delivery from re-registration onward and says what it can count. Keeping those events needs a longer window (D5) or a local journal; #1081 lists both as follow-up options.
- Sessions still strand when the server cannot answer:
  - every running local event server, until it is restarted, which is common right after an upgrade;
  - a Worker record older than a year, or wiped Worker storage;
  - a corrupted credential;
  - a deployment its owner deleted, on the Worker (E5).

  Each gets a message naming its fix: restart or redeploy the server, or the narrow reset.
  A replaced duplicate stops by design.
- A session that still strands reads as running on the agent page (D6).

## Verification plan

Python unit tests:

- `tests/test_subscription_recovery.py` gains `410` cases.
  - `evicted` and `unknown` register exactly once, save the new identity, delete the cursor, log the lost count, and hand the new id to the client.
  - `superseded` stands down.
  - A `410` whose reason is readable only from the header, only from the body, or from neither: the first two act on it; neither counts as ambiguous.
  - Every existing failure (200, 400, 401, 403, 404, 429, 5xx, timeout, malformed) still never registers.
  - A 403 from a server without the capability logs the old-server warning; with it, the narrow-reset warning.
- The `replacing` guard: the same saved id registers; an absent file registers; a different saved id stands down without a PUT or a registration.
- `tests/test_event_client.py`:
  - A gone handshake calls `on_identity_gone` once, on another thread, and stops.
  - A 403, a refused connection and a `410` with an unreadable reason keep reconnecting.
  - A retired client's `ack_through` writes no cursor and sends no frame.
  - The reconnect schedule stays within 10 s ±20%.
- `tests/test_session.py`:
  - The hand-over retires and stops the old subscription and starts the retry.
  - Attaching to an already-gone client fires the handler; a stale client is ignored.
  - A `stop()` racing the hand-over leaves no live client.
  - A late stop of the old subscription keeps the new reload controller.
- `tests/test_subscription_reload.py` and `tests/test_subscriptions_cli.py`: the reload body, the exit codes and all three CLI texts, including the rewritten 403.
- The admin listener: recovery registers again and deletes its cursor; `superseded` stops it.

Event-server tests (vitest), core and local:

- A replacement writes `superseded`.
- The lookup answers:
  - a recorded key and id: 410, with header, body and `Content-Length`;
  - a recorded key with another id: 403;
  - a live id with a wrong key: 403;
  - an absent id on the local adapter: 410 `unknown`;
  - an absent id on a non-authoritative adapter: 403.
- `superseded` wins over a later `evicted` record for the same key.
- Concurrent same-name registrations leave exactly one live deployment.
- The sweep skips a deployment that reconnected between collection and removal.
- The upgrade returns complete 403 and 410 responses. The per-reason caps hold. `/health` lists the capability.

Worker (the Wrangler suite):

- The alarm writes the record under its own key with the TTL, after re-checking sockets.
- The name index is deleted only when it is still the alarm's own.
- The routes return the 410.
- The eviction delay becomes overridable for tests, as `BOBI_ES_EVICTION_DELAY_MS`, mirroring the local variable (`local.ts:77`).
- Wrangler needs Node 22 and this host has Node 20, so PR CI proves these legs, not a local run.

Integration tests against real servers, with a short eviction window and no Claude leg (the change is brain-agnostic):

- T1 against the local server and against `wrangler dev`, through `tests/integration/test_event_server.py`, the one file CI runs against that backend (`.github/workflows/ci.yml:443-448`).
- T2 at boot: two runtime roots share one server.
- T2 while running: restart the local server under a running manager and under a second agent's running session. Both register again and receive an event published afterwards.
- T3 with a real `Session` (stub brain) behind a real TCP relay that closes both halves. The test first asserts that the server logged the disconnect: an earlier relay attempt here left the server unaware. It then asserts:
  - recovery, and an event published afterwards reaching the session;
  - an old message finishing after the hand-over leaves the new cursor untouched;
  - `POST /subscriptions/reload` answers `applied`.
- E1: process B's registration replaces process A's. A stands down and does not adopt B's identity, and the deployment count stays stable over several reconnect cycles.
- `tests/integration/test_interrupted_event_recovery.py:201-238` changes on purpose:
  - a wrong key for a live deployment still gets 403 and keeps its state;
  - an explicitly deleted deployment now answers `unknown` on the local backend (E5).
- `test_interrupted_event_recovery.py:64` (the #1114 archive) and `:123` (an interrupted event completes in the successor) stay green unchanged. They prove #1081's replay still works after a quick restart.

Each new test is shown to fail with its fix reverted, by a named mutant.

## Implementation plan

1. Failing tests first: the Python unit cases, the vitest cases, and the integration reproductions.
2. Server core: the record type, the key hash at registration, the adapter methods, the lookup decision, the replacement writer and log.
3. Local server: the per-reason maps, the eviction writer with its re-check, complete HTTP responses on upgrade, the capability.
4. Worker: the per-reason KV keys, the alarm writer with its re-checks, the conditional name delete, the upgrade route, the capability, the test override.
5. Client protocol and boot: the shared parser, `DeploymentGone`, `replacing`, standing down, the lost count, the messages.
6. Client running: the gone state and callback, the 10 s cap, `retire()`, the conditional unregister, the `Session` handler.
7. The supervisor admin listener.
8. The reload route and the CLI texts.
9. Docs, the rollout note, and the dated D10 amendment.

## Review record

Three spec reviews ran as fresh reviewers, against the code at `5ed76cf4`, through three lenses: engineering, operator experience, and scope. All three returned "approve with changes". Their findings are folded in above:

- F1 (engineering): a hand-over could adopt an identity another process saved, and the two would then knock each other's connection off. Fixed by the `replacing` guard (S3) and the restated E1.
- F2 (engineering): a Worker eviction could overwrite a `superseded` record. Fixed by per-reason keys with `superseded` read first, the alarm re-check, and the conditional name delete.
- F3 (engineering): websocket-client drops a rejected handshake's body unless `Content-Length` is present. Fixed by the header plus `Content-Length`, one parser, and real-socket tests.
- F4 (engineering): the supervisor admin listener has the same failure. Now in scope (S5, D2).
- F5 (engineering, scope): the tests missed the two-process case, the Worker end to end, the reload controller surviving the hand-over, and a real `Session`. All added, plus the Worker delay override.
- F6 (engineering): the answer could arrive before the session holds its subscription. Fixed by attaching the handler after publishing.
- F7 (engineering): old and new subscriptions share the reload controller and the watermark reporting. Fixed by the conditional unregister; `retire()` covers the reporting; the reactor reset is accepted.
- F8 (operator experience, blocker): "stop the duplicate process" left the agent deaf. Replaced by a superseded process that stops its subscription, an ERROR with pid and host, "keep one process" guidance, and a server log line.
- F9 (all three): the lost count both over- and under-counted. `last_seq` now goes on the wire, the client reports "up to N", and the uncounted window is stated.
- F10 (operator experience): stranded sessions were pointed at the destructive `--fresh`. Fixed by documenting the narrow reset and rewriting the 403 text.
- F11 (scope, operator experience): the reporter's still-running old server would have produced the same message after the upgrade. Fixed by the `/health` capability, a warning that names the fix, and the rollout notes.
- F12 (scope): T2 was understated, because sessions running when a shared server restarts strand too. Restated and tested.
- F13 (scope): the reconnect backoff overshoots the eviction window. The delay is now capped at 10 s.
- F14 (scope): the eviction window and visibility were not posed. Now D5 and D6.
- F15 (engineering): WebCrypto inside local critical sections. The key hash is now computed at registration, and the sweep re-checks.
- F16 (engineering): the Worker alarm can race a reconnect (pre-existing). It now re-checks.
- F17 (engineering): the local cap could push out `superseded` records. Fixed by per-reason caps and the corrected E3.
- F18 (all three): citation and doc-list corrections:
  - `process_id`, and the `:146-147` "additive" sentence;
  - the reload vocabulary, `skills/bobi.md`, and the plan's other D10 restatements;
  - #1056 is closed by #1102's lease;
  - the Slack rollback text is kept.

Declined, with reasons under Alternatives: telling the old Durable Object about a replacement, and tagging the cursor file.

Not run: an independent second-model review. Codex is not logged in on this host, and `aichat` has no gateway configured. The workflow's plan-review step runs next.
