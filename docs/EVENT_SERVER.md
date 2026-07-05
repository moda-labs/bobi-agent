# Event Server

The event server is bobi's pub/sub bus: it ingests webhooks and agent-emitted
events, routes them by topic to subscribed agents over a WebSocket, and replays
anything missed during downtime. It is **one TypeScript core that runs two ways** -
a Cloudflare Worker in production, or an embedded local Node process for local
runs - plus a Python client (`bobi/events/`) that every agent session uses.

## Mental model

```
       external webhooks
      (GitHub, Slack, Linear)
                │
                ▼
         ┌──────────────┐    routed by topic, delivered over
         │ event server │    each agent's outbound WebSocket
         │ Worker/local │ ───────────────────────────────────▶  agent sessions
         └──────────────┘                                        (Claude / Codex,
                ▲                                                  subscribed by topic)
                │
       POST /events/{topic}
      (monitors, inbox/reply, agent-emitted)
```

Two kinds of input - external webhooks and internal `POST /events/{topic}` publishes
(monitors, inter-agent messages, agent-emitted events) - converge on the event
server and route through one path. The **instance reaches out**: the agent opens the
WebSocket and receives events; nothing reaches into the instance.

## Architecture: one core, two runtimes

All domain logic lives in `event-server/src/core.ts` as pure functions that take a
`StorageAdapter` and return a transport-neutral `HandlerResult {status, body}`.
The core never touches a transport - it does not know `Response` vs `res.end`, KV
vs an in-memory `Map`, or a Durable Object vs an in-process socket. The seam is the
`StorageAdapter` interface (`core.ts`): deployment CRUD, the subscription index,
bubbles, Slack workspaces, resource grants, and the one method that fans an event
out - `deliver(event)`. Each runtime supplies its own implementation; everything
else is shared.

### Remote: Cloudflare Worker + KV + Durable Object

`event-server/src/index.ts` is the Worker entry. It implements `StorageAdapter`
over Workers KV (records keyed `deployments:<apiKey>`, `subscriptions:<key>`,
`bubble:<id>`, `resource_grant:<svc>:<res>:<bubble>`, ...), routes by URL, and fans
out through a **`DeploymentSession` Durable Object** (`deployment-session.ts`) -
one DO instance per deployment id. The DO owns the live WebSocket(s), assigns each
event a monotonic `seq`, mirrors events to KV (`events:<id>:<seq>`, 48h TTL) for
replay, tracks the acked cursor, and evicts itself ~60s after the last socket
disconnects. The Worker itself is stateless per request; durability and socket
affinity live in KV + the DO. Deployed with `wrangler` (`wrangler.jsonc`): needs a
KV namespace `EVENTS`, the DO binding, and an `INTERNAL_DO_SECRET`.

### Local: embedded Node + in-memory store

`event-server/src/local.ts` is the Node entry. A handful of module-level `Map`s
are the entire store, and a `LocalDeployment` folds together what the Worker splits
across KV and the DO: subscriptions, the `seq` counter, a capped in-memory event
buffer, and the live `Set<WebSocket>`. `deliver()` runs the **same** core
admission/routing logic, then `ws.send()`s directly - no DO hop. It binds
**loopback `127.0.0.1:8080` by default** (`BOBI_ES_BIND` / `BOBI_ES_PORT`).

**Implicit startup.** You never start the local server by hand in normal use. When
a session starts (`bobi/subagent.py`), if `event_server_url` is empty - or set to a
loopback address - it calls `ensure_running()` (`bobi/events/server.py`), which
health-checks `http://localhost:<port>/health`, and if nothing is up, locates the
bundled `event-server/`, builds `dist/local.js` if stale
(`npm run build:local`, an esbuild bundle), and spawns `node dist/local.js`. A
genuinely remote `https://` event server skips all of this - the container never
needs Node. (`bobi agent <name> event-server start` calls the same path as a
convenience.)

### The client

