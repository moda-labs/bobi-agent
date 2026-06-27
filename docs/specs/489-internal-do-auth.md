# 489 — Require internal Worker-to-Durable-Object auth for DeploymentSession

**Issue:** moda-labs/bobi-agent#489
**Type:** security / defense-in-depth (medium+)
**Status:** spec — awaiting Zach approval before implementation

---

## Problem

`DeploymentSession` Durable Objects accept internal RPC-style requests with **no
internal auth check**. The Worker validates the *public* request (bubble auth,
deployment auth, resource grants), then calls the DO for `/init`, `/event`, and
websocket subscribe. The DO trusts any caller that reaches `DeploymentSession.fetch()`.

Today the DO is only reachable via the Worker binding, so this is latent. But if
the DO path is ever exposed through a routing/config mistake, a second binding, or
a future refactor, a caller who can reach the DO could:

- inject arbitrary events into a deployment's stream (`POST /event`),
- re-initialize / alter a deployment session (`POST /init`),
- open an event-replay websocket for a deployment it does not own.

A second concrete weakness exists **now**: the websocket subscribe path forwards
the **raw client request** to the DO unchanged (`stub.fetch(request)` —
`event-server/src/index.ts:327`). That carries the client's `Authorization:
Bearer <api_key>` into the DO. The DO does not use it today, but forwarding a
client bearer token across an internal trust boundary is exactly the kind of
ambient-authority leak we should not ship into a shared/public event server.

This is **defense-in-depth** for the Cloudflare event-server backend. It does
**not** replace bubble auth (#487), deployment auth, or resource-grant
authorization (#488). It is the innermost layer: even a caller that bypasses the
public layers still cannot talk to a DO without the internal secret.

## Solution

Require an internal Worker→DO shared secret on **all** `DeploymentSession`
entrypoints.

1. A new env binding `INTERNAL_DO_SECRET`, present in both the Worker env and the
   DO env (Cloudflare injects the same binding into both).
2. The Worker adds header `x-bobi-internal: <INTERNAL_DO_SECRET>` to **every**
   request it sends to a DO stub.
3. `DeploymentSession.fetch()` verifies that header (constant-time) at the **top**,
   before any route dispatch. Missing/invalid → `403`. Applies to `/event`,
   `/init`, and websocket upgrade.
4. The websocket subscribe path stops forwarding the raw client request. The
   Worker builds a **fresh internal `Request`** that copies only what the DO needs
   for the WS upgrade (the URL — which carries the `last_seen` query param — and
   `Upgrade: websocket`) plus the internal header, and **does not** forward the
   client `Authorization`.

Because the Worker and the DO read the **same** binding value, the header the
Worker sends always matches what the DO expects. No secret travels in a request
body, and the secret is never exposed to clients.

---

## Scope

### In scope

- `INTERNAL_DO_SECRET` binding (Worker + DO `Env` interfaces).
- Internal header on all 5 Worker→DO call sites in `event-server/src/index.ts`:
  - `deliver()` — loop-detected event (`index.ts:125`)
  - `deliver()` — drained paused events (`index.ts:139`)
  - `deliver()` — main event delivery (`index.ts:154`)
  - `initDeploymentSession()` — `/init` (`index.ts:177`)
  - websocket subscribe — currently `stub.fetch(request)` (`index.ts:327`)
- Top-of-`fetch()` verification in `event-server/src/deployment-session.ts`.
- Fresh internal `Request` for the WS upgrade (drop client `Authorization`).
- Test env wiring so the vitest workers pool provides `INTERNAL_DO_SECRET`.
- Tests for the acceptance criteria below.
- A short doc note: the secret is a **Cloudflare Worker secret only**.

### Out of scope (explicitly)

- **The local Node backend (`event-server/src/local.ts`).** It has no Worker→DO
  boundary — websocket handling and delivery mutate in-process state directly, so
  there is no internal hop to authenticate. The issue's "loopback-only event
  servers are not required" note applies here. No internal-secret plumbing is
  added to `local.ts`. (See "Local/dev" below for the one place the local *Worker*
  test path needs a value.)
- Bubble auth (#487), resource grants (#488), deployment auth — different layers.
- Any propagation of the secret to Fly apps, agent teams, `run/.env`,
  bubble state, or user/account credentials. **The secret lives only in the
  Cloudflare event-server deployment.**
- Rotation tooling / multiple-secret support. A single secret, rotated by
  re-running `wrangler secret put`, is sufficient for this hardening pass.

---

## Technical approach

### 1. Env binding

Add `INTERNAL_DO_SECRET: string` to the module-local `interface Env` in **both**
`event-server/src/index.ts` and `event-server/src/deployment-session.ts`.

- **Cloudflare production:** `wrangler secret put INTERNAL_DO_SECRET` (set once per
  Worker deployment; not committed). Document this as a deploy prerequisite.
- **`wrangler.jsonc`:** no value committed (it is a secret). Add a comment
  documenting that `INTERNAL_DO_SECRET` is required and set via `wrangler secret put`.
- **Local/dev + tests:** the vitest workers pool needs a value so the DO and the
  Worker agree. Provide it via the test pool config (miniflare binding), e.g. a
  fixed dev value `INTERNAL_DO_SECRET: "test-internal-secret"` in
  `vitest.config.mts`, and augment `test/env.d.ts`'s `ProvidedEnv` with
  `INTERNAL_DO_SECRET: string` so tests typecheck. For a manually-run local
  Worker (`wrangler dev`), use the same canonical `INTERNAL_DO_SECRET` name in
  `.dev.vars`; this is dev-only and never committed.

### 2. Worker → DO header

Centralize header construction so no call site can forget it. Define one shared
header-name constant and **two** builders — one for the POST entrypoints and one
for the WS upgrade — so every Worker→DO path goes through a helper (no raw
`stub.fetch(request)` remains):

```ts
export const INTERNAL_HEADER = "x-bobi-internal";

