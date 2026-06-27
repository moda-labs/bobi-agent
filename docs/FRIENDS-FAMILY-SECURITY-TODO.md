# Friends and Family Security To-Do

Status: active backlog for the limited friends-and-family release.

This document tracks security work found in the June 2026 release review. Treat
each item as a discrete ticket. The key release decision is whether the system is
run only on loopback/private-network surfaces, or whether a shared/public event
server is used.

## Release Gate

- [ ] Decide and document the supported F&F deployment shape.
  - Safe enough for F&F: local loopback event server, or Fly instances with no
    public ingress and access through `fly proxy`.
  - Not safe for F&F yet: multiple external users on the shared/public
    Cloudflare Worker event server.

- [ ] If using Fly for F&F, make the private-ingress assumption explicit in the
  runbook.
  - Confirm generated Fly config has no public `http_service`.
  - Confirm the agent UI is reached through `fly proxy`, not public routing.
  - Confirm the event server URL is not a shared multi-tenant Worker unless the
    blockers below are fixed.

## Must Fix Before Shared/Public Event Server

- [ ] Authenticate `/slack/send`.
  - Current risk: anyone who can reach the event server can ask it to post Slack
    messages using a registered workspace bot token.
  - Code: `event-server/src/index.ts`, `event-server/src/local.ts`,
    `event-server/src/core.ts::handleSlackSend`.
  - Expected fix: require bubble-scoped HMAC auth, and authorize the requested
    workspace/app against that bubble.

- [ ] Authenticate `/slack/workspaces`.
  - Current risk: anyone who can reach the event server can register or overwrite
    workspace bot-token records.
  - Code: `event-server/src/index.ts`, `event-server/src/local.ts`,
    `event-server/src/core.ts::handleSlackWorkspaceRegister`.
  - Expected fix: require bubble-scoped HMAC auth and store workspace
    registrations per bubble, not globally.

- [ ] Close cross-bubble webhook topic reads.
  - Current risk: `github:`, `linear:`, and `slack:` resource topics are global,
    so a second bubble can subscribe to the same resource topic and receive
    webhook payloads.
  - Code: `event-server/src/core.ts::GLOBAL_TOPIC_PREFIXES`,
    `namespaceSubKey`, `subscriptionKeysForEvent`.
  - Expected fix: add inbound webhook subscription authorization, or namespace
    resource topics by tenant/bubble after proving each integration can still
    route correctly.

- [x] Add Worker-to-Durable-Object internal authentication.
  - Fixed by #489: the Worker adds `x-bobi-internal` on all
    Worker-to-DO calls, and the Durable Object rejects `/init`, `/event`, and
    websocket upgrade requests without a valid internal shared-secret check.
  - Code: `event-server/src/deployment-session.ts`,
    `event-server/src/index.ts::createKVStorage`.
  - Secret handling: `INTERNAL_DO_SECRET` is a Cloudflare Worker secret only
    (`wrangler secret put INTERNAL_DO_SECRET`); do not propagate it to Fly,
    Bubble, agent-team env, or `run/.env`.

- [ ] Add public admission control for `POST /deployments`.
  - Current risk: unsigned deployment minting is fine for bootstrap, but public
    exposure allows unbounded bubble/deployment creation and billable resource
    abuse.
  - Code: `event-server/src/core.ts::handleRegisterDeployment`.
  - Expected fix: per-IP/token-bucket rate limiting, optional invite/admin token,
    or another explicit admission mechanism for public deployments.

## Must Fix Before Packaging/Support Broadly

- [ ] Write `.env` files with owner-only permissions.
  - Current risk: `write_env_file()` writes secret material with default process
    umask; a local `.env` was observed as `0644`.
  - Code: `bobi/config.py::write_env_file`.
  - Expected fix: mirror `save_bubble_state()` and create/truncate env files with
    mode `0600`; add a regression test.

- [ ] Add a doctor/preflight warning for permissive secret-file modes.
  - Current risk: users may already have `.env` or `run/.env` readable by
    group/world.
  - Expected fix: `bobi agent <name> doctor` warns, and ideally offers the exact
    `chmod 600 ...` remediation.

## Medium Priority Hardening

- [ ] Harden whole-repo registry fallback extraction.
  - Current risk: `_fetch_repo_tarball()` still uses `tar.extractall()` on
    GitHub tarball members without the safe member filter used by the direct
    archive install path.
  - Code: `bobi/registry.py::_fetch_repo_tarball`,
    `_safe_members`, `_install_team_tar`.
  - Expected fix: reuse `_safe_members` and the `filter="data"` extraction path
    for repo fallback installs.

- [ ] Add replay deduplication for bubble signatures.
  - Current risk: signed requests include a nonce, but the server does not yet
    reject a duplicate nonce within the timestamp window.
  - Code: `event-server/src/core.ts::verifyBubbleSignature`,
    `authenticateBubble`.
  - Expected fix: maintain a short-TTL seen set keyed by `(bubble_id, nonce)`.

- [ ] Replace remaining bearer API-key paths with bubble signatures.
  - Current risk: websocket subscribe and subscription update still use a
    long-lived deployment API key over the wire.
  - Code: event-server subscribe and `PUT /deployments/<id>/subscriptions`
    routes in `event-server/src/index.ts` and `event-server/src/local.ts`.
  - Expected fix: sign these routes with the bubble key and retire api-key auth
    where feasible.

- [ ] Make the trusted-team-code boundary explicit.
  - Current risk: installed team packs can run shell commands through
    `requires.check`, monitor `command:`, and build hooks.
  - Code: `bobi/config.py::run_requires_checks`,
    `bobi/monitors/scheduler.py::_command_conditions`,
    `bobi/build_render.py`.
  - Expected fix: document that team packs are trusted code, and consider
    warning when installing teams from non-default registries or arbitrary URLs.

## Verification Work

- [ ] Run event-server tests under a supported Node version.
  - Local review could not run them because `/usr/local/bin/node` was v14.17.0
    and failed on modern Vitest syntax.

- [ ] Run `npm audit` under a modern npm that supports the v3 lockfile.
  - Local review could not complete audit because npm v6 could not read the
    current lockfile correctly.

- [ ] Run Python tests in an environment with project dependencies installed.
  - Local review failed collection because `websocket-client` was missing from
    the active Python environment.

- [ ] Run Python dependency/security tooling.
  - Suggested tools: `pip-audit` and `bandit`.
  - Local review environment did not have `pip-audit`, `safety`, or `bandit`
    installed.

## Notes

- `docs/SECURITY-FINDINGS.md` contains older comms/auth backlog context. Keep
  this document focused on the current F&F release gate and immediate to-do
  list.
- Source scanning during the review did not find obvious committed
  production-looking secret literals outside tests/docs/examples. Do not rely on
  that as a substitute for secret scanning in CI.