`bobi/events/client.py` (`EventServerClient`) is the outbound WebSocket client. It
derives `wss://` from the `https://` base, connects to
`/deployments/<id>/subscribe?last_seen=<cursor>` with `Authorization: Bearer
<api_key>`, and reconnects with capped exponential backoff. Received `event` /
`replay` frames are pushed onto a queue; `bobi/events/drain.py` batches them, runs
the auto-dispatch reactor, delivers formatted messages into the session inbox, and
**only then** advances the cursor (`cursor.json`). A transport ping alone can't
prove a hibernated Cloudflare socket is still being fed, so the client also runs an
app-level heartbeat and force-reconnects + re-subscribes if it goes "deaf".

## The event envelope

Every delivered event is a v2 `NormalizedEvent` (`core.ts`), wrapped on the wire as
`{type: "event" | "replay", data: <event + seq>}`:

```jsonc
{
  "v": 2,
  "id": "evt_...",               // delivery / event id
  "source": "github",            // github | slack | linear | monitor | inbox | reply | custom
  "type": "github.pull_request", // e.g. slack.mention, inbox/director, support.email
  "timestamp": "2026-06-27T14:30:00Z",
  "topics": ["github:moda-labs/bobi"], // the routing keys (see catalog below)
  "delivery": "bulk",            // "chat" (slack) | "bulk" (github/linear/monitor)
  "text": "...",                 // human summary the agent reads
  "conversation": "slack:T1:channel:C9:thread:12.34", // chat events: reply address (#618)
  "fields": { "number": 42, "action": "opened" }, // flat routing/context map
  "payload": { /* raw webhook body or publisher payload, passed through */ },
  "bubble_id": "bub_...",        // set for authenticated /events publishes; UNSET for webhooks
  "seq": 17                      // assigned per-deployment at store time
}
```

The server extracts routing fields and passes `payload` through untouched - agents
are models and interpret raw payloads directly. Routing context (repo, channel,
`thread_ts`, ...) is delivered in `fields`, not at the top level.

**Conversation references** (`conversation.ts`, mirrored by `bobi/conversation.py`).
Chat adapters set a channel-agnostic reply address on every chat event:
`<source>:<scope>:<chat_type>:<chat_id>[:thread:<thread_id>]`, where `scope` is the
platform's tenancy unit (Slack team id). The agent echoes it back verbatim -
`bobi reply <conversation>` or `POST /channels/send`
`{conversation, text, mode: post|update|final, edit_ref?, files?}` - and the gateway
parses it to address the platform send. Text is raw markdown; formatting is the
gateway's job (Slack: native `markdown_text`), and `mode: final` resolves a response
context (edit `edit_ref` when given, else post, then clear the typing indicator).
`POST /channels/typing` sets/clears the thinking indicator and `GET /channels/history`
reads a conversation's messages; both share the send path's auth and tenancy
boundary. Channel capabilities (edit, typing, files, length budget) are declared per
adapter in `event-server/src/channels.ts` (`ChannelDescriptor`); degradation is the
gateway's job, not the caller's. Only adapters build refs and only the gateway parses
them; the agent never assembles platform routing fields.

## Ingestion: getting events in

