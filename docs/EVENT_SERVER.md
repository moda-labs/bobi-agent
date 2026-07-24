# Event Server

The event server is bobi's pub/sub bus: it ingests webhooks and agent-emitted
events, routes them by topic to subscribed agents over a WebSocket, and replays
anything missed during downtime. It is **one TypeScript core that runs two ways** -
a Cloudflare Worker in production, or an embedded local Node process for local
runs - plus a Python client (`bobi/events/`) that every agent session uses.

## Mental model

```
       external webhooks                  generic ingress
 (GitHub, Slack, Linear, WhatsApp) (alerting, CI, SaaS webhooks,
                │                   via scoped ingest tokens)
                │                               │
                ▼                               ▼
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

All domain logic lives in `event-server/core/src/core.ts` as pure functions that take a
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
health-checks `http://localhost:<port>/health`.
A standard wheel already contains a self-contained `dist/local.js`, its input and output manifest, and fixed third-party notices.
Installed startup validates those files, requires Node.js 20 or newer, removes inherited Node preload and module-search variables, and spawns the bundle directly.
It never invokes npm, installs dependencies, builds JavaScript, or writes inside the installed package.

A writable source checkout uses the same bundle contract with content hashes across manifests, lockfile, TypeScript configuration, root sources, and workspace sources.
A fresh source bundle starts directly.
A stale source bundle validates the ignored installed dependency stamp, uses exact `npm ci --no-audit --no-fund` only when the locked tree needs repair, and runs the single `npm run build:local` command.
A genuinely remote `https://` event server skips all local Node and npm work.
`bobi agent <name> event-server start` calls the same path as a convenience.

The same local server also runs standalone - behind a tunnel on the agent's
machine, or on its own box behind TLS - to receive provider webhooks on
infrastructure you manage. Setup guide:
[SELF_HOSTED_EVENT_SERVER.md](SELF_HOSTED_EVENT_SERVER.md).

The local runtime also owns outbound channel transports.
Discord Gateway is always outbound, and Slack Socket Mode is enabled per app when a bubble-signed workspace registration carries an app-level token.
The hosted Worker has no persistent Slack socket driver.

### The client

`bobi/events/client.py` (`EventServerClient`) is the outbound WebSocket client. It
derives `wss://` from the `https://` base, connects to
`/deployments/<id>/subscribe?last_seen=<cursor>` with `Authorization: Bearer
<api_key>`, and reconnects with capped exponential backoff. Received `event` /
`replay` frames are pushed onto a queue; `bobi/events/drain.py` batches them, runs
the auto-dispatch reactor, and delivers formatted messages into the session inbox.
The inbox completion callback advances the cursor (`cursor.json`) only after the
session finishes a successful model turn. A transport ping alone can't
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
adapter in `event-server/core/src/channels.ts` (`ChannelDescriptor`); degradation is the
gateway's job, not the caller's. Only adapters build refs and only the gateway parses
them; the agent never assembles platform routing fields.

## Ingestion: getting events in

