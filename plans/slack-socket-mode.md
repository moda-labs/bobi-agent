# Slack Socket Mode transport

> **Status:** Approved
> **Tracking issue:** moda-labs/bobi-agent#749 · **Created:** 2026-07-21 · **Last amended:** - (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Self-hosted bobi deployments can use Slack without exposing any inbound network surface.
Slack Socket Mode becomes a supported transport that feeds the existing normalize -> store -> fan out -> dispatch pipeline, exactly as Discord Gateway inbound already does.
HTTP webhooks remain the default and the only option for hosted deployments; Socket Mode is an explicit opt-in for operators who self-host precisely because they want no public ingress (issue #749).
As a side effect, the ingress reachability check becomes transport-aware, which also cures an existing false positive for Discord-only agents.

## Problem

All claims verified against the working tree at 1baf7fe on 2026-07-21.

- Slack inbound is webhook-only.
  The only Slack entry point is `POST /webhooks/slack`, registered as the `slack` source with `preVerify`/`verify`/`handle` slots at `event-server/core/src/core.ts:1050-1083`; `verify` enforces the signing-secret HMAC (`verifySlackSignature`, `core.ts:522-535`).
  A workspace whose event server URL is not public HTTPS cannot receive Slack events at all.
- `bobi doctor` and agent start warn, with no remedy that avoids public ingress.
  `check_ingress_reachability` (`bobi/ingress.py:82-115`) warns whenever any `events: true` service is configured while `event_server_url` is loopback/private (`is_unusable_ingress_url`, `ingress.py:30-44`); it is surfaced by `bobi/doctor.py:394-415` and at spawn by `bobi/service.py:182-184`.
  The hint offers only the cloud event server, a deployed Worker, or a public tunnel - the three remedies issue #749's reporter (an air-gapped Tailscale-only host) rejects by design.
- The same check has an existing false positive: a Discord-only agent declares `services: discord` with `events: true` (`skills/discord-setup.md`), yet Discord inbound rides a persistent OUTBOUND WebSocket and needs no ingress - the warning fires anyway, because the check has no notion of transport.
  The check also folds in raw subscription topic strings (`explicit_subscriptions`, `ingress.py:92-102`), so any transport rule must classify prefixed topics (`slack:...`, `discord:...`) as well as service names.
- The generated Slack app manifest hardcodes the webhook topology: `request_url: ${EVENT_SERVER}/webhooks/slack` (`bobi/templates/slack-app.manifest.yaml:41`) and `socket_mode_enabled: false` (`:49`); `bobi create-slack-bot` (`bobi/cli.py:1328-1426`, rendering via `bobi/slack_manifest.py:32-42`) offers no alternative.
- `POST /slack/workspaces` deliberately accepts UNSIGNED registrations (the global record serves legacy self-reply loop prevention, `event-server/src/local.ts:583-595`), while Discord's socket-starting registration route `POST /discord/apps` is signed-only (`local.ts:606-618`).
  Any credential that drives an outbound authenticated connection must therefore not be writable through the unsigned path.
- History: a Socket Mode client existed (`manager/events/slack_socket.py`, added in `f46d2dd`) and was deleted by #86 (`aded618`) when Slack moved onto the event-bus pipeline.
  Three of #86's four objections (blocking `inject()` on the WS thread, no persistence, no workflow dispatch) were properties of that handler's pipeline bypass, not of the transport; the fourth ("we already have a public Cloudflare worker") is false by design for self-hosted air-gapped deployments.
  Issue #749's analysis of this is correct against the actual commits.
- What #749 asked for as step 1 is already done: `bridgeSlackWebhook` takes a raw JSON string body with zero HTTP coupling (`event-server/core/src/adapters/chat-sdk-slack.ts:44-51`; signature verification lives entirely upstream in the route's `verify` slot), and `handleSlackWebhook(storage, rawBody, payload)` (`core.ts:774-805`) is likewise HTTP-free: it loads the workspace record, computes the self-bot filters, and calls `storage.deliver`.
  A Socket Mode envelope wraps the very same `event_callback` body a webhook delivers (`team_id` and `api_app_id` included), so the socket path can feed `handleSlackWebhook` directly - no normalizer refactor is needed.
- The house pattern for exactly this shape already exists: Discord Gateway inbound is a sans-IO protocol state machine (`event-server/core/src/gateway/discord.ts`) driven by a local-runtime driver (`event-server/src/discord-gateway-local.ts`) that owns sockets, timers, backoff, a handshake watchdog, and a `/health` surface, delivering into the same `StorageAdapter` (`event-server/src/local.ts:264-343`).
  The hosted Worker has no Gateway driver (`skills/discord-setup.md`), establishing the precedent that dial-out transports are a local-event-server feature.

## Solution

Mirror the Discord Gateway architecture for Slack Socket Mode, reusing the existing Slack pipeline entry, with three Slack-specific deviations the protocol forces: connection URLs are single-use and rate-limited (`apps.connections.open` is Tier 1), there is no resume/replay (envelope redelivery IS the gap-recovery mechanism), and liveness is server-ping-based rather than client-heartbeat-based.

1. A sans-IO Socket Mode protocol session in events-core (`event-server/core/src/gateway/slack-socket.ts`): consumes decoded frames (`hello`, `events_api` envelopes, `disconnect` with per-reason policy, `slash_commands`/`interactive` acked and dropped) and staleness-timer input, emits driver actions (ack, deliver, reconnect, connected, fatal).
   Envelopes are acked by `envelope_id` immediately on receipt, before any delivery work - the discipline #86 wanted and the old handler violated.
   Unlike Discord's session (which normalizes internally), the deliver action carries the envelope's inner payload; self-bot filtering needs an async workspace lookup, so normalization stays in the driver via `handleSlackWebhook`.
2. A local driver (`event-server/src/slack-socket-local.ts`) mirroring `DiscordGatewayManager`: per-app connections, `apps.connections.open` via the existing `slackApiUrl()` seam (`event-server/core/src/channels.ts:39-46`) with the app-level token, rate-limit-aware backoff, handshake watchdog plus a frame-staleness watchdog, generation guard, `health()` surfaced in `/health` as `slack_socket`.
   Each `events_api` envelope becomes `handleSlackWebhook(storage, body, payload)` - workspace lookup, self-loop filtering, topics, and fan-out all come from the one existing code path.
3. Token plumbing through SIGNED registration only: the app-level token (`xapp-...`, scope `connections:write`) joins the `/slack/workspaces` record as a per-bot `SlackBotRecord` field with the same preserve-on-merge semantics as `signing_secret` (`core.ts:2407-2433`), and socket connections start/stop/repoint ONLY from signed registrations - the unsigned path stays inert for socket purposes, matching Discord's signed-only `/discord/apps` precedent.
   No env-token fallback (per Q2): registration runs at every session start (`bobi/subagent.py` -> `register_slack_workspaces`), which is also what cures the already-running-server case, and an env-only socket with no workspace record would silently disarm self-bot loop filtering (`workspaceBotIds(null)` returns an empty set, `core.ts:159-189`).
4. Opt-in UX: `SLACK_APP_TOKEN` as an optional Slack secret, `bobi create-slack-bot --socket-mode`, a transport-aware ingress check (also fixing the existing false positive for Discord-only agents), and doctor surfacing of the socket connection state so a mis-toggled app is observable rather than silently green.
5. Docs: Socket Mode section in `skills/slack-setup.md`, self-hosted topology note in `docs/SELF_HOSTED_EVENT_SERVER.md` and `docs/EVENT_SERVER.md`, including the webhook-to-socket migration note (a workspace briefly live on both transports can double-deliver the same `event_id`; `storage.deliver` has no id-level dedup).

Alternatives considered:

- Slack's `@slack/socket-mode` SDK instead of a sans-IO session: rejected pending Q1 - the in-house sans-IO pattern matches Discord, tests with recorded frames and no network, and avoids a new dependency tree in events-core; the protocol surface (hello, envelope+ack, disconnect taxonomy, ping-staleness) is still small.
- Matrix adapter (#744) as the self-hosted answer: complementary, not a substitute - it fixes ingress only for teams that leave Slack.
- Tunnel/hosted ingress: exists today and is exactly what #749's deployment model rejects.
- Socket Mode in the hosted Worker: rejected - a persistent outbound WebSocket is unnatural in a Worker and unnecessary there (hosted has public ingress); matches the Discord precedent.
- Env-configured socket start (Discord parity): rejected per Q2 recommendation - Slack's variant is broken two independent ways (no app identity in the env shape; no workspace record means no self-loop filtering) and adds nothing over registration, which every session start already performs.

## Relevant files

### Existing (verified 2026-07-21)

- `event-server/core/src/core.ts` - Slack source registration (`:1050-1083`), `handleSlackWebhook` (`:774-805`, the transport-free pipeline entry the driver reuses), `handleSlackWorkspaceRegister` + `mergeBot` per-bot merge semantics (`:2349-2477`), `verifySlackSignature` (`:522-535`, webhook-only, untouched), self-bot set derivation (`:159-189`, `:783-791`).
- `event-server/core/src/adapters/chat-sdk-slack.ts` - `bridgeSlackWebhook` (`:44-51`), transport-independent; untouched.
- `event-server/core/src/gateway/discord.ts` + `event-server/src/discord-gateway-local.ts` - the architecture template (sans-IO session + driver); its zombie-detection (`onTimer`, `discord.ts:203-212`) is the shape the Slack staleness watchdog mirrors.
- `event-server/core/src/channels.ts` - `slackApiUrl()`/`setSlackApiUrl` (`:39-46`), the REST seam for `apps.connections.open` and its test stub.
- `event-server/src/local.ts` - driver wiring points: manager construction (`:372` pattern), `/health` (`:471-483`), Slack registration route (`:573-595`, optional-auth), Discord's signed-only start precedent (`:606-618`).
- `bobi/events/server.py` - `register_slack_workspaces` (`:653+`, gains `app_token`; note `_slack_auth_info`/`_slack_app_id` hardcode `https://slack.com` at `:610`/`:641`, so integration tests must register via `signed_request` directly), `ensure_running` (`:194-199` returns `connected` on a bare health probe - env changes never reach an already-running server, which is why registration is the load-bearing config path).
- `bobi/ingress.py` - `check_ingress_reachability` (`:82-115`) becomes transport-aware; classifies both service names and prefixed topic strings.
- `bobi/doctor.py` (`:394-415`) and `bobi/service.py` (`:182-184`) - both surfaces of the ingress check; doctor additionally gains socket-state surfacing (Phase 3).
- `bobi/setup/services.py` - Slack `Secret` declarations (`:173-174`) gain optional `SLACK_APP_TOKEN`.
- `bobi/cli.py` (`:1328-1426`) + `bobi/slack_manifest.py` + `bobi/templates/slack-app.manifest.yaml` - `--socket-mode` rendering (post-processing mechanism, see Phase 3).
- `tests/test_slack_manifest.py` (`test_manifest_uses_http_events_not_socket_mode`, `:82-84`), `tests/test_ingress.py` (`:25` pins `"public tunnel" in warning.hint`), `tests/test_doctor.py` (`:126` pins `"event_server_url" in r.hint`) - existing suites the change extends; the two hint assertions are the known-touched ones.
- `tests/integration/test_discord_gateway.py` - the integration-test template (stubbed REST + stubbed WS server via `NODE_PATH`, real local event server, no live credentials).
- `tests/integration/test_slack_live.py` - the env-gated live-test pattern for the Phase 3 smoke leg.
- `skills/slack-setup.md`, `docs/SELF_HOSTED_EVENT_SERVER.md`, `docs/EVENT_SERVER.md` - docs surfaces.

### New

- `event-server/core/src/gateway/slack-socket.ts` - sans-IO Socket Mode session; no existing module owns WS protocol logic for Slack, and the Discord session is protocol-specific by design.
- `event-server/src/slack-socket-local.ts` - the local driver; parallel to `discord-gateway-local.ts` rather than merged with it because the protocols share no frames.
- `event-server/src/socket-driver-common.ts` (name at builder's discretion) - the genuinely shared driver scaffolding (backoff+jitter, watchdog timers, generation guard) hoisted out per the Q1 decision and consumed by BOTH drivers; protocol logic stays out of it.
- `event-server/test/slack-socket.spec.ts` - recorded-frame session tests, mirroring `discord.spec.ts`.
- `tests/integration/test_slack_socket_mode.py` - end-to-end integration mirroring `test_discord_gateway.py`.

## Questionables

- **Q1:** Protocol implementation - (a) in-house sans-IO session mirroring the Discord pattern / (b) depend on `@slack/socket-mode` (official SDK).
  Recommendation: (a) - consistency with the Discord seam, deterministic recorded-frame tests, no new dependency tree in events-core; the protocol surface (hello, envelope+ack, disconnect taxonomy, ping-staleness) is small, and the review pass enumerated it fully.
  **Decision (2026-07-21, Zach):** chose (a), with one addition: genuinely common socket-driver scaffolding (backoff+jitter, watchdog timers, generation guard) is hoisted into a shared helper both drivers consume during Phase 2, rather than copy-pasted a second time; protocol-specific logic stays per-transport.
- **Q2:** App-token plumbing - (a) signed `/slack/workspaces` registration ONLY (no env fallback) / (b) signed registration plus a `BOBI_ES_SLACK_APP_TOKEN` + companion-identity env fallback with documented caveats.
  Recommendation: (a).
  Review evidence overturned the original both-paths recommendation: an env-only socket has no app identity to key the connection (`apps.connections.open` returns only `{ok, url}`; Discord's env shape needs a second variable), and with no workspace record the self-bot filter set is empty (`core.ts:159-189`), recreating the #86 self-loop shape.
  Registration runs at every session start, so (a) also cures the already-running-server case that env pass-through cannot reach (`ensure_running` health-probes and returns, `bobi/events/server.py:194-199`).
  **Decision (2026-07-21, Zach):** chose (a).
- **Q3:** Ingress-check scope - (a) make the check transport-aware in this initiative, fixing the Discord false positive too / (b) special-case Slack-with-app-token only, leave Discord as-is.
  Recommendation: (a) - one mechanism, and the Discord warning is a real current defect this plan already has to touch the check for.
  Either way the rule must cover prefixed topic strings (`slack:...`, `discord:...`), not just service names (`ingress.py:92-102`).
  **Decision (2026-07-21, Zach):** chose (a).
- **Q4:** Envelope retry handling - (a) deliver retransmissions, deduping only envelopes THIS process already acked (bounded acked-`envelope_id` LRU in the session) / (b) ack retransmissions (`retry_attempt > 0`) without delivering, mirroring the webhook `preVerify` drop (`core.ts:1051-1064`).
  Recommendation: (a).
  Review evidence overturned the original (b): the webhook parity argument is unsound for this transport - an HTTP retry follows a request the server already received, while a Socket Mode retransmission is Slack redelivering an envelope that was never acked (connection died pre-ack, or the event fired in a reconnect gap).
  Slack has no resume/replay; redelivery IS the gap-recovery mechanism, and with a single connection recycling roughly hourly, drop-on-retry guarantees permanent loss of every event landing in a gap.
  The dedup tests on both transports must cross-reference each other so the two policies cannot drift unnoticed.
  **Decision (2026-07-21, Zach):** chose (a).

## Phases

### Phase 1 - Socket Mode protocol core (sans-IO, events-core)

- [ ] `event-server/core/src/gateway/slack-socket.ts`: session state machine consuming decoded frames - `hello` (connection established; carries `connection_info.app_id`, which the driver uses to key the connection), `events_api` envelope (emit ack action + deliver action carrying the inner payload), `disconnect` with per-reason policy (`warning` = advisory, no action - Slack sends it seconds before the real refresh; `refresh_requested` and close = reconnect action; `link_disabled` = fatal, the feature was turned off), `slash_commands`/`interactive`/unknown types (ack where an `envelope_id` is present, deliver nothing), malformed frames ignored without dying
- [ ] Staleness input: an `onTimer("staleness")` session input mapping to a reconnect action - WS-level auto-pong (the `ws` library answers server pings automatically) keeps the connection alive but detects nothing; a half-open socket must not park as connected forever (mirror of Discord's missed-ACK zombie detection, `gateway/discord.ts:203-212`)
- [ ] Retransmission dedup per Q4 decision: deliver by default; a bounded LRU of acked `envelope_id`s suppresses only true re-sends of envelopes this session already acked
- [ ] Action vocabulary mirroring `GatewayAction` (`send`, `deliver`, `reconnect`, `connected`, `fatal`) so the driver loop stays structurally identical to Discord's
- [ ] Export from the events-core package (`event-server/core/package.json` `exports` map, alongside `./gateway/discord`; the publish smoke auto-derives subpaths, no other packaging work)
- [ ] `event-server/test/slack-socket.spec.ts`: recorded-frame tests mirroring `discord.spec.ts` - hello/ack ordering, envelope -> deliver payload fidelity, per-reason disconnect policy, staleness -> reconnect, acked-envelope dedup (with a cross-reference comment to the webhook `preVerify` dedup test so the two policies are compared deliberately), malformed-frame survival, ack-before-deliver ordering

**Validation gate** - do not exit this phase until every line passes; if a command fails, fix the cause and re-run.

- [ ] `cd event-server && npm test` green, including the new spec
- [ ] The new module is inert: no driver references it yet, `npm run build:local` output unchanged in behavior

### Phase 2 - Local driver and wiring

- [ ] `event-server/src/slack-socket-local.ts`: `SlackSocketManager` mirroring `DiscordGatewayManager` - connections keyed provisionally per registration and re-keyed by `hello`'s `connection_info.app_id`, `apps.connections.open` via `slackApiUrl()` with the app token, handshake watchdog plus staleness watchdog (reset on every frame AND on the ws `'ping'` event), generation guard for stale-socket callbacks, `health()` reporting state/last-event-at/delivered-event count/connect counts
- [ ] Shared scaffolding hoist (Q1 decision): extract the genuinely common driver machinery (backoff+jitter schedule, watchdog timer management, generation-guard pattern, socket teardown incl. the sink-error-listener close dance) into a shared module consumed by both `discord-gateway-local.ts` and the new driver; behavior-preserving for Discord - the existing `tests/integration/test_discord_gateway.py` and `event-server/test/discord.spec.ts` stay green unmodified, which is the gate for the refactor being genuinely common rather than forced
- [ ] Rate-limit-aware reconnect: `apps.connections.open` is Tier 1 (roughly 1/min) and every returned `wss` URL is single-use - re-call it on every reconnect, never reuse a URL, backoff floor of several seconds (NOT Discord's 1s base), honor `Retry-After` on 429/`rate_limited`
- [ ] Fatal policy: `apps.connections.open` 4xx (bad/revoked app token, missing `connections:write`) and `link_disabled` park the connection fatal with the reason, no retry loop; transient failures back off
- [ ] Trust hardening: the driver rejects a returned connection URL whose scheme is not `wss://`; the `BOBI_ES_SLACK_API_URL` override is a test-only seam and the driver refuses to start with it set when `BOBI_ES_BIND` is non-loopback (the socket path has no HMAC second line of defense, so a poisoned API base would otherwise be a single point of total compromise)
- [ ] Secret hygiene: the app token never appears in `/health` output or any log line (fatal reasons included), with a test asserting both
- [ ] Envelope delivery: each `events_api` payload -> `handleSlackWebhook(storage, JSON.stringify(payload), payload)`; ack is sent before delivery is awaited; delivery failures are logged, never block the socket - this is at-most-once with documented loss on delivery failure (unlike the webhook path, where a non-200 makes Slack retry); bound concurrent in-flight deliveries and set an explicit `ws` `maxPayload`
- [ ] `handleSlackWorkspaceRegister` (`core.ts:2349`) accepts an optional `app_token`, stored per-bot on `SlackBotRecord` with the same preserve-on-merge rule as `signing_secret` (`core.ts:2407-2433` - an older client re-registering without the field must not wipe it)
- [ ] Socket lifecycle is signed-only: connections start/restart/stop from SIGNED `/slack/workspaces` registrations (and teardown); an `app_token` in an unsigned registration is not persisted to the global record and never drives a connection (Discord signed-only precedent, `local.ts:606-618`); test asserts an unsigned POST cannot start, stop, or repoint a socket
- [ ] `local.ts` wiring: manager construction, `/health` gains `slack_socket` (shape mirroring `discord_gateway`; verified additive - the only bobi-side field consumer is Discord-specific, `bobi/auth_bootstrap.py:312-343`)
- [ ] `tests/integration/test_slack_socket_mode.py` mirroring `test_discord_gateway.py`, with the three Slack-specific deltas spelled out: (1) the stub Slack REST API must implement `auth.test` (returning `ok` + matching `team_id` + `bot_id`) in addition to `apps.connections.open`, because signed registration verifies the bot token server-side (`core.ts:2367-2379`); (2) register via `signed_request` POST `/slack/workspaces` with explicit `bot_id`/`bot_user_id`/`app_id`/`app_token` - the production client cannot be pointed at stubs (`_slack_auth_info` hardcodes `slack.com`, `bobi/events/server.py:610`); (3) the signed registration is also what writes the `slack:{team}` resource grant that gates the test deployment's subscription (`core.ts:2458-2461`).
  Prove: envelope -> normalized `slack.mention`/`slack.dm` delivery to a subscribed deployment WebSocket, ack observed at the stub before delivery completes, reconnect after a `refresh_requested` disconnect with delivery still working, retransmitted-unacked envelope delivered exactly once, self-authored bot messages filtered, unsigned registration starts nothing

**Validation gate**

- [ ] `cd event-server && npm test` green
- [ ] `pytest tests/integration/test_slack_socket_mode.py -q` green
- [ ] `/health` on a socket-configured local server shows `slack_socket` with `state: connected` against the stub, and the app token appears nowhere in the `/health` body

### Phase 3 - Opt-in UX, doctor, docs

- [ ] `SLACK_APP_TOKEN` declared as an optional Slack secret (`bobi/setup/services.py`); `cfg.credential("slack", "app_token")` resolves it
- [ ] `register_slack_workspaces` includes `app_token` in the signed registration record when configured AND the event server is the local one; omit it for remote/hosted servers (the hosted store has no driver - a token registered there is a silent no-op credential)
- [ ] Doctor socket-state surfacing: when Slack is configured with an app token, doctor reads `/health`'s `slack_socket` block and reports connection state (registration is best-effort and swallows failures, `bobi/events/server.py:715-717`, so without this a mis-toggled app or dead socket is silently green); doctor also flags the `SLACK_APP_TOKEN` + remote `event_server_url` combination as ineffective
- [ ] `bobi create-slack-bot --socket-mode`: implemented as post-processing of the rendered default (flip the `socket_mode_enabled` line, remove the `request_url` line, adjust the header comment that says "HTTP Events API, no Socket Mode") so the no-flag output stays byte-identical by construction, pinned by a test asserting the no-flag render equals the raw template substitution; socket output has `socket_mode_enabled: true` and NO `request_url`; `bot_events` unchanged; the printed next-steps text explains the app-token step (`xapp-...`, scope `connections:write`) instead of the request URL
- [ ] Transport-aware ingress check per Q3 decision: `check_ingress_reachability` stops counting a source as webhook-inbound when its transport dials out (Slack with `app_token` configured, Discord always), covering both service names and prefixed topic strings; the warning still FIRES for webhook-only Slack config, with hint text updated to name Socket Mode as a remedy - the pinned assertions at `tests/test_ingress.py:25` and `tests/test_doctor.py:126` are updated alongside
- [ ] Docs: `skills/slack-setup.md` gains the Socket Mode path (app-level token creation, `connections:write` scope, no request URL); `docs/SELF_HOSTED_EVENT_SERVER.md` and `docs/EVENT_SERVER.md` state the transport topology (webhooks = hosted/public, Socket Mode = self-hosted opt-in, local event server only), the migration double-delivery note (same `event_id` on both transports during a webhook-to-socket switch; no id-level dedup in `storage.deliver`), and the login-flow limitation (no Slack analog of the Discord paste-back readiness gate, `bobi/auth_bootstrap.py:312-343` - a login DM sent before the socket connects is lost)
- [ ] Live smoke, env-gated: an integration leg following the `tests/integration/test_slack_live.py` pattern that runs only when `SLACK_APP_TOKEN`+`SLACK_BOT_TOKEN` are present in the environment - real `apps.connections.open`, one real mention delivered end-to-end; skipped in CI

**Validation gate**

- [ ] `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` green (covers `test_slack_manifest.py`, `test_ingress.py`, `test_doctor.py` extensions)
- [ ] On an isolated `BOBI_HOME` with Slack configured for Socket Mode and `event_server_url` unset: `bobi agent <name> doctor` shows NO ingress warning and reports the socket connection state; with webhook-only Slack config the ingress warning still fires
- [ ] Same isolated check for a Discord-only agent: no ingress warning (the false positive is gone)
- [ ] `bobi create-slack-bot --socket-mode --format yaml` output inspected: `socket_mode_enabled: true`, no `request_url`; no-flag output diffed byte-identical against main

## Proof of work

- This is a feature, not a bug fix: no failing-test-first requirement; each phase names its suites above.
- Real-brain e2e judgement call (per `CLAUDE.md`): NOT required.
  The change is a transport feeding the existing event pipeline upstream of any session; the brain path (delivery from event server to a live session) is unchanged and already covered.
  The risk lives in the WS protocol handling and the driver wiring, which the recorded-frame spec and the stubbed-server integration test exercise on the real local event server - the same bar the Discord channel shipped against (`tests/integration/test_discord_gateway.py`, no live credentials).
- The env-gated live smoke in Phase 3 is the real-dependency leg for operators who hold Slack credentials; it must skip cleanly without them.
- CI needs no workflow changes (verified): the event-server job runs `tsc --noEmit` + `vitest` over the workspace, and `integration-fast` runs unmarked `tests/integration/` tests with event-server deps installed (`.github/workflows/ci.yml`).
- Nothing in any phase touches `VERSION`, `pyproject.toml` version, or `CHANGELOG.md` (release rules).

## Ticket map

Filled by the split workflow after approval.

| Phase | Ticket | One-line scope | Status |
|---|---|---|---|

**Lanes:** cut tickets LARGE, along parallel-lane seams only (sizing directive, 2026-07-21, Zach: plans are optimized for agent implementation, and parallelism - not piece-level revertability - is the split criterion; rollback happens at feature level in practice).
Expected cut: **Lane A = Phases 1+2 as ONE ticket** (the transport, TS-side, proven end-to-end by the integration test - Phase 1 alone would land inert dead code and buys no parallelism), **Lane B = Phase 3 as one ticket** (Python-side opt-in surface; can build in parallel with A once the `app_token` registration-record field shape is fixed, lands after A; its integration-gate lines need A landed).
Phases remain checkpoints INSIDE a lane - a builder works through successive phase gates in one session/PR, never stopping at a phase boundary because it is merely landable.
A single builder executing both lanes in one session is acceptable; parallel dispatch is the optimization when available.

## Amendments

- **2026-07-21** (Zach, via plan session): Lanes note rewritten with the ticket-sizing directive - large agent-sized tickets cut along parallel-lane seams (expected: two tickets, Phases 1+2 and Phase 3), phases as in-lane checkpoints rather than ticket boundaries, piece-level revertability dropped as a split criterion.

## Notes

- Tracking-issue designation is deferred: #749 will be retitled/labeled as this plan's tracking issue in a follow-up step; until then discussion stays on #749 as filed, and this file is the source of truth for the design.
- #86's pipeline decision stands: Socket Mode here is a transport INTO the `normalize -> store -> fan out -> dispatch` pipeline, never a bypass; nothing touches sessions directly.
- Single connection per app is the deliberate v1 choice.
  Slack permits up to 10 simultaneous Socket Mode connections and recommends two for gapless hourly refresh; the acked-envelope dedup (Q4) plus redelivery covers the refresh gap at v1 scale, and a second connection is a small follow-up if gap loss is observed in practice.
- Socket Mode apps cannot be listed in the public Slack Marketplace (Slack platform constraint) - irrelevant for manifest-created single-workspace apps, but it is the standing reason HTTP webhooks stay the default rather than being replaced.
- Shared driver scaffolding: the original draft deferred any common-code extraction until a third transport existed; the Q1 decision (2026-07-21, Zach) overrides that - genuinely common scaffolding is hoisted in Phase 2, with "Discord tests stay green unmodified" as the line between common machinery and forced abstraction.
  A full shared connection-MANAGER (one class driving both protocols) remains out of scope; the protocols share no frames.
- Deferred follow-up: a Slack analog of the Discord login paste-back readiness gate (`_ensure_discord_paste_back_ready`); Phase 3 documents the limitation instead.
- If the setup web UI grows an app-token field beyond the generic secret declaration, `DESIGN.md` and `docs/FRONTEND_QA.md` govern that work; Phase 3 as scoped expects no bespoke UI.
- Plan review (2026-07-21, pre-approval): three adversarial lens passes (red-team, staff-engineer, implementer) produced 12 confirmed findings folded into this revision - signed-only socket lifecycle, staleness watchdog, rate-limit-aware reconnect, Q2/Q4 recommendations overturned with evidence, integration-test registration mechanics, manifest post-processing mechanism, doctor socket-state surfacing pulled into scope.
  Full findings are in the plan PR discussion.
- Prior art: #749 (this initiative's origin), #86 (webhook migration), #744 (Matrix, complementary), PR #741 (Discord Gateway, the architecture template).