**Webhooks** (the unified pipeline, #639). Every inbound webhook runs through one
pipeline in the shared core, identical on the Worker and the local server:

```
route (/webhooks/<source>) -> verifier -> normalizer -> deliver()
```

A source registers a **required** verify slot plus a normalizer in
`WEBHOOK_SOURCES` (`event-server/src/core.ts`); the verify field is non-optional
by type, so a route cannot exist without verification by construction. Transports
never stitch verification per-route. Verifiers run over the exact wire bytes; an
unconfigured provider secret admits that provider unverified (zero-config local
development), and a configured one rejects bad or missing signatures with 401.
Normalizers (`event-server/src/adapters/`) derive the routing key **from the
signed body, never from client input**:

- **GitHub** (`POST /webhooks/github`): `type = github.<event>`, key
  `github:<owner>/<repo>` from `repository.full_name`. Signature
  (`X-Hub-Signature-256`, HMAC-SHA256) is verified when `WEBHOOK_SECRET`
  (local: `BOBI_ES_WEBHOOK_SECRET`) is set.
- **Slack** (`POST /webhooks/slack`): the pipeline's pre-verify stage handles the
  `url_verification` challenge and retry dedup (both must run before the signature
  check); then verifies the `v0=` signature within a ±300s window, with the signing
  secret resolved per authoring app (`api_app_id`), falling back to
  `SLACK_SIGNING_SECRET` (local: `BOBI_ES_SLACK_SIGNING_SECRET`). Normalization
  runs through the Chat SDK bridge (`adapters/chat-sdk-slack.ts`, #628); the
  hand-rolled `adapters/slack.ts` normalizer remains as the golden parity
  reference until the bridge has soaked. Maps to `slack.mention` / `slack.dm` /
  `slack.thread_reply` and filters our own bots' messages.
- **Linear** (`POST /webhooks/linear`): `type = linear.<type>.<action>`, key
  `linear:<TEAM_KEY>`. Signature (`Linear-Signature`, HMAC-SHA256 of the raw body)
  is verified when `LINEAR_WEBHOOK_SECRET` (local: `BOBI_ES_LINEAR_WEBHOOK_SECRET`)
  is set, with replay rejection via the signed `webhookTimestamp` (±300s).

**Generic topic endpoint** (`POST /events/{topic}`). Monitors, lifecycle emits,
inter-agent inbox/reply, and any agent-emitted event publish here. It is
**bubble-signed and mandatory** (see Security) and rejects bodies carrying global
routing fields (`repo` / `team_key` / `workspace`) - those are webhook-only. `{topic}`
may contain slashes (e.g. `inbox/director`).

## Topic subscription model

A **topic** is the routing key; a **subscription key** is the topic, optionally
prefixed by the subscriber's bubble id for tenant isolation (see Security).

### Topic catalog

| Shape | Origin | Example |
|---|---|---|
| `github:<owner>/<repo>` | GitHub webhook | `github:moda-labs/bobi` |
| `linear:<TEAM_KEY>` | Linear webhook | `linear:MOD` |
| `slack:<team>` | Slack webhook (no app id) | `slack:T0ABC` |
| `slack:<team>:app:<app_id>[:<channel>]` | Slack webhook (app-qualified) | `slack:T0ABC:app:A123:C0XYZ` |
| `inbox/<session>` | inter-agent fire-and-forget | `inbox/director` |
| `reply/<uuid>` | transient ask-reply channel | `reply/9f3a...` |
| `<type>` and `<source>/<type>` | monitors / agent publishes | `support.email`, `monitor/support.email` |
| `agent/session.completed` (+ bare `session.completed`) | sub-agent lifecycle | |

`github:`, `linear:`, and `slack:` are **global** topics (cross-bubble, gated by
resource grants). Everything else is **bubble-scoped**. Monitors and lifecycle
events are published to both the bare `<type>` and the `<source>/<type>` form, and
clients subscribe to both, for cross-version compatibility.

### Declaring subscriptions

Resolved in `bobi/events/subscriptions.py` + `adapters.py`:

1. **Explicit** - `agent.yaml` top-level `subscribe:` after environment interpolation.
2. **Auto-detected** from `services:` entries with `events: true`:
   - **github** from the project's `git remote` (`owner/repo`); for a director over
     many repos with no root remote, from each immediate child repo.
   - **slack** from the bot token (`auth.test` -> team + app id), scoped to any
     configured `channels:`.
   - **linear** from the API key (teams query -> one `linear:<KEY>` per team).
3. **Fallback** - the project directory name.

On top of that, `bobi agent <name> start` adds any `--subscribe` extras, every
effective monitor's event topic, the sub-agent lifecycle topics, and the session's
own `inbox/<self>`.

### Delivery and routing

`subscriptionKeysForEvent` (`core.ts`) is the single source of truth used
identically at registration and delivery, so a publish and a subscription only
match when their keys agree:

1. Take the event's `topics` (or `[type]` if none).
2. Namespace each: **global topics are never namespaced**; a non-global topic with
   a `bubble_id` becomes `<bubble>:<topic>`; a non-global topic with no bubble stays
   bare (and matches no namespaced subscription, so it reaches nobody).
3. For each key, look up subscriber deployment ids. Matching is **exact** - there is
   no prefix matching, so channel scoping is done by emitting multiple explicit
   topics, not by prefixes.
4. For a **global** key, a subscriber is admitted only if its bubble holds a
   matching resource grant (fail-closed; see Security). Non-global keys admit every
   indexed subscriber.

Admitted deployments are then delivered to: on the Worker, via the per-id
`DeploymentSession` DO; locally, by direct `ws.send()`. A per-`(deployment,
conversation)` circuit breaker pauses delivery if an agent's own output loops back
without a human event in between (it exempts `inbox/*` and `reply/*`).

### Replay and catch-up

Replay is cursor-based, per deployment (each deployment has its own `seq` space, so
sessions must not share a cursor):

- The client stores `last_seen` in `cursor.json` and sends it as `?last_seen=<seq>`
  on connect.
- The server replays each buffered event with `seq > last_seen` (from KV on the
  Worker, the in-memory buffer locally) as `{type: "replay"}` frames, then a
  `{type: "connected", next_seq}` frame. Buffer TTL is 48h.
- The cursor advances only after the drain delivers a batch to the inbox, so a
  crash before delivery loses nothing - the server replays from the unchanged
  cursor on reconnect.
- Caveat: a brand-new deployment connecting with `last_seen = 0` gets **no** replay,
  so an event published during its connect window can be missed. The blocking-ask
  path avoids this by subscribing and waiting for the `connected` frame before the
  publish.

## Inter-agent messaging

Agents message each other over the **same** bus - no per-session HTTP server
(`bobi/inbox.py`):

- **Fire-and-forget** (`message`): publish to `inbox/<target>`; the target's
  `inbox/<target>` subscription delivers it straight into its in-process inbox.
- **Blocking ask** (`ask`): the sender opens a transient `reply/<uuid>` channel
  (a throwaway deployment registered **into the instance's bubble**, subscribed to
  `reply/<uuid>`), waits for `connected`, then publishes with `reply_to`. The target
  replies on that channel; the sender correlates by id and tears the channel down.
  A reply is only honored if `reply_to` starts with `reply/`, so wire input can't
  redirect a reply into another inbox or a broadcast topic.

`bobi agent <name> ask` / `message` route through this same path.

## Security

Two independent layers protect the bus: a **trust bubble** (HMAC) proves an event
belongs to an instance, and **resource grants** prove an instance may receive a
given external topic.

### Trust bubbles (HMAC)

A *trust bubble* is minted once per named-agent start; every deployment (each
session, sub-agent, and reply channel) joins it. Non-global topics are namespaced
by bubble id, so cross-bubble injection or eavesdropping on custom topics is blocked
by construction.

Each signed request carries `x-moda-bubble / -algo / -timestamp / -nonce /
-signature`. The signature is `HMAC-SHA256` (hex) over the canonical string, built
identically on both sides (`bobi/events/signing.py`, `core.ts`):

```
{timestamp}\n{nonce}\n{METHOD}\n{path}\n{body}
```

`timestamp` is epoch seconds; `path` is the exact wire path including query; `body`
is the exact transmitted bytes (the client serializes once, compact + key-sorted,
and sends those bytes). The server verifies within a **±300s** window and compares
in constant time. A bubble miss substitutes a dummy key and still runs the full
HMAC, so a missing bubble and a bad signature are timing-indistinguishable; all
failures return an opaque 403.

**Mint vs join.** A request with no signing headers is a **mint**: the server
generates the bubble id + key and returns the key **once, at mint, over TLS** (never
on join). A signed request is a **join** and must verify. Partial signing headers
are hard-rejected, never silently treated as a mint (which would fork a new bubble).
The bubble key lives in `bubble.json` at `0600`; clearing it forces a re-mint. The
client funnels every deployment through one lock-protected `ensure_bubble` so
concurrent first-registrations converge on a single bubble.

**What each operation requires:**

| Operation | Route | Auth |
|---|---|---|
| Mint | `POST /deployments` (no headers) | none - guarded client-side: the client refuses to mint over a non-loopback cleartext URL |
| Join / register | `POST /deployments` (signed) | bubble signature |
| Publish | `POST /events/{topic}` | bubble signature |
| Authorize resource | `POST /resources/authorize` | bubble signature |
| Slack send | `POST /slack/send` | bubble signature (bubble-scoped to its own workspace) |
| Channel send | `POST /channels/send` | bubble signature (same tenancy boundary as `/slack/send`) |
| Channel typing | `POST /channels/typing` | bubble signature |
| Channel history | `GET /channels/history` | bubble signature (covers path + query, empty body) |
| WS subscribe / update subs / deregister | `/deployments/{id}/...` | bearer `api_key` (issued at register) |

The bubble key authenticates *membership*; the per-deployment `api_key`
authenticates a *specific deployment's transport*.

### Proof-of-access: resource grants

Before a bubble may subscribe to or receive a **global webhook topic**, the server
verifies an upstream credential **once** and stores a `ResourceGrant` - storing only
the grant, never the credential:

- **GitHub**: `GET /repos/{owner}/{repo}` with the token must return 2xx (read
  access).
- **Linear**: a teams query must return the specific team key (access to that team,
  not just the org).
- **Slack**: the bubble-signed `/slack/workspaces` registration (proving the bot
  token + signing secret via `auth.test`) doubles as the grant.

Unknown services are default-deny (only `github:` / `linear:` / `slack:` are global).
The grant is enforced at **three points**, all sharing one parser so they cannot
diverge:

1. **Registration** - any ungranted global topic rejects the whole request.
2. **Subscription update** - the same gate on newly added subscriptions.
3. **Delivery** - the authoritative, fail-closed boundary: a global event is
   admitted to a subscriber only if its bubble currently holds the grant, so a stale
   index entry for a revoked bubble is dropped.

The client authorizes its github/linear topics (and registers Slack workspaces)
before it registers subscriptions, dropping any topic whose credential is missing or
rejected so it never trips the server's hard reject.

### Internal Worker to Durable Object auth

On Cloudflare, the Worker stamps internal calls to the DO with a shared secret
(`INTERNAL_DO_SECRET`, header `x-bobi-internal`), constant-time compared. This gates
the DO's `/init` and `/event` endpoints. The WebSocket upgrade to the DO is gated
upstream by the Worker's bearer `api_key` check (DOs are not externally
addressable, reachable only via the Worker binding).

### v1 boundary

Grants prove access, but tenancy is coarse: an inbound global webhook topic fans out
to every granted bubble, and a `linear:<TEAM>` key can collide across two Linear orgs
(the organization id is recorded but not yet used to disambiguate). Binding inbound
webhooks to a specific account is the multi-tenant hardening tracked in **#239**.

Running a shared or public event server is a deliberate step with prerequisites:
serve over TLS and set all three provider webhook secrets (`WEBHOOK_SECRET`,
`SLACK_SIGNING_SECRET`, `LINEAR_WEBHOOK_SECRET`) so every inbound route verifies.

## Key files

- `event-server/src/core.ts` - shared handlers, the unified webhook pipeline,
  routing, HMAC auth, grant filter.
- `event-server/src/index.ts` - Cloudflare Worker entry (KV storage, DO fan-out).
- `event-server/src/local.ts` - local Node entry (in-memory store, direct sockets).
- `event-server/src/deployment-session.ts` - the per-deployment Durable Object.
- `event-server/src/adapters/{github,slack,linear}.ts` - webhook normalizers.
- `bobi/events/server.py` - local-server launcher + bubble mint / grant setup.
- `bobi/events/client.py` - the WebSocket client (connect, replay, heartbeat).
- `bobi/events/{subscriptions,adapters,drain,publish,signing}.py` - subscription
  resolution, delivery to the inbox, publishing, and request signing.
- `bobi/inbox.py` - inter-agent inbox / reply channels over the bus.
