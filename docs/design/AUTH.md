# Auth — GitHub OAuth for the Cloud Event Server

Status: **thought collection — not started.** This doc exists to accumulate
design thinking until the work begins. Tracked by **#142**; the prior
implementation attempt (PR #143) was closed as stale with a
[salvage map](https://github.com/moda-labs/modastack/pull/143) — read it
before writing any code.

Sequencing: **after #177** (event contract v2 worker adapter refactor), so
auth is written once against the stable adapter structure instead of being
refactored through the v2 cutover.

---

## Why

The cloud event server (Cloudflare Worker) accepts **anonymous deployment
registrations** — anyone who knows the URL can register and subscribe to any
repo's events. That's fine for a single-operator deployment; it's the main
blocker to **hosted onboarding**: a new user should be able to point their
install at our event server and go, without standing up their own. Auth is
the gate between "we run the event server for ourselves" and "users don't
need to set one up."

Goals (from #142):
1. **User identity** — GitHub OAuth; we know who's registering.
2. **Access control** — subscriptions only to repos the user can access.
3. **Clean lifecycle** — `start` subscribes (auto-login on first run),
   `stop` unsubscribes.

Explicit non-goals for the first cut: no org model, no encryption-at-rest
story beyond what the platform gives, no fine-grained roles.

## Settled decisions (from #142 — don't re-litigate)

- **GitHub OAuth App**, not GitHub App user-auth: tokens don't expire, no
  refresh machinery for the first version.
- **Localhost redirect flow**: CLI opens a browser, listens on an ephemeral
  port for the callback. Headless fallback: print the URL (device flow is a
  future upgrade — see open questions).
- **Server-side code exchange**: the CLI sends the auth code to the event
  server, which holds the client secret. The secret never ships in the CLI.
- **Session tokens** (`moda_sess_<uuid>`) issued by the event server; the
  CLI stores them, not the GitHub token… (the GitHub token lives server-side
  on the user record for ACL checks).

## System sketch

```
┌──────────────┐   1. modastack login          ┌─────────────────────────┐
│   CLI        │ ────────────────────────────▶ │  Event server (worker)  │
│              │   GET /auth/config            │                         │
│  auth.py     │ ◀──────────────────────────── │  GITHUB_CLIENT_ID/      │
│              │      { client_id }            │  GITHUB_CLIENT_SECRET   │
│  browser ────┼──▶ github.com/login/oauth ──┐ │  (wrangler secrets)     │
│  localhost:N │ ◀── redirect ?code=… ◀──────┘ │                         │
│              │   POST /auth/github/callback  │  exchanges code,        │
│              │ ────────────────────────────▶ │  fetches /user,         │
│              │ ◀──── { session_token, user } │  stores user record     │
│  auth.yaml   │                               │                         │
└──────────────┘                               └─────────────────────────┘

┌──────────────┐   2. modastack start          ┌─────────────────────────┐
│   CLI        │   POST /deployments           │  authenticateUser()     │
│  (Bearer     │ ────────────────────────────▶ │  per github:* key:      │
│   session    │                               │   GET api.github.com/   │
│   token)     │ ◀──── 201 / 403 per-key ───── │   repos/{owner}/{repo}  │
└──────────────┘                               │   with user's token     │
                                               └─────────────────────────┘

┌──────────────┐   3. modastack stop           ┌─────────────────────────┐
│   CLI        │   DELETE /deployments/{id}    │  verify user_id owns    │
│              │ ────────────────────────────▶ │  deployment; clean      │
│              │   (best-effort)               │  subscription index     │
└──────────────┘                               └─────────────────────────┘
```

### Pieces

| Piece | Where | Notes |
|---|---|---|
| `AuthState` + OAuth flow + token persistence | `modastack/auth.py` (new) | Port nearly as-is from the PR #143 branch |
| `modastack login` / `logout` | `cli.py` | Login fetches client_id from `/auth/config`; logout clears `auth.yaml` |
| Auto-login on `start`, unregister on `stop` | `events/client.py` / CLI start path | `ensure_authenticated()` before registration; DELETE is best-effort |
| OAuth endpoints + session/user records | event-server, as **`core.ts` handlers over `StorageAdapter`** | The PR wrote raw-KV code in `index.ts` — rewrite post-#169/#177 |
| Registration ACL | event-server registration handler | Validates `github:org/repo` subscription keys against the user's repo access |

### Storage

- **Client:** `~/.modastack/auth.yaml` — user identity, deliberately *not*
  per-project (this is "who am I", not "what does this project use").
  Note: this is the one exception to "no global ~/.modastack/" — identity
  is per-human, not per-project. Worth a conscious sign-off.
- **Server:** `users:{github_user_id}` and `session:{token}` records, via
  `StorageAdapter` (the PR's KV-key shapes are fine; the access goes through
  the adapter so the local server could share the handlers).

## Interaction with event contract v2

- v2 keeps subscription key shapes unchanged (`github:org/repo`), so the
  ACL design survives intact.
- The ACL is inherently service-specific (it parses `github:` keys). Under
  v2's "service names only in adapters" rule, the natural long-term home is
  an optional per-adapter hook — e.g. `authorizeSubscription(key, user)` on
  the adapter interface, with github implementing a repo-access check and
  other services defaulting to allow. **Don't build the generic hook in the
  first cut** — one hardcoded github check in the registration handler is
  fine until a second service needs ACL — but don't build anything that
  blocks it either.

## Open questions (collect thoughts here)

- **Existing anonymous deployments** — when auth turns on, what happens to
  the prod director's registration? Needs a migration step in the rollout
  (re-register authenticated), or a grace mode where the worker accepts
  pre-existing deployment api_keys but no *new* anonymous registrations.
- **Local event server** — does `local.ts` get auth too? Lean: no — local
  is single-operator by construction; auth handlers live in core but the
  local server doesn't mount them. Decide explicitly.
- **Headless / EC2 logins** — the prod director can't open a browser.
  Device flow, or operator logs in locally and copies `auth.yaml` to the
  box? The copy-the-file answer may be fine for v1; say so out loud.
- **Slack/Linear subscription keys** — first cut only ACLs `github:*` keys;
  `slack:`/`linear:` keys pass unchecked for an authenticated user. Is
  "authenticated = trusted for non-github keys" acceptable until the
  per-adapter hook exists?
- **Token revocation / expiry** — OAuth App tokens don't expire, sessions
  shouldn't live forever. Cheap answer: sessions expire after N days,
  `ensure_authenticated` re-runs login. Pick N.
- **GitHub token scope** — `repo` scope is broad (it grants write). Is
  read-only repo visibility achievable (`read:org`? fine-grained PATs via
  GitHub App?) without giving the event server a write-capable token per
  user? This is the strongest argument for revisiting GitHub App auth
  later.
- **Rate limits** — per-repo access checks hit api.github.com on every
  registration; fine at current scale, cache `user→repo` results with a
  short TTL if registration ever gets chatty.
- **Multi-user shared repos** — two users registering deployments against
  the same repo is the whole point ("enterprise use"); make sure the
  subscription index keeps deployments distinct per user (it already keys
  by deployment id — verify nothing assumes one deployment per repo).

## Salvage from PR #143 (closed, branch preserved)

Full map on the [PR close comment](https://github.com/moda-labs/modastack/pull/143).
Short version — carry over: `auth.py`, `tests/test_auth.py`, the behavioral
assertions in `tests/test_event_server.py` / `event-server/test/index.spec.ts`,
and the CLI command shapes. Rewrite: worker endpoints (as core.ts handlers)
and the Python consumer wiring (the PR targets the deleted `manager/`
package).