function internalEventRequest(env: Env, path: string, body: string): Request {
  return new Request(`https://internal${path}`, {
    method: "POST",
    headers: { [INTERNAL_HEADER]: env.INTERNAL_DO_SECRET },
    body,
  });
}

function internalWebSocketRequest(env: Env, url: string): Request {
  return new Request(url, {
    method: "GET",
    headers: { "Upgrade": "websocket", [INTERNAL_HEADER]: env.INTERNAL_DO_SECRET },
  });
}
```

Replace the four `new Request("https://internal/...", { method, body })` calls in
`deliver()` and `initDeploymentSession()` with `internalEventRequest(env, ...)`.
(`createKVStorage(env)` already closes over `env`, so the helpers are reachable.)
`INTERNAL_HEADER` is exported and imported by `deployment-session.ts` so the
header name lives in exactly one place (no string drift between sender and
verifier).

### 3. WebSocket subscribe — fresh internal request

Replace `index.ts:327` `return stub.fetch(request)` with
`return stub.fetch(internalWebSocketRequest(env, request.url))`. The fresh request
carries only what the DO reads (`url` for `last_seen`, `Upgrade: websocket`) plus
the internal header.

Because the fresh request copies **no** client headers, this drops not just
`Authorization` but **all** ambient client auth (`Cookie`, etc.) by construction —
strictly stronger than stripping one header. The DO's `handleWebSocketUpgrade`
reads only `request.headers.get("Upgrade")` (routing) and `new URL(request.url)`
`last_seen` — both preserved.

No `Sec-WebSocket-*` handshake headers are needed: the Cloudflare WS-to-DO upgrade
uses `WebSocketPair` + `acceptWebSocket`, not a network handshake. No client
subprotocol (`Sec-WebSocket-Protocol`) is negotiated today, so none is copied; if
a subprotocol is introduced later, that header must be added to
`internalWebSocketRequest` and is called out here so the omission is deliberate,
not accidental.

### 4. DO verification

At the top of `DeploymentSession.fetch()`, before route dispatch:

```ts
override async fetch(request: Request): Promise<Response> {
  const provided = request.headers.get(INTERNAL_HEADER);
  const expected = this.env.INTERNAL_DO_SECRET;
  if (!expected || !provided || !constantTimeEqual(provided, expected)) {
    return new Response(null, { status: 403 });
  }
  // ... existing route dispatch unchanged
}
```

- **Top of `fetch()`, before any body read or route match** — so even a
  route-valid request (correct path + parseable body) is rejected when the header
  is missing. Auth must never run *after* parsing/dispatch, or a parse error could
  short-circuit ahead of the gate (a fail-open ordering bug). Tests assert this
  explicitly (valid bodies, no header → 403).
- **Reuse `constantTimeEqual` from `core.ts`** (already exported, already used for
  signature verification) rather than `===`, to avoid leaking the secret via
  timing. Import it into `deployment-session.ts`. Its documented length
  early-return is acceptable here: the secret is a high-entropy, fixed
  configured value, so a length mismatch only reveals the attacker sent the wrong
  size, never the secret itself (same rationale as its existing HMAC-digest use).
- **No oracle:** the 403 returns an empty body and the verifier logs **nothing**
  about the provided/expected values. Don't turn the gate into an information
  leak for a directly-exposed DO.
- **Fail closed:** if `INTERNAL_DO_SECRET` is unset, every request 403s. A
  misconfigured deploy fails loudly (delivery stops) rather than silently
  disabling the check. This is the correct posture for a security gate and is a
  documented deploy step.

### Trust-boundary note

The secret authenticates *the Worker to the DO* (a who-are-you check on an
internal hop). It is not per-deployment and not a capability token — public
authorization (bubble/deployment/grant) still happens in the Worker first. This
layer only ensures the DO is unreachable except via the Worker.

---

## Verification plan

New tests in `event-server/test/` (likely `index.spec.ts` or a new
`deployment-session.spec.ts`), driving the DO stub directly via
`env.DEPLOYMENT_SESSION.idFromName(id)` for the "direct, no header" cases and
`SELF.fetch` for the Worker-mediated cases.

Mapping to acceptance criteria:

1. Direct `POST /event` to a DO with a **route-valid body** but **no** internal
   header → `403` (proves auth runs before parse/dispatch, not after).
2. Direct `POST /init` to a DO with a route-valid body, **no** header → `403`.
3. Direct websocket upgrade (`Upgrade: websocket`, no internal header) → `403`.
4. Direct request to an **unknown path** with no header → `403` (not `404`),
   proving the top-of-`fetch()` guard precedes routing.
5. Direct request **with a wrong** internal header value → `403` (guards the
   constant-time compare, not just header presence).
6. Direct `/init` **with the correct** internal header → succeeds, proving the
   gate is value-checked, not always-deny. (Kept narrow — `/init` with a minimal
   body — and *not* wrapped in any reusable helper, so the test never reads as a
   sanctioned "talk to a DO directly" API outside the test file.)
7. Worker-mediated delivery still succeeds across **all three** `deliver()` call
   sites — main delivery (existing `#341` routing tests), drained-paused
   redelivery, and the loop-detected event. Each path must be exercised, not just
   main delivery.
