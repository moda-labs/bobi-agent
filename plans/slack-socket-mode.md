# Slack Socket Mode transport

> **Status:** Draft
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
- The generated Slack app manifest hardcodes the webhook topology: `request_url: ${EVENT_SERVER}/webhooks/slack` (`bobi/templates/slack-app.manifest.yaml:41`) and `socket_mode_enabled: false` (`:49`); `bobi create-slack-bot` (`bobi/cli.py:1328-1426`, rendering via `bobi/slack_manifest.py:32-42`) offers no alternative.
- History: a Socket Mode client existed (`manager/events/slack_socket.py`, added in `f46d2dd`) and was deleted by #86 (`aded618`) when Slack moved onto the event-bus pipeline.
  Three of #86's four objections (blocking `inject()` on the WS thread, no persistence, no workflow dispatch) were properties of that handler's pipeline bypass, not of the transport; the fourth ("we already have a public Cloudflare worker") is false by design for self-hosted air-gapped deployments.
  Issue #749's analysis of this is correct against the actual commits.
- What #749 asked for as step 1 is already done: `bridgeSlackWebhook` takes a raw JSON string body with zero HTTP coupling (`event-server/core/src/adapters/chat-sdk-slack.ts:44-51`; signature verification lives entirely upstream in the route's `verify` slot), and `handleSlackWebhook(storage, rawBody, payload)` (`core.ts:774-805`) is likewise HTTP-free: it loads the workspace record, computes the self-bot filters, and calls `storage.deliver`.
  A Socket Mode envelope wraps the very same `event_callback` body a webhook delivers, so the socket path can feed `handleSlackWebhook` directly - no normalizer refactor is needed.
- The house pattern for exactly this shape already exists: Discord Gateway inbound is a sans-IO protocol state machine (`event-server/core/src/gateway/discord.ts`) driven by a local-runtime driver (`event-server/src/discord-gateway-local.ts`) that owns sockets, timers, backoff, a handshake watchdog, and a `/health` surface, delivering into the same `StorageAdapter` (`event-server/src/local.ts:264-343`).
  Registration flows through both an env fallback (`local.ts:847-850`) and a signed HTTP registration (`POST /discord/apps`, `local.ts:606-614`).
  The hosted Worker has no Gateway driver (`skills/discord-setup.md`), establishing the precedent that dial-out transports are a local-event-server feature.

## Solution

Mirror the Discord Gateway architecture for Slack Socket Mode, reusing the existing Slack pipeline entry.

1. A sans-IO Socket Mode protocol session in events-core (`event-server/core/src/gateway/slack-socket.ts`): consumes decoded frames (`hello`, `events_api` envelopes, `disconnect`, `slash_commands`/`interactive` ignored), emits driver actions (ack, deliver, reconnect, connected, fatal).
   Envelopes are acked by `envelope_id` immediately on receipt, before any delivery work - the discipline #86 wanted and the old handler violated.
   Unlike Discord's session (which normalizes internally), the deliver action carries the envelope's inner payload; self-bot filtering needs an async workspace lookup, so normalization stays in the driver via `handleSlackWebhook`.
2. A local driver (`event-server/src/slack-socket-local.ts`) mirroring `DiscordGatewayManager`: per-app connections, `apps.connections.open` via the existing `slackApiUrl()` seam (`event-server/core/src/channels.ts:39-46`) with the app-level token, exponential backoff with jitter, handshake watchdog, generation guard, `health()` surfaced in `/health` as `slack_socket`.
   Each `events_api` envelope becomes `handleSlackWebhook(storage, body, payload)` - workspace lookup, self-loop filtering, topics, and fan-out all come from the one existing code path.
3. Token plumbing through the existing registration: the app-level token (`xapp-...`, scope `connections:write`) joins the `/slack/workspaces` record alongside `bot_token`/`signing_secret` (`bobi/events/server.py:register_slack_workspaces`, `core.ts:2349 handleSlackWorkspaceRegister`), plus a `BOBI_ES_SLACK_APP_TOKEN` env fallback mirroring Discord's.
4. Opt-in UX: `SLACK_APP_TOKEN` as an optional Slack secret, `bobi create-slack-bot --socket-mode` generating a manifest with `socket_mode_enabled: true` and no `request_url`, and a transport-aware ingress check that stops warning when every configured inbound source dials out (Slack with an app token, Discord always).
5. Docs: Socket Mode section in `skills/slack-setup.md`, self-hosted topology note in `docs/SELF_HOSTED_EVENT_SERVER.md` and `docs/EVENT_SERVER.md`.

