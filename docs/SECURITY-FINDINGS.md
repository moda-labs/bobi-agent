# Security findings backlog — comms / auth-v1 (#240 and related)

**Status: parked for post-friends-and-family review.** The F&F release runs the
**local Node event server** (loopback), where the isolation mechanism is sound and
most items below are not reachable. This doc collects every security-relevant
finding from the #240 build + reviews (adversarial plan review, 5 specialists,
Codex cross-model) so they can be triaged later as the system evolves. Nothing
here blocks the local-server F&F release.

Sources: `REVIEW-240.md` (Cn/Hn/Mn), the `/review` pass, Codex adversarial run.
Severity is "if/when the relevant mode is exposed", not current F&F exposure.

---

## Hard gates — fix BEFORE enabling Cloudflare mode

### S1. Durable Object accepts internal RPC with no auth  (CF only)
- **Severity:** High (when CF mode on) · **Reachable now:** No (local has no DOs)
- **Where:** `event-server/src/deployment-session.ts` (`/init`, `/event`, WS upgrade);
  Worker forwards via `index.ts` `stub.fetch(request)` / `deliver()`.
- **Risk:** DO trusts that the Worker is the only caller. A DO's id is
  `idFromName(deploymentId)` — deterministic from the (non-secret) deployment_id.
  If the DO is ever reachable off the Worker's verified path (second binding,
  route change), anyone knowing a deployment_id can `POST /event` straight into a
  victim's WebSocket → arbitrary event injection into their session.
- **Fix:** Worker→DO shared secret header verified by the DO on `/init`+`/event`
  (binding secret, never sent to clients); and/or Worker builds a fresh internal
  request instead of forwarding the client request verbatim; assert path
  deployment_id ∈ authenticated bubble. Needs human + adversarial review (epic).

### S2. Cloudflare KV `addSubscription` race  (CF only, pre-existing)
- **Severity:** Med (reliability+isolation) · **Reachable now:** No (local uses an atomic Map)
- **Where:** `event-server/src/index.ts` `addSubscription` (KV read-modify-write, no CAS).
- **Risk:** Concurrent joins/subscription updates on the same topic → last writer
  drops the other deployment id → silent missed deliveries. Predates #240 (we only
  changed the key to be bubble-namespaced).
- **Fix:** CAS/optimistic-concurrency on the KV write, or move the subscription
  index into a Durable Object for serialization. Bundle with S1 (same CF pass).

---

## Deferred security hardening (accepted for v1)

### S3. Inbound webhooks fan out cross-bubble  (accepted → #239)
- **Severity:** Med (cross-tenant read) · **Reachable now:** only multi-trust-domain
- **Where:** `core.ts` `isGlobalTopic` / `subscriptionKeysForEvent`; github/slack/linear
  resource topics intentionally global.
- **Risk:** Any bubble can subscribe to `github:victim/repo` (etc.) and read another
  tenant's webhook payloads (PR diffs, issue bodies, Slack content). Explicitly
  accepted for v1 to keep Slack/GitHub working. Locked by
  `test_webhook_fans_out_across_bubbles` so closing it is a conscious change.
- **Fix:** Inbound webhook subscription authorization → **#239**.