8. Worker-mediated `/init` (deployment registration → `initDeploymentSession`)
   still succeeds.
9. Worker-mediated websocket subscribe still succeeds after public deployment
   auth even when the client sends `Authorization` **and** `Cookie`; the DO-bound
   request carries the **internal** header and **neither** client credential.
   Observed via a unit test on `internalWebSocketRequest` (assert built headers =
   `Upgrade` + internal only) rather than a DO-internal probe, since the DO does
   not — and must not — expose received headers.
10. Secret **unset** in the test env makes Worker-mediated calls fail loudly
    (403), proving a misconfiguration cannot silently bypass the gate.
11. A search/lint guard test asserts no raw `stub.fetch(request)` to
    `DEPLOYMENT_SESSION` remains in `index.ts` — every Worker→DO call must go
    through an `internal*Request` helper (catches future call sites).

Plus: `npm test` (vitest) green, `tsc` typecheck clean.

---

## Implementation plan

1. Add `INTERNAL_DO_SECRET` to both `Env` interfaces; wire the test pool value
   (`vitest.config.mts`) + `test/env.d.ts` augmentation; comment `wrangler.jsonc`.
2. Write the failing tests for acceptance criteria 1–8 (TDD).
3. Add `internalEventRequest` helper + the `INTERNAL_HEADER` constant; convert the
   four event/init call sites.
4. Rebuild the WS subscribe path as a fresh internal request (drop client auth).
5. Add the top-of-`fetch()` verification in `deployment-session.ts` (import
   `constantTimeEqual`).
6. `npm test` + typecheck green; `/review`.
7. Doc note (`docs/FRIENDS-FAMILY-SECURITY-TODO.md` checkbox + one line on the
   Worker-secret-only constraint).

## Release / coordination notes

- **No version bump, no `CHANGELOG.md` edit** (feature PR — bobi release policy).
- **Trio merge order: #487 → #488 → #489.** #489 is a distinct layer (internal DO
  auth) and does not depend on #487/#488 logic, so spec + impl proceed in
  parallel. The only overlap is co-editing `event-server/src/index.ts` (Worker→DO
  call sites). After #487 and #488 land, **rebase the #489 PR on main** — a
  mechanical `index.ts` rebase (different lines: those PRs touch the public-auth
  surface; this one touches the DO call sites).
- Hardening blocker before enabling shared/public Cloudflare event-server mode;
  tracked from `docs/FRIENDS-FAMILY-SECURITY-TODO.md`.

### Operational notes (from adversarial review)

- **Rotation is a brief-outage operation, not zero-downtime.** A single secret
  rotated via `wrangler secret put` can momentarily desync: a Worker isolate
  running new code/env may send the new secret to a DO isolate still holding the
  old env (or vice versa), 403-ing delivery/websockets during the propagation
  window. Acceptable for this hardening pass (rotation is rare and operator-driven)
  but **must be documented** at the rotation step. Zero-downtime rotation (accept
  two secrets during a window) is explicitly deferred — out of scope.
- **Production must not ship the dev value.** The test/dev binding value (e.g.
  `"test-internal-secret"`) must never reach production; prod uses a strong,
  randomly generated secret via `wrangler secret put`. Fail-closed catches an
  *unset* secret but not a *weak* one — call this out in the deploy runbook.
- **One env-var name only.** The binding is `INTERNAL_DO_SECRET` everywhere
  (prod secret, `.dev.vars`, test pool). The issue's `BOBI_ES_INTERNAL_SECRET`
  refers to the local Node backend, which is out of scope here; we do **not**
  introduce a second env-var name into the Worker/DO code path — `INTERNAL_DO_SECRET`
  is canonical, no aliasing/mapping.