Alternatives considered:

- Slack's `@slack/socket-mode` SDK instead of a sans-IO session: rejected pending Q1 - the in-house sans-IO pattern matches Discord, tests with recorded frames and no network, and avoids a new dependency tree in events-core; the protocol is markedly simpler than Discord's Gateway (no IDENTIFY budget, no resume state, no application heartbeat).
- Matrix adapter (#744) as the self-hosted answer: complementary, not a substitute - it fixes ingress only for teams that leave Slack.
- Tunnel/hosted ingress: exists today and is exactly what #749's deployment model rejects.
- Socket Mode in the hosted Worker: rejected - a persistent outbound WebSocket is unnatural in a Worker and unnecessary there (hosted has public ingress); matches the Discord precedent.

## Relevant files

### Existing (verified 2026-07-21)

- `event-server/core/src/core.ts` - Slack source registration (`:1050-1083`), `handleSlackWebhook` (`:774-805`, the transport-free pipeline entry the driver reuses), `handleSlackWorkspaceRegister` (`:2349`), `verifySlackSignature` (`:522-535`, webhook-only, untouched).
- `event-server/core/src/adapters/chat-sdk-slack.ts` - `bridgeSlackWebhook` (`:44-51`), transport-independent; untouched.
- `event-server/core/src/gateway/discord.ts` + `event-server/src/discord-gateway-local.ts` - the architecture template (sans-IO session + driver).
- `event-server/core/src/channels.ts` - `slackApiUrl()`/`setSlackApiUrl` (`:39-46`), the REST seam for `apps.connections.open` and its test stub.
- `event-server/src/local.ts` - driver wiring points: env config (`:121-128` pattern), manager construction (`:372`), `/health` (`:471-483`), registration route (`:573-595`), env-configured start (`:847-850`).
- `bobi/events/server.py` - `register_slack_workspaces` (`:653+`, gains `app_token`), `ensure_running` env pass-through (`:219-230` pattern for the new env var).
- `bobi/ingress.py` - `check_ingress_reachability` (`:82-115`) becomes transport-aware.
- `bobi/doctor.py` (`:394-415`) and `bobi/service.py` (`:182-184`) - both surfaces of the ingress check; no changes beyond what the shared function provides.
- `bobi/setup/services.py` - Slack `Secret` declarations (`:173-174`) gain optional `SLACK_APP_TOKEN`.
- `bobi/cli.py` (`:1328-1426`) + `bobi/slack_manifest.py` + `bobi/templates/slack-app.manifest.yaml` - `--socket-mode` rendering.
- `tests/test_slack_manifest.py`, `tests/test_ingress.py`, `tests/test_doctor.py` - existing suites the change extends.
- `tests/integration/test_discord_gateway.py` - the integration-test template (stubbed REST + stubbed WS server, real local event server, no live credentials).
- `skills/slack-setup.md`, `docs/SELF_HOSTED_EVENT_SERVER.md`, `docs/EVENT_SERVER.md` - docs surfaces.

### New

- `event-server/core/src/gateway/slack-socket.ts` - sans-IO Socket Mode session; no existing module owns WS protocol logic for Slack, and the Discord session is protocol-specific by design.
- `event-server/src/slack-socket-local.ts` - the local driver; parallel to `discord-gateway-local.ts` rather than merged with it because the protocols share no frames (a shared-manager refactor is deferred until a third dial-out transport exists - see Notes).
- `event-server/test/slack-socket.spec.ts` - recorded-frame session tests, mirroring `discord.spec.ts`.
- `tests/integration/test_slack_socket_mode.py` - end-to-end integration mirroring `test_discord_gateway.py`.

## Questionables

- **Q1:** Protocol implementation - (a) in-house sans-IO session mirroring the Discord pattern / (b) depend on `@slack/socket-mode` (official SDK).
  Recommendation: (a) - consistency with the Discord seam, deterministic recorded-frame tests, no new dependency tree in events-core; the protocol surface (hello, envelope+ack, disconnect) is small.
- **Q2:** App-token plumbing - (a) both signed `/slack/workspaces` registration field and `BOBI_ES_SLACK_APP_TOKEN` env fallback, mirroring Discord / (b) env-only.
  Recommendation: (a) - registration is how every other Slack credential flows, and env-only would couple socket startup to process environment in a way registration-driven workspaces are not.
- **Q3:** Ingress-check scope - (a) make the check transport-aware in this initiative, fixing the Discord false positive too / (b) special-case Slack-with-app-token only, leave Discord as-is.
  Recommendation: (a) - one mechanism, and the Discord warning is a real current defect this plan already has to touch the check for.
- **Q4:** Envelope retry handling - (a) mirror the webhook `preVerify` dedup precedent (`core.ts:1051-1064`): ack retransmissions (`retry_attempt > 0`) without re-delivering / (b) deliver retransmissions and rely on downstream idempotence.
  Recommendation: (a) - the webhook path already chose drop-on-retry for the same payloads; the transports should not diverge on dedup semantics.

## Phases

### Phase 1 - Socket Mode protocol core (sans-IO, events-core)

- [ ] `event-server/core/src/gateway/slack-socket.ts`: session state machine consuming decoded frames - `hello` (connection established, counts toward connected state), `events_api` envelope (emit ack action + deliver action carrying the inner payload), `disconnect` (`refresh_requested`/`link_disabled` etc. -> reconnect action; Slack recycles connections roughly hourly), `slash_commands`/`interactive`/unknown types (ack where an `envelope_id` is present, deliver nothing), malformed frames ignored without dying
- [ ] Retry dedup per Q4 decision
- [ ] Action vocabulary mirroring `GatewayAction` (`send`, `deliver`, `reconnect`, `connected`, `fatal`) so the driver loop stays structurally identical to Discord's
- [ ] Export from the events-core package (`event-server/core/package.json` `exports` map, alongside `./gateway/discord`)
- [ ] `event-server/test/slack-socket.spec.ts`: recorded-frame tests mirroring `discord.spec.ts` - hello/ack ordering, envelope -> deliver payload fidelity, disconnect -> reconnect, retry dedup, malformed-frame survival, ack-before-deliver ordering

**Validation gate** - do not exit this phase until every line passes; if a command fails, fix the cause and re-run.

- [ ] `cd event-server && npm test` green, including the new spec
- [ ] The new module is inert: no driver references it yet, `npm run build:local` output unchanged in behavior

### Phase 2 - Local driver and wiring

- [ ] `event-server/src/slack-socket-local.ts`: `SlackSocketManager` mirroring `DiscordGatewayManager` - per-app connection keyed by app id, `apps.connections.open` via `slackApiUrl()` with the app token, exponential backoff with jitter, handshake watchdog, generation guard for stale-socket callbacks, `health()` reporting state/last-event/connect counts
- [ ] Fatal policy: `apps.connections.open` 4xx (bad/revoked app token, missing `connections:write`) parks the connection fatal with the reason, no retry loop; transient failures back off
- [ ] Envelope delivery: each `events_api` payload -> `handleSlackWebhook(storage, JSON.stringify(payload), payload)`; ack is sent before delivery is awaited; delivery failures are logged, never block the socket
- [ ] `local.ts` wiring: manager construction, `/health` gains `slack_socket` (shape mirroring `discord_gateway`), env-configured start via `BOBI_ES_SLACK_APP_TOKEN`, and start/restart on `/slack/workspaces` registrations whose record carries `app_token`
- [ ] `handleSlackWorkspaceRegister` (`core.ts:2349`) accepts and stores the optional `app_token` field on the workspace record
- [ ] `tests/integration/test_slack_socket_mode.py` mirroring `test_discord_gateway.py`: stub `apps.connections.open` behind `BOBI_ES_SLACK_API_URL`, stub Socket Mode WS server (small Node script on the event server's `ws` dependency), real local event server; prove envelope -> normalized `slack.mention`/`slack.dm` delivery to a subscribed deployment WebSocket, ack observed at the stub before delivery completes, reconnect after a `disconnect` frame with delivery still working, self-authored bot messages filtered

**Validation gate**

- [ ] `cd event-server && npm test` green
- [ ] `pytest tests/integration/test_slack_socket_mode.py -q` green
- [ ] `/health` on a socket-configured local server shows `slack_socket` with `state: connected` against the stub

### Phase 3 - Opt-in UX, doctor, docs

- [ ] `SLACK_APP_TOKEN` declared as an optional Slack secret (`bobi/setup/services.py`); `cfg.credential("slack", "app_token")` resolves it
- [ ] `register_slack_workspaces` includes `app_token` in the registration record when configured; `ensure_running` passes `BOBI_ES_SLACK_APP_TOKEN` through to the local server env (mirroring the signing-secret pass-through, `bobi/events/server.py:219-230`)
- [ ] `bobi create-slack-bot --socket-mode`: rendered manifest has `socket_mode_enabled: true` and NO `request_url`/`event_subscriptions.request_url` line; `bot_events` unchanged; default (no flag) output byte-identical to today; the printed next-steps text explains the app-token step instead of the request URL
- [ ] Transport-aware ingress check per Q3 decision: `check_ingress_reachability` stops counting a source as webhook-inbound when its transport dials out (Slack with `app_token` configured, Discord always); warning `label`/`hint` mention Socket Mode as a remedy for Slack; both doctor and spawn surfaces inherit the fix
- [ ] Docs: `skills/slack-setup.md` gains the Socket Mode path (app-level token creation, `connections:write` scope, no request URL); `docs/SELF_HOSTED_EVENT_SERVER.md` and `docs/EVENT_SERVER.md` state the transport topology (webhooks = hosted/public, Socket Mode = self-hosted opt-in, local event server only); README channel table row if one exists
- [ ] Live smoke, env-gated: an e2e leg (pattern of `tests/e2e/test_slack_live.py`) that runs only when `SLACK_APP_TOKEN`+`SLACK_BOT_TOKEN` are present in the environment - real `apps.connections.open`, one real mention delivered end-to-end; skipped in CI

**Validation gate**

- [ ] `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` green (covers `test_slack_manifest.py`, `test_ingress.py`, `test_doctor.py` extensions)
- [ ] On an isolated `BOBI_HOME` with Slack configured for Socket Mode and `event_server_url` unset: `bobi agent <name> doctor` shows NO ingress warning; with webhook-only Slack config the warning still fires verbatim
- [ ] Same isolated check for a Discord-only agent: no ingress warning (the false positive is gone)
- [ ] `bobi create-slack-bot --socket-mode --format yaml` output inspected: `socket_mode_enabled: true`, no `request_url`

## Proof of work

- This is a feature, not a bug fix: no failing-test-first requirement; each phase names its suites above.
- Real-brain e2e judgement call (per `CLAUDE.md`): NOT required.
  The change is a transport feeding the existing event pipeline upstream of any session; the brain path (delivery from event server to a live session) is unchanged and already covered.
  The risk lives in the WS protocol handling and the driver wiring, which the recorded-frame spec and the stubbed-server integration test exercise on the real local event server - the same bar the Discord channel shipped against (`tests/integration/test_discord_gateway.py`, no live credentials).
- The env-gated live smoke in Phase 3 is the real-dependency leg for operators who hold Slack credentials; it must skip cleanly without them.
- Nothing in any phase touches `VERSION`, `pyproject.toml` version, or `CHANGELOG.md` (release rules).

## Ticket map

Filled by the split workflow after approval.

| Phase | Ticket | One-line scope | Status |
|---|---|---|---|

**Lanes:** expected shape - Phase 1 -> Phase 2 sequential (driver consumes the session); Phase 3 parallelizable with Phase 2 after the Q3 decision except its integration-gate lines, which need Phase 2 landed.

## Amendments

None yet.

## Notes

- Tracking-issue designation is deferred: #749 will be retitled/labeled as this plan's tracking issue in a follow-up step; until then discussion stays on #749 as filed, and this file is the source of truth for the design.
- #86's pipeline decision stands: Socket Mode here is a transport INTO the `normalize -> store -> fan out -> dispatch` pipeline, never a bypass; nothing touches sessions directly.
- Socket Mode apps cannot be listed in the public Slack Marketplace (Slack platform constraint) - irrelevant for manifest-created single-workspace apps, but it is the standing reason HTTP webhooks stay the default rather than being replaced.
- A shared dial-out-connection-manager refactor (Discord + Slack drivers share backoff/watchdog/generation scaffolding) is deliberately deferred until a third transport wants it; two data points do not justify the abstraction.
- Deferred follow-up: `doctor` could surface `/health`'s `slack_socket`/`discord_gateway` connection state in `_check_event_server`; out of scope here.
- If the setup web UI grows an app-token field beyond the generic secret declaration, `DESIGN.md` and `docs/FRONTEND_QA.md` govern that work; Phase 3 as scoped expects no bespoke UI.
- Prior art: #749 (this initiative's origin), #86 (webhook migration), #744 (Matrix, complementary), PR #741 (Discord Gateway, the architecture template).
