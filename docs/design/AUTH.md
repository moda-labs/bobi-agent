# Auth & Tenancy — Accounts, Connections, and the Hosted Event Server

Status: **two layers.**

- **v1 — Bubble-scoped isolation + HMAC signing (anonymous).** Specced and
  ready to build: epic **#238**, core **#240**, loopback bind **#241**,
  observability **#242**. Draws the transport trust boundary with **no
  identity**, and ships first. Described in the next section.
- **v2 — Accounts & tenancy (the rest of this doc).** Identity, service
  connections, and the per-resource ACL that **binds onto** v1 bubbles.
  Still **thought collection**; tracked by **#239** (the v2 epic). The
  original OAuth-login spec (**#142**) and its implementation attempt
  (**PR #143**) are both closed as superseded — #142's GitHub-OAuth-App
  approach was reversed by the one-GitHub-App decision below, and its package
  paths (`manager/`) are deleted. Read the
  [#143 salvage map](https://github.com/moda-labs/bobi-agent/pull/143) before
  writing v2 code; the login-flow mechanics carry over (see Salvage, bottom).

Sequencing: v1 ships now (no dependencies). v2 lands **after #177** (event
contract v2 worker adapter refactor), so it's written once against the
stable adapter structure.

Scope note: this started as "GitHub OAuth login" and grew. The real subject
is **tenancy**: who may subscribe to which events, and how the boundary is
enforced. v1 draws that boundary anonymously (bubbles); v2 attaches identity
to it. Login is one piece of v2.

---

## Why

The cloud event server (Cloudflare Worker) accepts **anonymous deployment
registrations** — anyone who knows the URL can register and subscribe to any
repo's events. That's fine for a single-operator deployment; it's the main
blocker to **hosted onboarding**: a new user should be able to point their
install at our event server and go, without standing up their own.

Goals (from #142, restated for the multi-tenant frame):
1. **Identity** — we know which account is registering.
2. **Tenancy** — a deployment receives only events its account is
   authorized for, per service.
3. **Clean lifecycle** — `start` subscribes (auto-login on first run),
   `stop` unsubscribes; accounts can list and prune their deployments.

Non-goals for the first cut: no org model (no sharing connections between
accounts), no fine-grained roles, no encryption-at-rest story beyond the
platform's.

## v1 — Bubble-scoped isolation + HMAC signing (anonymous, ships first)

Status: **specced, ready to build** — epic #238, core #240, loopback bind
#241, observability #242. This layer needs none of the accounts machinery
below; v2 binds identity onto it later.

The accounts model in the rest of this doc is the destination, but it's
blocked on the GitHub-App login flow and the v2 adapter cutover — and
meanwhile `POST /events/{topic}` drives autonomous agents while
unauthenticated. v1 closes that now, without identity.

**Bubbles.** A bubble is a trust domain on the event server. Every bobi
instance owns one bubble, shared by all its agents. Events are scoped to a
bubble — a publish reaches only same-bubble subscribers; cross-bubble read
and write are impossible. Registration is **open and self-namespacing**:
registering creates or joins a bubble and grants access to *that bubble
only*. A stranger who registers lands in their own empty bubble — harmless.
So there is **no registration gate, no shared registration token, no
`wrangler secret put`**; the boundary is "what a registration grants" (its
own bubble), not "who may register." Registration becomes **mint-or-join**:
the first `bobi agent <name> start` mints the bubble (server returns `bubble_id` +
key); each agent session then joins by signing its registration with the
stored key.

**A bubble is a proto-account.** Today it's anonymous, self-minted on first
`start`. The accounts model below is the graduation (#239): registration
gains an account identity and the anonymous bubble binds to it — same flow,
identity added as one parameter. Everything below (connections,
`authorizeSubscription`) then layers on top of bubbles.

**Credential — HMAC, not bearer.** Each bubble has a secret key, established
once at registration (over TLS) and **never transmitted again**. Every
publish and every WS subscribe is **HMAC-signed** with the bubble key (the
construction the worker already uses for webhook signatures), with a signed
timestamp for replay protection. The key rides the wire once, not per
request. → This **supersedes** the old "Generic topics" v1 fix (deployment
`api_key` as a bearer token) and invariant #3's "api_key remains the WS
credential" below. HMAC is symmetric — the server holds the key to verify;
if a hosted multi-tenant server ever becomes an untrusted party, the
graduation is **asymmetric signing (Ed25519)**, and the wire format reserves
an algorithm field for it.

**Webhooks stay global in v1.** Inbound GitHub/Slack/Linear events
authenticate by source signature and fan out to whatever subscribed to the
resource topic, **regardless of bubble** — `github:org/repo` is a global
key. Authorizing a resource subscription needs an account that owns the
resource, so inbound subscription ACL is v2 (the `authorizeSubscription`
work below). Single-operator it's a non-issue; it's the boundary v2 closes
before multi-tenant.

**Local server.** A co-located local server is safe by **loopback bind**
(#241) — `127.0.0.1` unless explicitly widened — so it needs no ACL. The
bubble/HMAC path secures the shared/remote (worker) surface. This refines
the "local gets no auth" decision below: local stays zero-config, but the
reason is loopback.

---

## v2 — Accounts & tenancy

Everything from here down is the v2 layer: it attaches identity to the v1
bubbles above. Where it says "deployment holds an `api_key`" or "api_key is
the WS credential," read that as v1's bubble key + HMAC; v2's addition is
the *account binding* and the *per-resource ACL*, not a new transport
credential.

## Settled decisions

- **One GitHub App for everything — login, ACL, and webhook ingestion.**
  This supersedes the #142-era decision ("OAuth App, not GitHub App") —
  superseded on new input, not re-litigated:
  - The bobi GitHub App **already exists and is public**, giving
    one-click webhook onboarding: install it on a repo and that repo's
    events flow to the cloud event server, signed with the app's webhook
    secret (which only the worker holds).
  - The OAuth App's sole advantage was non-expiring tokens. GitHub Apps
    support the identical authorization-code login flow, and
    **user-to-server token expiration is an opt-out app setting** — toggle
    it off and there's no refresh machinery either. (Contingency: GitHub
    has signaled it may eventually force expiration; if so, refresh
    machinery lives entirely server-side and the CLI never notices.)
  - App permissions can be **read-only metadata** — no write-capable
    `repo`-scope token per user sitting on the server. This was the old
    doc's "strongest argument for revisiting GitHub App auth later."
  - A user-to-server token sees exactly **(repos the user can access) ∩
    (repos with the app installed)** — which is precisely the set of repos
    whose events both exist on this server and this user may read. The ACL
    check stays a one-liner.
- **Localhost redirect flow**: CLI opens a browser, listens on an ephemeral
  port for the callback. Headless fallback: print the URL (device flow is
  a future upgrade).
- **Server-side code exchange**: the CLI sends the auth code to the event
  server, which holds the client secret. Secrets never ship in the CLI.
  Same pattern for every service connection (Slack, Linear), not just login.
- **Session tokens** (`moda_sess_<uuid>`) issued by the event server; the
  CLI stores them, never the GitHub token (that lives server-side on the
  account record).
- **Sessions are many-per-account and don't expire in v1.** Login on a
  second machine appends a session, never replaces one (`session:{token}`
  is its own keyspace — do NOT store the token as a field on the account
  record). Logout deletes only its own session. Sessions are revocable
  server-side (operator can delete `session:{token}` records). Expiry
  ships together with device flow, not before — expiring sessions without
  a headless re-login path would kill EC2 deployments every N days.
- **GitHub identity is load-bearing, not cosmetic.** The ACL checks repo
  access with the user's own GitHub token. Google/email login would
  provide identity but nothing to check repo access with; supporting it
  later means "identity provider + connected GitHub account" as a
  two-step. Not v1.
- **Hard cutover, no grace mode.** Exactly one anonymous deployment exists
  (the prod director). Rollout: deploy worker with auth required → login
  on the EC2 box (copy `auth.yaml`, see open questions) → `bobi
  restart`. A grace mode is compatibility machinery for a fleet that
  doesn't exist.
- **Local event server is safe by loopback, not by an ACL.** v1 binds the
  local server to `127.0.0.1` unless explicitly widened (#241), so a
  co-located server needs no account ACL. The v2 account handlers (login,
  connections) live in `core.ts` and are worker-only — `local.ts` doesn't
  mount them. Local stays single-operator, zero-config.

## The tenancy model

```
account (github user)
  ├── sessions      1:N   one per machine login; CLI credential
  ├── connections   1:N   per-service installs; each grants a key namespace
  │     github   → app installation(s)   → github:{owner}/{repo} keys
  │     slack    → workspace install     → slack:{team_id} keys
  │     linear   → org connection        → linear:{org_id}/{team_key} keys
  └── deployments   1:N   one per project per machine; each holds an api_key
        subscriptions ⊆ union of namespaces granted by the account's connections
```

**Three invariants make the boundary; state them in code review terms:**

1. **Ingestion derives subscription keys server-side from
   signature-verified payloads.** The tenant id in a key (`team_id`, repo,
   org) comes from inside the signed webhook, never from anything a client
   supplies. (`subscriptionKeysForEvent` in `core.ts` already works this
   way — keep it true.)
2. **Registration checks every key against the account's connections.**
   Per-adapter `authorizeSubscription(key, account)` — see v2 interaction
   below.
3. **Delivery is exact-match between the two.** No wildcard or prefix
   subscriptions that can span tenants; the deployment asserts nothing at
   delivery time. Publish and WS subscribe are **HMAC-signed with the bubble
   key** (v1 — supersedes the old per-deployment-`api_key`-as-bearer);
   session tokens gate management endpoints only.

If those hold, a deployment receives only the subscriptions implied by its
keys, because keys can't be registered without authorization and events
can't be mislabeled without breaking a webhook signature.

## System sketch

```
┌──────────────┐   1. login flow          ┌─────────────────────────┐
│   CLI        │ ────────────────────────────▶ │  Event server (worker)  │
│              │   GET /auth/config            │                         │
│  auth.py     │ ◀──────────────────────────── │  GitHub App client id/  │
│              │      { client_id }            │  secret, app webhook    │
│  browser ────┼──▶ github.com/login/oauth ──┐ │  secret (wrangler       │
│  localhost:N │ ◀── redirect ?code=… ◀──────┘ │  secrets)               │
│              │   POST /auth/github/callback  │  exchanges code,        │
│              │ ────────────────────────────▶ │  fetches /user,         │
│              │ ◀──── { session_token, user } │  stores account record  │
│  auth.yaml   │                               │                         │
└──────────────┘                               └─────────────────────────┘

┌──────────────┐   2. bobi agent <name> start          ┌─────────────────────────┐
│   CLI        │   POST /deployments           │  per subscription key:  │
│  (Bearer     │   {keys, project, hostname}   │  authorizeSubscription( │
│   session    │ ────────────────────────────▶ │    key, account)        │
│   token)     │ ◀── 201 {deployment_id,       │  github: GET /repos/…   │
│              │         api_key} / 403 ────── │  with user-to-server    │
│              │                               │  token (403 hint:       │
│              │   WS w/ api_key (unchanged)   │  "install the app")     │
└──────────────┘                               └─────────────────────────┘

┌──────────────┐   3. bobi agent <name> stop / prune   ┌─────────────────────────┐
│   CLI        │   DELETE /deployments/{id}    │  verify account owns    │
│              │   GET  /deployments (mine)    │  deployment; clean      │
│              │ ────────────────────────────▶ │  subscription index     │
└──────────────┘                               └─────────────────────────┘
```

## Users ↔ deployments

One account, many deployments — across projects on one machine and across
machines — is the normal case, not an edge case.

- Session is per-machine (`$BOBI_HOME/auth.yaml`); deployment identity is
  per named agent (`deployment_id` + `api_key` persisted in `run/state/`
  by `save_deployment_state`). N machines × M agents works with no new
  machinery, *provided* the many-sessions-per-account invariant above holds.
- **Registration is insert-only** (`handleRegisterDeployment` mints a fresh
  UUID per call). The persisted state covers normal restarts, but
  `--fresh`, a deleted agent slot, or a rebuilt machine orphans the old
  record, which stays in the subscription index receiving fan-out. With
  owned deployments the broom is cheap and is part of v1:
  - `GET /deployments` — list the account's deployments.
  - `DELETE /deployments/{id}` — ownership-checked (already planned).
  - Registration payload carries human-meaningful metadata: project name,
    hostname, agent team. A user with five deployments needs more than
    five UUIDs.
  - Optional later: a stable client-side key (hash of account + machine +
    project) to make registration an upsert. Not v1.
- Two deployments (same or different accounts) subscribed to the same repo
  is the point, not a conflict — the subscription index maps key → list of
  deployment ids; fan-out is per-deployment. Verify nothing assumes one
  deployment per key.

## Service connections

### GitHub (login + events, one app)

Install the public bobi GitHub App on a repo → that repo's events
flow to the worker, signed with the app webhook secret. Subscribe-time ACL:
`GET /repos/{owner}/{repo}` with the user-to-server token; 200 = allowed.
A 403 response should hint "install the bobi GitHub App on this repo" —
the auth error doubles as the onboarding instruction.

**Public repos: deliberately allowed.** Any authenticated account may
subscribe to a public repo where the app is installed, even if someone else
installed it. It's public data. This is a decision, not an accident.

### Slack

A bobi Slack app with an "Add to Slack" OAuth flow, mirroring login:
server-side code exchange, install yields `team_id` + a per-workspace bot
token, stored as a connection bound to the installing account. ACL for a
`slack:{team_id}` key is one lookup: team_id ∈ account's connections.

The key shape is already workspace-scoped and server-derived
(`slack:{team_id}` from the verified payload) — no change needed.

**Bot token custody is an open fork** (see open questions): proxy outbound
sends through the server (token never leaves the server; `/slack/send`
already exists) vs. handing the token down into the project's
`run/.env` (today's model, agents call Slack directly).

### Linear

**Current key shape leaks across tenants.** `linear:{team_key}` uses the
Linear team key (`ENG`), which is only unique within an org — two tenants
with an `ENG` team would receive each other's events. The key must become
org-qualified (`linear:{org_id}/{team_key}`, org id from the verified
webhook payload), with a Linear OAuth connection for the ACL — same
pattern as Slack. Key-shape change coordinates with the v2 adapter work
(#177-#181); do it there, not here.

### Generic topics

**Solved by v1 bubbles** (see the v1 section; #240). `POST /events/{topic}`
was unauthenticated and global — an event-injection vector (events drive
autonomous agents) and a cross-tenant broadcast (two tenants on topic
`deploy.failed` hearing each other). v1 closes both: publishing is
HMAC-signed with the bubble key, and topics are namespaced to the posting
bubble on publish and subscribe, so generic events never cross bubbles. v2
adds nothing here — when bubbles bind to accounts, the namespacing becomes
identity-aware for free.

### Outbound endpoints

`POST /slack/send` is currently unauthenticated — anyone with the URL can
puppet the bot. It needs deployment auth plus a check that the target
workspace is among the account's connections. Audit any other
action-taking endpoint the same way: **the tenancy boundary applies
outbound, not just to event delivery.**

## Storage

- **Client:** `$BOBI_HOME/auth.yaml` — account identity, deliberately separate
  from any one named agent ("who am I", not "what does this agent use").
  `BOBI_HOME` defaults to `~/.bobi` and can be changed with the environment.
- **Server**, via `StorageAdapter` (handlers in `core.ts` so the local
  server *could* mount them, though it doesn't):
  - `account:{github_user_id}` — identity + user-to-server GitHub token
  - `session:{token}` → account id (many per account)
  - `connection:{account_id}:{service}:{tenant_id}` — slack workspaces,
    linear orgs; github connections are implied by app installations
  - deployment records gain `account_id` + metadata (project, hostname,
    team)

## Interaction with event contract v2

- v2 keeps subscription key shapes (`github:org/repo`); the ACL design
  survives. The linear org-qualification is the one key-shape change, and
  it belongs in the v2 adapter cutover (#177-#181).
- The old doc deferred the generic per-adapter ACL hook until "a second
  service needs ACL." **Slack is the second service** — so
  `authorizeSubscription(key, account)` on the adapter interface is now
  v1 scope: github checks repo access via user-to-server token, slack and
  linear check connections, unknown services default-deny.
- Key derivation from verified payloads already lives where v2 puts it
  (adapters/normalizers). Invariant #1 above is the review criterion.

## Open questions (collect thoughts here)

- **Headless / EC2 logins** — the prod director can't open a browser.
  v1 answer, said out loud: operator logs in locally and copies
  `auth.yaml` to the box. This works *because* sessions don't expire in
  v1. Device flow is the real fix and the prerequisite for ever turning
  expiry on.
- **Bot token custody (Slack)** — proxy sends through the server vs. copy
  token to `run/.env`. Lean: proxy — the send path exists, tokens
  never leave the server, and revocation is instant; cost is migrating
  agents off direct Slack API calls and the server becoming a send
  dependency. Decide before building the Slack connection flow.
- **Rate limits** — per-repo ACL checks hit api.github.com per
  registration; fine at current scale. Cache `account→repo` verdicts with
  a short TTL if registration gets chatty.
- **Workspace sharing / org model** — two accounts, one Slack workspace:
  the second account can't subscribe until connections can be shared.
  Explicit non-goal for v1; the connection record shape shouldn't preclude
  adding a member list later.
- **DMs** — Slack routing stays channel/workspace-scoped; DMs deferred to
  the future org-level router (existing decision, unchanged by auth).
- **GitHub forces token expiration someday** — contingency only: refresh
  machinery would live entirely server-side against the stored
  user-to-server token; CLI and session model unaffected. No action now.

## Rollout

This is the **v2** rollout. v1 bubbles need none of it — an instance
self-mints its bubble key at first `start`, registration is open, and there
is no secret to set, so v1 ships with no cutover and no manual step.

1. Deploy worker with auth required (hard cutover — new registrations
   need a session; existing deployment api_keys keep working for WS/event
   delivery, so the running director doesn't drop mid-deploy).
2. Run the login flow locally; copy `auth.yaml` to the EC2 box.
3. `bobi agent <name> restart` on the box — re-registers authenticated.
4. Delete the old anonymous deployment record (first use of the broom).

## Salvage from PR #143 (closed, branch preserved)

Full map on the [PR close comment](https://github.com/moda-labs/bobi-agent/pull/143).
Still accurate with one amendment: the OAuth flow in `auth.py` carries over
nearly as-is, but the `client_id` it fetches from `/auth/config` is now the
**GitHub App's**, and no OAuth App ever gets created. Carry over:
`auth.py`, `tests/test_auth.py`, the behavioral assertions in
`tests/test_event_server.py` / `event-server/test/index.spec.ts`, and the
CLI command shapes. Rewrite: worker endpoints (as `core.ts` handlers over
`StorageAdapter`) and the Python consumer wiring (the PR targets the
deleted `manager/` package).