### S4. Unauthenticated mint = resource exhaustion / open admission
- **Severity:** Med (DoS) · **Reachable now:** No on loopback; yes if server is internet-reachable
- **Where:** `core.ts` `handleRegisterDeployment` mint branch; `index.ts` `/deployments`.
- **Risk:** Mint requires no credential (by design — you mint your OWN isolated
  bubble, you can't reach others). But nothing rate-limits it → unbounded bubbles +
  per-mint DO creation (CF, billable) / Map growth (local OOM).
- **Fix:** Per-IP rate limit / token bucket on `/deployments`; cap bubbles per source.
  Note: this is DoS, NOT an isolation break — a stranger's bubble can't reach yours.

### S5. No replay dedup within the signing window
- **Severity:** Med · **Reachable now:** needs MITM / log access / co-resident process
- **Where:** `core.ts` `verifyBubbleSignature` (±300s window, no nonce seen-set).
- **Risk:** A captured signed publish can be replayed for up to 5 min. The `nonce`
  is already in the signed wire format (forward-compatible), but the server does not
  yet reject duplicates.
- **Fix:** Short-TTL seen-set of `(bubble_id, nonce)` over the 300s window; reject dups.

### S6. `/slack/send` and `/slack/workspaces` are unauthenticated
- **Severity:** High (if server reachable) · **Reachable now:** No on loopback
- **Where:** `core.ts` `handleSlackSend` / `handleSlackWorkspaceRegister`; both
  backends route them unsigned. Pre-existing; left working so Slack keeps functioning.
- **Risk:** Anyone who can reach the server can post arbitrary Slack messages to any
  registered workspace, or overwrite a workspace's stored bot token.
- **Fix:** Bubble-scope + sign these routes; scope the workspace token store per bubble.

### S7. api_key remains a transmitted bearer credential
- **Severity:** Low–Med · **Reachable now:** loopback only
- **Where:** WS-subscribe (`local.ts handleUpgrade` / `index.ts`) and
  `PUT /deployments/<id>/subscriptions` still use `Authorization: Bearer <api_key>`.
- **Risk:** Read isolation already holds via bubble-namespaced subscriptions, but the
  api_key is a long-lived secret sent on every WS connect / PUT (contradicts
  "secret rides the wire once"). The `?token=` URL-in-query leak WAS removed.
- **Fix:** Move WS-subscribe + PUT to bubble signatures; retire api_key once done.

### S8. Cleartext transport for payloads over remote http
- **Severity:** Med (only if deployed remote + http) · **Reachable now:** No (loopback)
- **Where:** transport. Mint over non-loopback `http://` is now HARD-REFUSED
  (`server.py:_is_loopback_or_tls`), but post-mint event payloads still travel
  cleartext if someone points at a remote `http://` server.
- **Fix:** Require TLS for any non-loopback URL (not just mint); add the advisory
  `doctor` / `start` preflight warning (currently only the hard mint-refuse exists).

---

## Auth-correctness nuances (latent, don't affect supported configs)

### S9. Signed path assumes no URL prefix + URL-safe topics
- **Severity:** Low (correctness → spurious 403) · **Reachable now:** No
- **Where:** `signing.py` signs literal `/events/{topic}` / `/deployments`; server
  verifies `url.pathname + url.search`.
- **Risk:** A base URL mounted under a path prefix, or a topic/session needing
  percent-encoding (space, `#`, `%`, non-ASCII), would 403 with the right key. Current
  topics are sanitized identifiers / hex, so unaffected.
- **Fix:** Sign `urlsplit(url).path` (handles prefixes); pin decoded-vs-encoded path
  convention with a cross-language vector; or document the constraint.

### S10. Constant-time compare operates on prefixed hex strings
- **Severity:** Low · **Reachable now:** marginal timing oracle
- **Where:** `core.ts` `verifyGitHubSignature` / `verifySlackSignature` compare
  `"sha256="+hex` / `"v0="+hex` via `constantTimeEqual` (length-check + XOR).
- **Risk:** Comparing variable-length prefixed strings can leak length; for these the
  expected length is fixed so it's marginal, but best practice is comparing
  fixed-length decoded digest bytes.
- **Fix:** Compare raw decoded digests of equal length, not prefixed hex strings.

---

## Reliability-adjacent (not strictly security, noted for completeness)

### S11. Live subscribers don't self-heal after a server restart
- **Where:** `client.py` reconnects with the dead deployment_id/api_key; new
  registrations re-mint correctly (`BubbleRejected` → `force_remint_of`), but an
  already-connected WS doesn't re-register. Publish-drop on 403 is now surfaced
  (returns False + logs), not silent. Largely pre-existing. Relates to #278/#279.

---

## Resolved during the #240 build/review (for context — already fixed)
- Timing-attackable `===` in Slack/GitHub webhook verifiers → constant-time. (was H2)
- `?token=` WS query-param credential leak → removed (header-only).
- Publish silently returned `True` on a 403 → now logs + returns False.
- Partial/incomplete signing headers on `/deployments` fell through to MINT (silent
  session fork) → now rejected (403).
- Re-mint lock wait (10s) < mint HTTP timeout (15s) → bubbles could fork on slow
  mint → wait budget raised to 30s.
- Mint over non-loopback cleartext http → hard-refused.
- bubble.json written mode 0600; bubble key returned once at mint, never on join,
  never logged. Cross-language HMAC parity locked by a golden test vector.

---

## Triage hints for later
- Everything under **Hard gates** + **S6** is the real list before any
  internet-reachable / multi-tenant deployment.
- **S1/S2** = one Cloudflare hardening pass (pairs with #279 / DO work).
- **S3** = #239. **S4/S5/S7/S8/S10** = incremental hardening, independently shippable.
- For the F&F local-server release: none are reachable; ship as-is.
