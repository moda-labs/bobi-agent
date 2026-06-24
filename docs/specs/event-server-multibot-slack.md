# Multi-bot Slack support in the event server

**Status:** core implemented (2026-06-24) — loop-safety + per-app signing +
multi-bot registration landed with tests; rollout (Worker deploy + secrets +
bobbers redeploy) pending. Cross-talk routing (#4) deferred — see Implementation
notes. **Tickets:** #239 (auth-v2 / multitenant Slack), #215 (per-deployment
identities). Related loop-safety: #232 (batch dedup), #299, #463 (the incident
thread).

## Implementation notes (2026-06-24)

Taken over and implemented in one session. Two intentional divergences from the
original design, both keeping the spec's intent:

1. **Self-filter stays at normalize, keyed by `api_app_id`** (not moved to
   per-deployment fan-out). Slack delivers a *separate* signed webhook per app
   (each carries its own `api_app_id`), so a normalize-time decision is already
   per-app-correct — `getSlackBotForApp(ws, api_app_id).bot_id` resolves THIS
   app's own id, which a second bot can no longer clobber. Avoids needing
   `DeploymentRecord.identities` plumbing for the loop fix. The breaker (now
   `bot_id`-aware) is the per-conversation backstop.
2. **Cross-talk (#4) deferred.** It does not bite the live topology: `modabot`
   is channel-scoped (`slack:<team>:<chan>`) and `bobbers` is DM-only, so they
   share no channel/DM and never receive each other's events. A full fix
   (app-qualified topics + per-app subscription) is a larger routing change;
   tracked as follow-up, not required for the incident or for the two teams to
   coexist.

Storage record is now `team_id → { bots: { [api_app_id]: { bot_token, bot_id,
signing_secret, app_id } }, …legacy }` with read-migration of the old
single-bot shape (so `modabot` keeps working pre-redeploy). Sites:
`core.ts` `SlackBotRecord`/`getSlackBotForApp`/`resolveSlackSigningSecret`/
`slackSigningSecretFor`, `handleSlackWebhook`, `handleSlackWorkspaceRegister`
(upsert + `bots.info` app_id), `handleSlackSend` (token-by-bot); per-app
signature in `index.ts` + `local.ts`; `bot_id` preserved in
`adapters/slack.ts` (+ dead `chat-sdk-slack.ts`); Python
`register_slack_workspaces` sends `app_id` + `signing_secret`; both team
`agent.yaml`s gained `signing_secret: ${SLACK_SIGNING_SECRET}`. Tests:
`event-server/test/{core,circuit-breaker}.spec.ts`,
`tests/test_event_subscription.py` (all green: 191 TS + 2199 py unit).

## Problem

The event server cannot host **two Slack apps/bots in one workspace**. All
Slack state is keyed by `team_id` with last-writer-wins, so a second bot's
registration clobbers the first's, and inbound signature validation uses a
single global secret. This is the deferred multitenant work; it surfaced (and
caused a prod incident) when a second app was deployed into a workspace that
already had one.

### How it surfaced

- **Prod incident (2026-06-24):** `moda-eng-team` (`modabot`, bot_id
  `B0B4ZRGBT5F`) spammed `#bobi-eng-team` (PR #463) with ~20 "Evaluating…"
  placeholder messages, ~1/4s. Deploying a second app (`bobbers`) into the same
  workspace (`T0952RZRZ0X`) against the same `MODASTACK_EVENT_SERVER` overwrote
  modabot's stored `bot_id`, so modabot's own placeholder messages stopped
  matching `selfBotId` → it replied to itself → loop. The circuit breaker that
  should have capped it never fired.
- **Bobbers deploy:** the second bot's inbound events were `401`-rejected by the
  Worker (signed with bobbers' signing secret; Worker validates one global
  secret), so its login-code DM never arrived. Verified by probe: a real event
  with a non-matching signature → `HTTP 401`; `url_verification` bypasses.

## Root causes (and which bit us)

1. **`selfBotId` clobber (BIT US — caused the loop).** The self-reply filter
   `adapters/slack.ts:23` (`event.bot_id === selfBotId`) resolves `selfBotId`
   per *workspace* (`core.ts:490-493` `getSlackWorkspace(teamId)`), and
   registration stores one bot per workspace (`core.ts:722 putSlackWorkspace`,
   last-writer-wins). Two bots → the second's id overwrites the first's →
   the first no longer recognizes its own messages.
2. **Single global signing secret (BIT US — 401'd inbound).**
   `index.ts:272` `verifySlackSignature(env.SLACK_SIGNING_SECRET, …)` (and the
   local mirror `local.ts:71`). A second app's events fail signature → dropped.
3. **`bot_id` stripped on normalize (LATENT — broke the backstop).**
   `normalizeSlackWebhook` does not carry `bot_id` into the normalized event's
   `fields`/`payload`, so `circuit-breaker.ts:122` (`innerEvent.bot_id ??
   payload.bot_id`) always sees "human", its window resets, and it never trips.
4. **Bare-topic fan-out / cross-talk (LATENT — bites next).** Both bots
   subscribe to `slack:<team>`; every event fans to every bot. DMs are
   bare-topic-only (`adapters/slack.ts:59` skips the channel-scoped topic for
   DMs), so a user's DM to bot A is also delivered to bot B.

## Design

Key Slack state by **`api_app_id`** — the only identifier present on every
inbound `event_callback` *and* unique per app (two bots in one workspace share
a `team_id`, so `team_id` cannot be the key). Store the full per-bot record,
not just the id:

```
team_id → { [api_app_id]: { bot_token, bot_id, signing_secret } }
```

Self-filtering moves from a single normalize-time skip to **per-deployment at
fan-out**: deliver an event to a deployment unless `event.bot_id` equals that
deployment's own `bot_id`. `DeploymentRecord` already reserves an `identities`
field (`core.ts:38`, for #215) — that is where a deployment's `bot_id`/`app_id`
lives.

Everything is **backward-compatible**: fall back to `env.SLACK_SIGNING_SECRET`
when an app has no registered per-app secret, and read-migrate the existing
single-bot record so `modabot` keeps working without a redeploy.

## Fix checklist (by area, with sites)

### Storage / shape
- [x] `SlackWorkspaceRecord` + `SlackBotRecord` (`core.ts`) → per-app map keyed
      by `api_app_id` with `{bot_token, bot_id, signing_secret, app_id}`.
- [x] KV + local backends store the richer record as-is (JSON); test storages
      updated.
- [x] Read-migration via `getSlackBotForApp` (legacy single-bot + single-entry
      fallback).

### Inbound signature validation
- [x] `index.ts` + `local.ts`: `slackSigningSecretFor(storage, payload, env)`
      resolves the authoring app's secret by `api_app_id`, falling back to the
      global `SLACK_SIGNING_SECRET`.

### Self-reply filter + breaker
- [x] `normalizeSlackWebhook` (`adapters/slack.ts`): **preserves `bot_id`** on
      `fields` + `payload`. Same fix applied to dead `adapters/chat-sdk-slack.ts`.
- [x] Self-skip kept at normalize but keyed per-app via `getSlackBotForApp(ws,
      api_app_id).bot_id` (see Implementation notes for why this is per-app-
      correct without fan-out plumbing).
- [x] Breaker now sees `bot_id`; covered by a test that drives REAL normalizer
      output (not the fictional nested shape) through `recordDelivery`.

### Routing (cross-talk)
- [ ] **DEFERRED** (follow-up). Does not bite the live topology (modabot
      channel-scoped, bobbers DM-only). Full fix = app-qualified topics +
      per-app subscription. See Implementation notes.

### Outbound send
- [x] `handleSlackSend`: selects the bot_token by `app_id` → `bot_id` → single
      bot → legacy workspace token.

### Registration
- [x] `handleSlackWorkspaceRegister`: **upserts** one entry into the per-app
      map (migrates a legacy record in), accepts + stores `signing_secret`.
- [x] Sources `api_app_id` via `bots.info?bot=<bot_id>` (server fallback) and
      from the client (Python resolves it).

### Python / client + config
- [x] `register_slack_workspaces` (`modastack/events/server.py`) resolves
      `app_id` (`bots.info`) and passes `app_id` + `signing_secret`.
- [x] slack service in both `agent.yaml`s gains
      `signing_secret: ${SLACK_SIGNING_SECRET}`; deploy materializes it as a Fly
      secret (`--env-file` reconcile already handles `${VAR}` refs).
- [x] `auth_bootstrap._wait_for_code` inherits it via the same register call.
- [ ] Already landed in `agents/personal-assistant`: `channels:
      ${SLACK_CHANNELS:-}` (optional) + a comment that a **DM id must never** go
      in `SLACK_CHANNELS` (DMs ride the bare `slack:<team>` topic, not the
      channel-scoped one — `adapters/slack.ts:59`).

## Tests (write failing-first, per CLAUDE.md)

Assert on the **real** `event_callback` shape (the incident shipped because
tests used a shape the live adapter never emits):

- [x] Two bots, one workspace: each bot's OWN message is filtered (keyed by
      api_app_id); a third-party bot passes through; registering a 2nd bot does
      not clobber the 1st. (`core.spec.ts`)
- [x] Per-app signature resolution: app A's secret used for A; unknown app and
      legacy record fall back to the global secret. (`resolveSlackSigningSecret`
      tests) — full HTTP A-vs-B reject path covered by the resolution unit +
      existing `index.spec` signature tests.
- [x] Breaker trips on a bot-authored loop using REAL normalizer output once
      `bot_id` is preserved. (`circuit-breaker.spec.ts`)
- [ ] **Cross-delivery (esp. DMs)** — deferred with the routing fix above.

## Notes / recovery

- **Recover a live loop:** `fly machine stop` the looping instance; re-register
  the legitimate bot (`POST $ES/slack/workspaces {workspace_id, bot_token,
  bot_id}` — idempotent, same as startup); stop the intruding bot; `fly machine
  start`; confirm the thread's `reply_count` stops growing and no
  `chat.postMessage`/`Evaluating` in logs.
- The personal-assistant test deployment (`personal-bobbers`, Fly) is **stopped**
  (app + volume + secrets kept) pending this fix, then a redeploy with
  `SLACK_SIGNING_SECRET` set.