**Webhooks** (the unified pipeline, #639). Every inbound webhook runs through one
pipeline in the shared core, identical on the Worker and the local server:

```
route (/webhooks/<source>) -> verifier -> normalizer -> deliver()
```

A source registers a **required** verify slot plus a normalizer in
`WEBHOOK_SOURCES` (`event-server/core/src/core.ts`); the verify field is non-optional
by type, so a route cannot exist without verification by construction. Transports
never stitch verification per-route. Verifiers run over the exact wire bytes; an
unconfigured provider secret admits that provider unverified (zero-config local
development; counted on `/health` as `webhook_unverified` so a public server
running unverified is visible), and a configured one rejects bad or missing
signatures with 401 (counted as `webhook_bad_signature`).
`/health` also reports the release stamp baked into the deployed Worker:
`release.version`, `release.sha`, and Cloudflare Worker version metadata when
available. The release workflow uses this to fail fast if the fleet
`event_server` URL is still pointed at a different Worker after deploy.
Normalizers (`event-server/core/src/adapters/`) derive the routing key **from the
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
  runs through the Chat SDK bridge (`adapters/chat-sdk-slack.ts`, #628), the
  only Slack inbound normalizer (#647). Maps to `slack.mention` / `slack.dm` /
  `slack.thread_reply` and filters our own bots' messages.
- **Slack Socket Mode** is an opt-in local-runtime transport into that same normalizer and delivery pipeline.
  The local server calls `apps.connections.open` with an app-level `xapp-` token, holds the returned outbound WebSocket, acknowledges each envelope, and passes its inner Events API payload to `handleSlackWebhook`.
  Python forwards the app token only through bubble-signed `POST /slack/workspaces` registration after `/health` reports `mode: local`; there is no event-server environment fallback.
  `/health.slack_socket` reports per-app connection state for doctor.
  The Worker remains webhook-only.
- **Linear** (`POST /webhooks/linear`): `type = linear.<type>.<action>`, key
  `linear:<TEAM_KEY>`. Signature (`Linear-Signature`, HMAC-SHA256 of the raw body)
  is verified when `LINEAR_WEBHOOK_SECRET` (local: `BOBI_ES_LINEAR_WEBHOOK_SECRET`)
  is set, with replay rejection via the signed `webhookTimestamp` (±300s;
  fail-closed - a signed payload without a numeric `webhookTimestamp` is
  rejected, since it would otherwise be replayable forever).
- **WhatsApp** (`POST /webhooks/whatsapp`, #656): `type = whatsapp.message`, key
  `whatsapp:<phone_number_id>`. Meta signs like GitHub (`X-Hub-Signature-256`),
  verified when `WHATSAPP_APP_SECRET` (local: `BOBI_ES_WHATSAPP_APP_SECRET`) is
  set. Meta additionally verifies the URL with a **GET subscribe handshake**
  (`hub.challenge` echoed as raw text when `hub.verify_token` matches
  `WHATSAPP_VERIFY_TOKEN` / `BOBI_ES_WHATSAPP_VERIFY_TOKEN`; rejected when the
  token is unset, so a third party can never bind the URL). Inbound messages
  also record the conversation's 24h customer-service window, which
  `/channels/send` enforces (`outside_message_window` typed error). Delivery
  receipts (`statuses`) are skipped.
- **Discord** (#2) is the exception to the webhook pattern: Discord delivers
  message events only over the **Gateway**, a persistent outbound WebSocket.
  The **local server** runs a Gateway connection manager
  (`src/discord-gateway-local.ts`, one connection per registered bot) around
  a runtime-agnostic protocol core (`core/src/gateway/discord.ts`:
  IDENTIFY/RESUME, heartbeats, close-code policy - resume-first reconnect
  protects the 1000/day IDENTIFY budget; bad-token/bad-intent closes park the
  connection as `fatal` in `/health`). The Worker has no Gateway driver yet.
  Types `discord.dm` / `discord.mention` / `discord.reply`, key
  `discord:<application_id>`; v1 receives DMs, bot @mentions, and replies to
  the bot only (no channel firehose), and drops all bot-authored messages
  (loop prevention). Connections start at boot from `BOBI_ES_DISCORD_BOT_TOKEN`
  + `BOBI_ES_DISCORD_APPLICATION_ID` and on `POST /discord/apps`
  registrations; `BOBI_ES_DISCORD_MESSAGE_CONTENT=1` opts into the privileged
  Message Content intent.
- **Generic ingest** (`POST /webhooks/ingest/<topic>`, #640): the escape hatch for
  external systems that cannot compute per-request signatures (alerting, CI, SaaS
  webhooks - most can only set static headers). The verify slot checks a **scoped
  ingest token** sent as `Authorization: Bearer <token>` (see Security); the
  default normalizer delivers the raw JSON body as the event's `payload` with its
  top-level primitives mirrored into `fields`, on exactly the token's bound topic,
  bubble-scoped to the minting bubble. Body fields never influence routing (unlike
  `createTopicEvent`, routing fields such as `repo` are inert here). Requests are
  capped at 256 KiB, rejected with 413 before the body is ever parsed, and
  rate-limited per token at 60/min (429; in-memory - authoritative locally,
  per-isolate on the Worker). This is the only webhook route whose path has a
  slash-bearing remainder; provider routes stay exact-match.

**Generic topic endpoint** (`POST /events/{topic}`). Monitors, lifecycle emits,
inter-agent inbox/reply, and any agent-emitted event publish here. It is
**bubble-signed and mandatory** (see Security) and rejects bodies carrying global
routing fields (`repo` / `team_key` / `workspace`) - those are webhook-only. `{topic}`
may contain slashes (e.g. `inbox/director`).

### Slack transport migration boundary

HTTP Events API stays the default for hosted or publicly reachable ingress.
Socket Mode removes the public Request URL requirement for a self-hosted local Node event server.
Both transports produce the same normalized Slack event shapes and topics.

Slack switches a single app exclusively between HTTP and WebSocket delivery, so there is no same-app overlap window.
Prepare the app token and bobi configuration while HTTP remains active, keep the existing Request URL and signing secret, and schedule a quiet cutover window.
Toggle Socket Mode on, immediately start or restart the agent, and wait for doctor to report `connected` before sending a test event.
Events that arrive after the toggle but before the socket connects can be lost.

Slack has no equivalent of Discord's paste-back login readiness gate.
A login DM emitted before the Socket Mode connection reaches `connected` can be lost; verify doctor before starting a Slack login flow.
For rollback, toggle Socket Mode off first so Slack resumes the saved HTTP Request URL, then verify webhook delivery.
Revoke the app-level token, remove `SLACK_APP_TOKEN`, restart the local event server, and immediately restart every agent pointed at it because the server restart clears registrations.
Events arriving between the server restart and agent re-registration are dropped.

## Topic subscription model

A **topic** is the routing key; a **subscription key** is the topic, optionally
prefixed by the subscriber's bubble id for tenant isolation (see Security).

### Topic catalog

| Shape | Origin | Example |
|---|---|---|
| `github:<owner>/<repo>` | GitHub webhook | `github:moda-labs/bobi` |
| `linear:<TEAM_KEY>` | Linear webhook | `linear:MOD` |
| `slack:<team>` | Slack webhook or Socket Mode (no app id) | `slack:T0ABC` |
| `slack:<team>:app:<app_id>[:<channel>]` | Slack webhook or Socket Mode (app-qualified) | `slack:T0ABC:app:A123:C0XYZ` |
| `whatsapp:<phone_number_id>` | WhatsApp webhook | `whatsapp:747556541` |
| `discord:<application_id>` | Discord Gateway (local server) | `discord:111222333444555666` |
| `inbox/<session>` | inter-agent fire-and-forget | `inbox/director` |
| `reply/<uuid>` | transient ask-reply channel | `reply/9f3a...` |
| `<type>` and `<source>/<type>` | monitors / agent publishes | `support.email`, `monitor/support.email` |
| `agent/session.completed` (+ bare `session.completed`) | sub-agent lifecycle | |

`github:`, `linear:`, `slack:`, `whatsapp:`, and `discord:` are **global** topics (cross-bubble, gated by
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
   - **whatsapp** from the configured phone-number id after Graph API validation.
   - **discord** from the configured application id after Discord API validation;
     Discord has one app-wide `discord:<application_id>` subscription in v1,
     while channel reachability is limited by Discord permissions and the
     Gateway normalizer's DM / @mention / reply filter.
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
- The cursor advances only after the session successfully completes each
  delivered inbox message. Queued messages, provider-error turns, and turns
  interrupted before their terminal result remain unacknowledged and replay
  after a process restart.
- The local server treats `last_seen = 0` as a real cursor and replays every
  buffered event with `seq > 0`. This preserves the first unacknowledged event
  (`seq = 1`) across a manager restart.
- The blocking-ask path still waits for the `connected` frame before publishing.
  That ordering keeps live request/reply delivery deterministic across backends.

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
| Ingest token mint / list / revoke | `POST` / `GET /ingest-tokens`, `DELETE /ingest-tokens/{id}` | bubble signature |
| Generic ingest | `POST /webhooks/ingest/{topic}` | bearer ingest token (topic-bound) |
| Channel send | `POST /channels/send` | bubble signature (bubble-scoped to its own workspace) |
| Channel typing | `POST /channels/typing` | bubble signature |
| Channel history | `GET /channels/history` | bubble signature (covers path + query, empty body) |
| WS subscribe / update subs / deregister | `/deployments/{id}/...` | bearer `api_key` (issued at register) |

The bubble key authenticates *membership*; the per-deployment `api_key`
authenticates a *specific deployment's transport*.

### Scoped ingest tokens

Generic publishes require a bubble signature, but external systems (alerting,
CI, SaaS webhooks) can rarely compute one - most can only set a static header.
A **scoped ingest token** (#640) is the credential for that case: the instance
mints it bound to one `(bubble, topic)` pair
(`bobi agent <name> events ingest-token create alert/firing`), and the external
system sends it as `Authorization: Bearer <token>` on
`POST /webhooks/ingest/<topic>`.

Properties:

- **Hash-only storage.** The server stores the SHA-256 of the token; the
  plaintext transits exactly once, in the mint response. `list` shows metadata
  only.
- **Scoped blast radius.** A leaked token allows publishing spurious events on
  its one bound topic in its one bubble - a 403 on every other topic - and
  never exposes bubble membership or the bubble key, which stays inside the
  instance.
- **Revocable.** `DELETE /ingest-tokens/<id>` (CLI: `ingest-token revoke`)
  takes effect immediately on the local server; on the Worker it is subject
  to KV propagation (typically seconds, up to ~60s across points of
  presence). Management routes are bubble-signed, and ids resolve only
  within the caller's own bubble.
- **Env-seeded locally.** The local server can seed tokens at boot from
  `BOBI_ES_INGEST_TOKENS`, a comma-separated list of `topic=token` bindings,
  for example `alert/firing=ingt_...`; commas are delimiters, so env-provided
  tokens must be comma-free. The server validates each topic, stores only the
  token hash, and lazily attaches the seeded records to the first local bubble
  after the first deployment registers. `list` flags these records as
  `env_managed`; `revoke` rejects them with guidance to rotate by changing
  `BOBI_ES_INGEST_TOKENS` in the deployment secret store and restarting. A
  changed secret takes effect after that restart, not as a hot reload. This is
  local-server only; Worker tokens are already durable in KV.
- **Bounded.** 256 KiB body cap enforced before the body is parsed, 60
  requests/min per token. Auth rejections are opaque 403s (missing, unknown,
  revoked, and wrong-topic are indistinguishable) and count into
  `webhook_bad_signature` on `/health`; 413/429 policy rejections do not
  pollute that counter.
- **Topic shape.** `source/type` form from `[A-Za-z0-9_.-]` segments; the
  `github`/`linear`/`slack` sources and `:`-style global keys are rejected at
  mint, so an ingest token can never reach a provider or global topic.

### Proof-of-access: resource grants

Before a bubble may subscribe to or receive a **global webhook topic**, the server
verifies an upstream credential **once** and stores a `ResourceGrant` - storing only
the grant, never the credential:

- **GitHub**: `GET /repos/{owner}/{repo}` with the token must return 2xx (read
  access).
- **Linear**: a teams query must return the specific team key (access to that team,
  not just the org).
- **Slack**: the bubble-signed `/slack/workspaces` registration (proving the bot token via `auth.test`) doubles as the grant and stores per-app send and verification credentials.
  On a local runtime, the same signed record may carry the Socket Mode app token and start the outbound connection; unsigned registrations never do.
- **WhatsApp**: the bubble-signed `/whatsapp/numbers` registration (proving the
  Cloud API token can read the phone-number node on the Graph API) doubles as
  the grant, and stores the bubble-scoped send credential.
- **Discord**: the bubble-signed `/discord/apps` registration (proving the bot
  token via `GET /applications/@me`, whose id must match) doubles as the
  grant, and stores the bubble-scoped send credential. On the local server it
  also starts the app's Gateway connection.

Unknown services are default-deny (only `github:` / `linear:` / `slack:` / `whatsapp:` / `discord:` are global).
The grant is enforced at **three points**, all sharing one parser so they cannot
diverge:

1. **Registration** - any ungranted global topic rejects the whole request.
2. **Subscription update** - the same gate on newly added subscriptions.
3. **Delivery** - the authoritative, fail-closed boundary: a global event is
   admitted to a subscriber only if its bubble currently holds the grant, so a stale
   index entry for a revoked bubble is dropped.

The client authorizes its github/linear topics (and registers Slack workspaces /
WhatsApp numbers / Discord apps) before it registers subscriptions, dropping any
topic whose credential is missing or rejected so it never trips the server's
hard reject.

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

`event-server/` is an npm workspace. The runtime-agnostic protocol lives in the
`event-server/core/` package (`@moda-labs/bobi-events-core`); both runtimes under
`event-server/src/` consume it by package name, never by relative path (enforced
by `tests/test_import_boundaries.py`).

- `event-server/core/src/core.ts` - shared handlers, the unified webhook pipeline,
  routing, HMAC auth, grant filter.
- `event-server/core/src/adapters/{github,slack,linear}.ts` - webhook normalizers.
- `event-server/src/index.ts` - Cloudflare Worker entry (KV storage, DO fan-out).
- `event-server/src/local.ts` - local Node entry (in-memory store, direct sockets).
- `event-server/src/deployment-session.ts` - the per-deployment Durable Object.
- `bobi/events/server.py` - local-server launcher + bubble mint / grant setup.
- `bobi/events/client.py` - the WebSocket client (connect, replay, heartbeat).
- `bobi/events/{subscriptions,adapters,drain,publish,signing}.py` - subscription
  resolution, delivery to the inbox, publishing, and request signing.
- `bobi/inbox.py` - inter-agent inbox / reply channels over the bus.
