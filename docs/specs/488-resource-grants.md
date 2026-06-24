# Spec: Authorize webhook topic subscriptions with upstream resource grants (#488)

> **Agent-authored design spec — pending human approval.** Written for #488 by
> the engineer agent. Reviewed via the eng / scope lenses and an adversarial
> codex pass (see Review notes). **Not self-approved** — routing to the director
> for Zach's sign-off before any implementation. This spec is a **superset** of
> the original issue; the original issue text is preserved verbatim at the
> bottom.
>
> **Dependency on #487 (`wf-issue-lifecycle-project-487`).** #488 reuses the
> bubble-HMAC auth machinery #487 adds to the event server
> (`readBubbleAuthHeaders` / `authenticateBubble` / `hasBubbleSignature` /
> `hasPartialBubbleSignature`) and explicitly builds its Slack path on #487's
> bubble-scoped workspace registration. **Merge order: #487 lands first.** This
> spec branches off `origin/main` (which does not yet contain #487); the
> implementation PR will be **rebased on `main` after #487 merges** and will
> import #487's helpers rather than re-defining them. If #487's helper names or
> the `slack_workspace:{bubbleId}:{workspaceId}` key shape change before it
> merges, this spec's §3 / §6 follow them.

> **Revision 2 (2026-06-24)** — folds in an adversarial codex review. Net
> changes: (1) **GitHub verification hardened** — a bare `GET /repos` returns
> `2xx` for *public* repos to any valid token, so the grant now additionally
> requires `private == true` **or** a minimum permission from the response
> `permissions` object (§3.2, open Q4); (2) **resource normalization** —
> lowercase + strip `.git` for github, so the topic and grant key never diverge
> on case/alias (§3.3); (3) **the security boundary is delivery (fail-closed)**,
> registration-skip is operational, not a fail-open hole — clarified, because
> delivery re-checks live grants regardless of the index (§2, §3.4); (4) a
> **deployment↔bubble binding invariant + test** is made explicit — #487 already
> derives `DeploymentRecord.bubble_id` from the *authenticated* bubble (JOIN
> needs the bubble key) and update-subscriptions namespaces from the *stored*
> record, so the "register under a victim bubble id" bypass is structurally
> closed; #488 must not regress it (§5, test 13); (5) **KV eventual-consistency**
> handling — authorize→register may race KV propagation; delivery is the
> authoritative gate and the client retries (§3.6); (6) new security notes on
> the **credential trust assumption** (TLS-required, trusted operator, prefer
> fine-grained/short-lived tokens) and **inbound webhook signatures already
> verified** (pre-existing, orthogonal) (§5); (7) **known limitation: `linear:TEAM`
> is workspace-ambiguous** (team keys are unique per workspace, not globally) —
> a pre-existing topic-format under-scoping, flagged with a follow-up, not fixed
> here (§4, §9 Q3); (8) revocation/expiry restated as deferred with the explicit
> caveat that "current grants" means current *stored* grants, not live upstream
> access (§4).

---

## 1. Problem

Webhook resource topics — `github:owner/repo`, `linear:TEAM`, `slack:T123` — are
**global** in the event server. The router treats them as cross-bubble on
purpose today:

```ts
// event-server/src/core.ts
const GLOBAL_TOPIC_PREFIXES = ["github:", "linear:", "slack:"];
export function namespaceSubKey(bubbleId, key) {
  if (isGlobalTopic(key)) return key;   // <- NOT namespaced by bubble
  ...
}
```

So `subscriptions:github:owner/repo` is a single list of deployment IDs across
**every** bubble, and `deliver()` fans an inbound webhook out to all of them.
Resource names are not secrets (a repo slug, a Linear team key, a Slack team id
are all guessable/observable), so on a shared/public event server **any bubble
can subscribe to another tenant's resource topic by naming it and receive that
resource's webhook events** — a cross-bubble read hole. The code comments even
flag it as an accepted hole "to be closed by #239 (inbound subscription auth)";
#488 is that closure.

## 2. Solution (goal + invariant)

A bubble may subscribe to / receive inbound webhook events for a resource only
if the event server has **server-verified** that the bubble has upstream access
to that resource. The agent already needs a GitHub token / Linear API key /
Slack credential to read the upstream; the server uses that credential **once**
to verify access, then stores a **bubble-scoped grant** — never the credential.

```
valid bubble + server-verified upstream access to resource
    = may subscribe to / receive that resource's webhook events
```

Enforced at **both** layers so neither alone can be bypassed:

1. **Registration / update** — adding a global resource topic to a deployment's
   subscriptions requires a matching grant for the authenticated bubble.
2. **Delivery** — every inbound webhook is filtered by the **current** grants at
   delivery time, so a stale subscription-index entry (grant since revoked, or
   written before enforcement existed) can never bypass authorization.

Back-compat is preserved by having the **client authorize its own resources at
startup** with the credentials it already holds (it already auto-detects
`github:owner/repo` from the git remote and `linear:TEAM` from the Linear API).
A single-bubble local deployment authorizes its own repo/team and keeps working;
a guessing bubble has no credential, cannot obtain a grant, and is blocked.

## 3. Technical approach

### 3.1 New endpoint: `POST /resources/authorize`

Request body (bubble-signed, exact-bytes per the #487 signing contract):

```json
{ "service": "github", "resource": "owner/repo", "credential": "<token>" }
{ "service": "linear", "resource": "TEAMKEY",   "credential": "<linear api key>" }
```

Server flow (`handleAuthorizeResource` in `core.ts`, wired in `index.ts` +
`local.ts` exactly like #487 wires `/slack/send`):

1. Read raw body, parse JSON. Build `BubbleAuthContext` via
   `readBubbleAuthHeaders`. **Require a full, valid bubble signature**
   (`authenticateBubble`); unsigned / partial / bad-sig ⇒ opaque `403`. (Unlike
   #487's `/slack/workspaces`, auth here is **mandatory**, not optional — there
   is no legacy unsigned caller.)
2. Validate `service ∈ {github, linear, slack}` and a non-empty `resource`.
3. **Verify the credential against the upstream service** (§3.2). Failure ⇒
   `403` (opaque; the server never says whether the resource exists, only
   pass/fail).
4. On success, **store only a grant** (§3.3). Respond `200 {ok:true}` (no echo
   of the credential).
5. **Never log the credential or the raw body.** The route is excluded from any
   body logging; verification errors are logged as a service + reason only,
   never with the token.

`/resources/authorize` is **idempotent** — re-authorizing an existing grant
re-verifies upstream and is a no-op write. The client re-authorizes on every
startup, which is what re-populates an in-memory local server's grants after a
restart.

### 3.2 Upstream verification (MVP)

| service | check | pass condition |
|---|---|---|
| `github` | `GET https://api.github.com/repos/{owner}/{repo}` with `Authorization: Bearer <credential>`, `User-Agent: modastack-event-server` | HTTP `2xx` **and** entitlement check (below) |
| `linear` | GraphQL `POST https://api.linear.app/graphql`, `Authorization: <credential>`, query `{ teams(filter:{key:{eq:"TEAMKEY"}}){ nodes { id key organization { id } } } }` | a node with the requested key is returned |
| `slack` | **converge on #487's flow** — no separate verify call (§6) | bubble-scoped workspace record (proving the bot token + signing secret) exists for this team |

Verification uses `fetch` (available in both the Worker and the Node runtime).
A non-2xx, a network error, or a 404 (`owner/repo` not visible to the token)
all ⇒ verification failure ⇒ `403`.

**GitHub entitlement — beyond bare `2xx`.** `GET /repos/{owner}/{repo}` returns
`2xx` for a *public* repo to **any** valid token (or even an over-permissioned
one), so `2xx` alone would let any bubble grant itself a public repo's events.
The response includes `private: bool` and a `permissions: {admin, push, pull}`
object. The grant therefore requires `2xx` **and** (`private == true`
**or** `permissions.push == true`) — i.e. either the repo is private (so read
access is itself meaningful) or the token has more than drive-by pull access.
**Recommended default; confirm in open Q4** — the issue's MVP says "require
success," so this is a deliberate tightening.

**Linear** verification returns the team's `organization.id`; the team key is
unique only *within* a workspace (see §4 known limitation). `resource` is
validated as a non-empty team key before the call. GitHub `resource` is
validated as `owner/repo` shape and **normalized** (§3.3) before the call.

### 3.3 Storage shape

New `StorageAdapter` methods (KV-backed in the Worker, `Map`-backed in
`local.ts`), mirroring the existing bubble/workspace methods:

```ts
putResourceGrant(grant: ResourceGrant): Promise<void>;
hasResourceGrant(service: string, resource: string, bubbleId: string): Promise<boolean>;
getDeploymentById(id: string): Promise<DeploymentRecord | null>;  // needed by delivery filter
```

Keys (Worker KV; `local.ts` uses the analogous in-memory maps):

```
resource_grant:{service}:{resource}:{bubble_id}      -> ResourceGrant (JSON)
resource_grants_for_bubble:{bubble_id}               -> [grant ids]   (deregister/observability)
```

`ResourceGrant` record — shaped now for the future account system so we never
bake "bubble == user":

```ts
interface ResourceGrant {
  id: string;
  account_id: string | null;   // null for now; account layer fills it later
  bubble_id: string;
  service: "github" | "linear" | "slack";
  resource: string;
  granted_by: "upstream_token_verification";
  created_at: string;
  expires_at: string | null;   // null = no expiry in MVP
}
```

`{service}:{resource}` is parsed from a global topic key by splitting on the
**first** `:` (`github:owner/repo` ⇒ service `github`, resource `owner/repo`);
the resources here (`owner/repo`, a Linear team key, a Slack team id) never
contain a `:`. This is the inverse of how `createTopicEvent` builds the topic
and is the single helper `parseGlobalTopic(key)` used by both enforcement
layers. **Resource normalization** is applied identically at authorize time and
at topic construction so the grant key and the topic never diverge: github
`owner/repo` is lowercased and a trailing `.git` stripped (matching the git-remote
slug the adapter already produces); a `github:`/`linear:` with an empty resource
is rejected (`400`). The grant key is the normalized form.

### 3.4 Enforcement layer 1 — registration / update

In `handleRegisterDeployment` and `handleUpdateSubscriptions`, for every
requested subscription where `isGlobalTopic(sub)` is true, check
`hasResourceGrant(service, resource, bubble.id)`:

- **grant present** → add to the subscription index as today.
- **grant absent** → **skip** that one topic (do not write the index entry) and
  return it in a `skipped_unauthorized: string[]` field on the response so the
  client/caller can see what was withheld. Non-global topics are unaffected.

Skip (not hard-reject) is chosen so a deployment subscribing to a mix of
authorized and unauthorized topics still registers its authorized + non-global
topics rather than failing wholesale — and because the client's normal startup
order (authorize → then register, §3.6) means a legitimately-authorized topic is
never skipped. **This is not a fail-open hole:** the actual security boundary is
**delivery** (§3.5), which re-checks the live grant for every event regardless
of what the index says. A client that ignores `skipped_unauthorized` and
*believes* it is subscribed simply receives nothing — fail-closed. Registration
gating is the early/cheap layer; delivery is authoritative. (Open Q2 asks
whether to hard-reject instead, for louder operational feedback.)

The MINT path (`modastack start`'s first boot) registers `_bootstrap` and other
non-global topics, which are never gated; global topics arrive after the bubble
exists and has authorized (§3.6), so mint is unaffected.

### 3.5 Enforcement layer 2 — delivery

`deliver()` (both `index.ts` KV adapter and `local.ts` Map adapter): after
resolving candidate deployment IDs from the subscription index, **for global
resource topics only**, filter each candidate by a live grant check:

```
for each candidate deployment id:
    dep   = getDeploymentById(id)            // local already has it in-hand
    {service, resource} = parseGlobalTopic(matchedGlobalTopicKey)
    if not hasResourceGrant(service, resource, dep.bubble_id): drop it
```

Only deployments whose bubble **currently** holds a matching grant receive the
event. This makes the grant — not the subscription index — the source of truth,
so a stale index entry (or one written before this feature shipped) cannot
bypass. Non-global delivery is unchanged. The filter runs only when the event
carries a global topic, so the common bubble-scoped path pays nothing.

> **Multiple bubbles, same resource:** each bubble that independently verifies
> `owner/repo` gets its own `resource_grant:github:owner/repo:{bubble_id}`, and
> the delivery filter admits each independently — so N tenants can all legitimately
> receive the same repo's events, while a guessing bubble with no grant gets none.

### 3.6 Client side (Python) — auto-authorize at startup

New `authorize_resources(...)` in `modastack/events/server.py` (sits beside
`register` / `register_slack_workspaces`, signs with `signing.sign_headers`):

- For each detected global resource subscription (`github:owner/repo`,
  `linear:TEAM`), read the matching credential from `Config.credential(...)`
  (the same token the adapter used to detect the resource) and `POST
  /resources/authorize` bubble-signed. Best-effort + logged per resource:
  a single resource's failure (missing/expired token) must not abort startup;
  that topic simply won't be subscribed/delivered, and the failure is logged
  loudly (and surfaced in `skipped_unauthorized`).
- Slack continues to call `register_slack_workspaces` (already bubble-signs in
  #487); the server derives the slack grant from that (§6).

**Startup sequence** (the only ordering constraint):

```
ensure_bubble()                # mint/join  (#487)
authorize_resources()          # NEW: verify creds -> grants
register()/update_subscriptions()   # global topics now have grants -> accepted
```

Call sites that register the persistent deployment with auto-detected
subscriptions — `modastack/inbox.py` (~L202-220), `modastack/subagent.py`
(~L1029-1129), and `modastack/auth_bootstrap.py` — invoke
`authorize_resources()` between `ensure_bubble()` and `register()`.

**KV eventual consistency.** Cloudflare KV is eventually consistent; a grant
written by `/resources/authorize` may not be globally visible the instant
`register()` reads it, so the registration-layer check could transiently skip a
just-authorized topic. Mitigations: (a) delivery (§3.5) is the authoritative
gate and re-reads grants per event, so a missed registration only delays, never
denies once propagated; (b) `authorize_resources()` and the subsequent
`register()`/`update_subscriptions()` run in-process back-to-back, typically on
the same PoP (read-your-writes within a PoP is the common case); (c) the client
treats a global topic appearing in `skipped_unauthorized` *despite* a successful
authorize as retryable (one bounded re-`update_subscriptions` after a short
delay). The local Node adapter is strongly consistent (in-memory), so this is a
Worker-only consideration.

## 4. Scope

**In scope**
- `POST /resources/authorize` (Worker + local Node), mandatory bubble auth.
- GitHub + Linear upstream credential verification; Slack convergence onto
  #487's bubble-scoped workspace registration.
- `ResourceGrant` storage (KV + in-memory), grant + per-bubble index.
- Two-layer enforcement: registration/update skip-if-no-grant, delivery filter.
- Python `authorize_resources()` + wiring into the three startup call sites.
- Tests (§7).

**Out of scope (explicitly deferred)**
- The account system (`account_id` is stored as `null`; no account endpoints).
  We only ensure the grant shape and enforcement don't assume `bubble == user`.
- Grant **revocation** API / expiry enforcement (`expires_at` is stored but not
  checked in MVP) and **periodic re-validation against upstream**. Consequence,
  stated plainly: delivery filters on the current **stored** grant, which is
  *not* the same as current live upstream access — a token that verified once
  then lost upstream access keeps its grant until the grant is deleted (local:
  server restart + re-authorize failing; Worker: a future revoke/expiry pass).
  A revoke endpoint + `expires_at` enforcement + re-validation are the named
  follow-up. Grant cleanup on deployment-deregister/bubble-teardown is cheap and
  *may* be included opportunistically, but full lifecycle is deferred.
- **Fixing the `linear:TEAM` workspace ambiguity** (see limitation below).
- Replay-nonce dedup (inherited deferral from the #487/#240 signing contract).
- Reverse `resource -> bubbles` index / delivery perf optimization (§8).
- Any change to non-resource (bubble-scoped) topic routing, to inbound webhook
  signature verification, or to the outbound deployment transport (delivery is
  to the deployment's own Durable Object / WebSocket, reachable only by the
  holder of its api_key — unchanged).

**Known limitation (pre-existing, surfaced not fixed): `linear:TEAM` is
workspace-ambiguous.** Linear team keys (e.g. `ENG`) are unique only *within* a
workspace, so two Linear workspaces can both have team `ENG` and both map to
topic `linear:ENG`. A grant for `linear:ENG` thus gates whichever workspace's
webhooks arrive under that topic — the topic format under-scopes the resource.
This predates #488 (the topic is built as `linear:{team_key}` with no org id).
#488 verifies and records the team's `organization.id` in the grant so a future
fix can disambiguate, but does **not** change the topic format here; see Q3.

## 5. Security considerations

- **Credential never persisted, never logged.** Only the grant is stored. The
  `/resources/authorize` route is excluded from body logging; verification
  failures log `{service, reason}` only.
- **Mandatory bubble auth** on `/resources/authorize` with the existing opaque
  `403` + constant-time-on-miss behavior (`authenticateBubble` already does a
  dummy HMAC on bubble-miss), so the endpoint can't be used to enumerate bubbles.
- **Opaque verification result** — a `403` does not distinguish "resource
  doesn't exist" from "your token can't see it," so the endpoint isn't a
  resource-existence oracle beyond what the upstream API already leaks to a
  valid token.
- **Delivery is the backstop.** Even if a stale/forged index entry exists,
  delivery re-checks the live grant, so authorization can't be bypassed by
  manipulating the subscription index alone.
- **SSRF / input shape** — `service` is whitelisted and `resource` is
  shape-validated + normalized before any upstream call; the upstream hosts are
  fixed constants (`api.github.com`, `api.linear.app`), never client-supplied.
- **Deployment↔bubble binding (no register-as-victim bypass).** Delivery checks
  `hasResourceGrant(service, resource, dep.bubble_id)`, so it is only as sound as
  the binding between a deployment and its bubble. That binding is already
  structural in #487: a JOIN must be signed with the bubble's key and
  `DeploymentRecord.bubble_id` is set from the **authenticated** bubble (never a
  client-supplied field); `handleUpdateSubscriptions` namespaces from the
  **stored** record. An attacker therefore cannot register/point a deployment at
  a victim bubble without that bubble's key. #488 must not regress this — test 13
  pins it.
- **Credential trust assumption (inherent to the issue's design).** The agent
  sends its upstream credential to the event server for one-time verification.
  This trusts: (a) **TLS** to a non-loopback server (the client already refuses
  to transmit the bubble key over cleartext — the same guard applies); (b) the
  **server operator** (a malicious operator sees the credential in transit/memory
  regardless of "don't persist/log"). We mitigate blast radius by storing/logging
  nothing and recommend callers supply **fine-grained / short-lived** tokens
  (GitHub fine-grained PAT scoped to the repo, rotateable Linear key) rather than
  broad ones. The credential lives only in the request handler's stack; the route
  is excluded from body logging and from any error object that would echo headers.
- **Inbound webhook authenticity is pre-existing & orthogonal.** The event server
  already verifies inbound GitHub (`x-hub-signature-256` vs `WEBHOOK_SECRET`) and
  Slack (`x-slack-signature`) signatures before normalizing/delivering. #488 gates
  *who* a verified event fans out to; it does not change inbound verification.
- **Replay** of a captured `/resources/authorize` request is possible until the
  platform-wide signing-nonce dedup ships (deferred with #487/#240); a replay only
  re-creates a grant the bubble was already entitled to, so marginal risk is low.

## 6. Slack convergence (builds on #487)

#487 already writes a **bubble-scoped** workspace record
(`slack_workspace:{bubbleId}:{workspaceId}`) when `/slack/workspaces` is
bubble-signed. For #488, `handleSlackWorkspaceRegister`, when called with an
authenticated `bubbleId`, **also writes a `resource_grant:slack:{teamId}:{bubbleId}`**
— the signed registration (proving possession of the bot token + signing secret)
*is* the proof of access, so no separate verify call is needed. `slack:{teamId}`
inbound delivery is then grant-filtered by §3.5 exactly like github/linear. The
**global** `slack_workspace:{teamId}` record and its inbound self-reply-loop
prevention (`handleSlackWebhook`) are untouched — loop prevention is orthogonal
to delivery gating.

## 7. Verification plan (tests first — TDD)

**Event-server (`vitest`, `event-server/test/`):**
1. `/resources/authorize` unsigned / partial-sig / bad-sig ⇒ `403`, no grant written.
2. GitHub verify: stubbed `fetch` 2xx ⇒ grant written; 404/401 ⇒ `403`, no grant.
3. Linear verify: team key present in GraphQL result ⇒ grant; absent ⇒ `403`.
4. Credential never appears in the stored record nor in any captured log line.
5. **Registration:** bubble with a grant subscribing to `github:owner/repo` is
   indexed; bubble without ⇒ topic skipped + listed in `skipped_unauthorized`;
   non-global topics in the same request still register.
6. **Delivery, the headline AC:** a `github:owner/repo` webhook is delivered
   only to deployments whose bubble holds the grant — a **stale index entry**
   for a grant-less bubble is **not** delivered to (inject the index entry
   directly, assert zero delivery).
7. **Multi-bubble:** two bubbles each authorize `owner/repo`; both receive the
   event; a third (no grant) receives nothing.
8. Slack: signed `/slack/workspaces` writes the slack grant; `slack:{teamId}`
   delivery is grant-filtered the same way.
9. Both runtimes: each enforcement test runs against the `local.ts` Map adapter
   and the `index.ts` KV adapter (mirror #487's dual-route test layout).
13. **Deployment↔bubble binding (codex's headline bypass):** a JOIN/register
    claiming another bubble's id without that bubble's key is rejected (`403`),
    and `update_subscriptions` namespaces from the *stored* record — so a
    deployment cannot be pointed at a victim bubble's grants. (Pins the #487
    invariant #488 relies on; assert it still holds post-rebase.)
14. **GitHub public-repo tightening:** a `2xx` for a *public* repo with
    `permissions.push == false` ⇒ no grant (and a private repo, or `push:true`,
    ⇒ grant) — guards the §3.2 entitlement check.

**Python (`pytest`, `tests/`):**
10. `authorize_resources()` signs the request and posts per detected resource;
    a missing credential for one resource logs + skips without aborting startup.
11. Startup order: `authorize_resources` runs before `register`, so a detected
    `github:`/`linear:` topic ends up subscribed (faked event server).
12. **Single-bubble back-compat:** a local deployment that authorizes its own
    detected repo/team still receives those events end-to-end (existing
    local-flow tests keep passing).

## 8. Implementation plan (ordered; after #487 merges + rebase)

1. Rebase `agent/488` on `main` (post-#487); import #487's auth helpers.
2. `core.ts`: `ResourceGrant` type, `parseGlobalTopic`, `StorageAdapter`
   additions (`putResourceGrant`, `hasResourceGrant`, `getDeploymentById`),
   `handleAuthorizeResource`, GitHub + Linear verifiers. (tests 1-4 first)
3. `index.ts` (KV) + `local.ts` (Map): implement the new adapter methods; wire
   `POST /resources/authorize` in both entry files (mirror #487's `/slack/send`).
4. Enforcement layer 1 in `handleRegisterDeployment` /
   `handleUpdateSubscriptions` + `skipped_unauthorized` response. (test 5)
5. Enforcement layer 2 in both `deliver()` implementations. (tests 6-7)
6. Slack grant write in `handleSlackWorkspaceRegister`. (test 8)
7. Python `authorize_resources()` in `events/server.py` + wire into `inbox.py`,
   `subagent.py`, `auth_bootstrap.py`. (tests 10-12)
8. `/review` gate; run `event-server` tests + `pytest tests/ --ignore=integration`.
9. Do **not** bump `VERSION` / `pyproject.toml` / `CHANGELOG.md` (modastack
   release policy). Open PR against `main`.

## 9. Open questions for the reviewer

1. **Enforcement always-on vs. mode-gated, and rollout.** This spec makes
   enforcement **always-on** and relies on client auto-authorize for back-compat
   (no bypass flag — a bypass env is a security footgun). The issue's "Release
   context" allows deferral when loopback-only; an always-on design satisfies
   that case too (the loopback bubble authorizes itself). The one rollout risk
   (codex #3): an **old client** pointed at an upgraded server, before it ships
   `authorize_resources()`, silently loses `github:`/`linear:`/`slack:`
   delivery. Mitigant: the client + event-server ship from the **same release**
   (`release.yml` builds both), and F&F deployments are controlled, so the
   upgraded server only ever sees grant-aware clients. **Recommended: always-on,
   coupled release, no kill-switch.** Confirm — or do you want a
   `REQUIRE_RESOURCE_GRANTS` flag (default on) purely as an incident break-glass?
2. **Skip vs. hard-reject** unauthorized topics at registration. Spec picks
   **skip + report** (§3.4); delivery is the fail-closed boundary regardless.
   Codex argues hard-reject gives louder operational feedback. Confirm skip, or
   switch to `400` listing the unauthorized topics.
3. **Linear:** (a) confirm `teams(filter:{key:{eq}})` is the right accessibility
   signal (vs. `viewer`/`organization`), or name a preferred query; (b) is the
   `linear:TEAM` workspace-ambiguity (§4) acceptable for F&F, or should this
   issue also move the topic to an org-scoped form (`linear:{orgId}:{team}`)?
   That would widen scope into the adapter + topic contract.
4. **GitHub entitlement bar (§3.2).** Spec tightens past the issue's bare
   "require success" to `private == true || permissions.push`. Confirm that bar,
   or specify the intended one (e.g. require `admin`, or accept any `2xx`
   including public repos).

---

## Appendix: original issue #488 (verbatim)

<!-- preserved exactly as filed; the spec above is a superset -->

## Problem

Webhook resource topics such as `github:owner/repo`, `linear:TEAM`, and `slack:T123` are currently global. On a shared/public event server, a bubble can subscribe to a resource topic by guessing the topic name and receive webhook events for that resource. Resource names are not secrets, so this is a cross-bubble read risk.

We should gate subscriptions and delivery with server-verified resource grants. The key idea: an agent already needs a GitHub token, Linear API key, or Slack bot/app credential to read from that upstream service. The event server can use that credential once to verify access, then store a bubble-scoped grant.

## Goal

A bubble can receive inbound webhook events for a resource only if the event server has verified that the bubble has upstream access to that resource.

Invariant:

```text
valid bubble + verified upstream read/access capability for resource = can subscribe/receive webhook events
```

## Proposed design

Add a bubble-signed resource authorization flow, for example:

```text
POST /resources/authorize
```

Example GitHub body:

```json
{
  "service": "github",
  "resource": "owner/repo",
  "credential": "<github token>"
}
```

Example Linear body:

```json
{
  "service": "linear",
  "resource": "TEAMKEY",
  "credential": "<linear api key>"
}
```

The event server should:

1. Require a valid bubble HMAC signature on the authorization request.
2. Verify the credential against the upstream service.
   - GitHub MVP: call `GET /repos/{owner}/{repo}` with the supplied token and require success.
   - Linear MVP: call Linear GraphQL with the supplied API key and verify the requested team/key is accessible.
   - Slack can initially use the bubble-scoped workspace registration/signing-secret flow from #487, then converge on the same grant model.
3. Store only a resource grant, not the upstream credential.
4. Never log the credential or raw request body.

Possible storage shape:

```text
resource_grant:{service}:{resource}:{bubble_id} = true
resource_grants_for_bubble:{bubble_id} = [...]
```

Leave room for future account identity:

```text
resource_grant {
  id
  account_id nullable for now
  bubble_id
  service
  resource
  granted_by = "upstream_token_verification"
  created_at
  expires_at nullable
}
```

## Enforcement requirements

- Subscription registration/update must reject or skip global resource topics when the authenticated bubble does not have a matching grant.
- Webhook delivery must also filter by current resource grants, so stale subscription index entries cannot bypass authorization.
- Multiple bubbles may intentionally receive the same resource if each has its own verified grant.
- The fix must not assume `bubble_id` is the long-term user identity. Bubbles are runtime trust domains; a later account system will sit above them and attach bubbles to users/orgs.

## Future account-system constraint

We expect to add account auth later. When a bubble is minted or joined, the client may also send a user/org-scoped API key so the event server can attach that bubble to an account. This issue should avoid baking in a model where “bubble equals user.”

Desired later layering:

```text
account_id -> owns many bubbles
bubble_id -> runtime signing key
bubble_id/account_id -> authorized resources
```

This should make it possible to revoke a bubble without deleting the account, rotate user API keys without rotating bubble keys, and eventually support account-level resource grants inherited by bubbles.

## Acceptance criteria

- A bubble cannot receive GitHub/Linear/Slack webhook events solely by guessing/subscribing to a resource topic.
- A bubble with a verified GitHub token for `owner/repo` can subscribe to and receive `github:owner/repo` events.
- A bubble without verified access to `owner/repo` cannot subscribe to or receive `github:owner/repo` events, even if the subscription index contains a stale entry.
- A bubble with a verified Linear API key for `TEAMKEY` can subscribe to and receive `linear:TEAMKEY` events.
- Multiple bubbles can independently verify and receive the same resource.
- Upstream credentials are not persisted and are not logged.
- Existing local/single-bubble flows keep working.

## Release context

This is a blocker for shared/public centralized event-server mode. F&F can defer it only if deployments are constrained to loopback/private event-server access or isolated per-tenant event servers.

Tracked from `docs/FRIENDS-FAMILY-SECURITY-TODO.md`.
