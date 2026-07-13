# Security Model

bobi runs agents that act autonomously on real-world events using your
credentials. Two facts shape the whole model:

- **The event bus is a front door to a model's prompt.** Every event the bus
  delivers becomes input an agent acts on, so neither publishing onto a topic nor
  subscribing to one can be open.
- **A team is code that runs with your credentials.** Installing one runs its
  prompts, checks, and hooks against your machine and tokens.

So the model controls four things: who can put events on the bus, what external
events you can receive, what code runs with your credentials, and where secrets
live. This doc is the overview; the event-bus mechanics are detailed in
[EVENT_SERVER.md](EVENT_SERVER.md), and the deployment/secrets specifics in
the private deploy repo's CONTAINERIZED_DEPLOYMENT.md (repo split).

## Trust boundary: local by default

An agent **reaches out**: it opens an outbound WebSocket to its event server and
receives events. Nothing reaches in. The local event server binds **loopback only**
(`127.0.0.1`), so nothing leaves your machine until you deliberately point the agent
at a remote event server or connect a messaging integration. The client refuses to
mint a trust bubble over a non-loopback cleartext URL - a remote event server must
be served over TLS.

## Credentials and secrets

- **Declared, not embedded.** A team's `agent.yaml` references credentials as
  `${VAR}`, never literals. `bobi agents install` scans for those refs, prompts for
  each, and writes `run/.env` - gitignored, created with owner-only permissions.
  `Config.load()` resolves them at runtime through one path.
- **Never committed.** `run/.env` and the bubble key live under `run/` and are
  gitignored. Treat them like any credential; never copy them off the host.
- **Deployed secrets** are stored as Fly secrets (the runtime store) and reconciled
  to the team's declared set on each deploy, so the store converges on exactly what
  `agent.yaml` declares (see the private deploy repo's
  CONTAINERIZED_DEPLOYMENT.md). An undeclared key is
  dropped, not provisioned.
- **Auth modes.** `api_key` mode uses `ANTHROPIC_API_KEY`; `subscription` mode uses
  Claude OAuth credentials on the volume and **requires `ANTHROPIC_API_KEY` to be
  absent** (it silently outranks subscription auth and bills the API). The image
  refuses to start with both set.

## Event-bus trust: bubbles and proof-of-access

Two independent layers, detailed in [EVENT_SERVER.md](EVENT_SERVER.md):

- **Who can publish or subscribe - trust bubbles (HMAC).** Each named agent mints a
  signed *trust bubble* on first start; every deployment joins it with the bubble
  key. Publishes and join-registrations are signed (HMAC-SHA256 over a canonical
  `timestamp / nonce / method / path / body`, verified within a ±300s window). Only
  bubble members can put events on the bus, and non-global topics are namespaced by
  bubble id, so one bubble cannot inject into or eavesdrop on another's custom
  topics. The bubble key transits exactly once, at mint, over TLS - **protect it**.
- **What external events you can receive - proof-of-access grants.** Before a bubble
  may receive a global webhook topic (`github:owner/repo`, `linear:team`,
  `slack:workspace`), the server verifies an upstream credential once (a GitHub repo
  read, a Linear team read, a Slack workspace registration) and stores only the
  resulting grant, never the credential. The grant is enforced at delivery
  (fail-closed), so a bubble can never receive another resource's events without
  proving it controls that resource.

## Untrusted input: the prompt-injection surface

Inbound event **content** is untrusted. A bubble and a grant control *who* may send
and *which* resources you receive, but the body of a GitHub comment, a Slack
message, or an email is attacker-controllable text that the agent will read and may
act on. Defenses are layered, not absolute:

- **Scope the blast radius.** Give deployed agents narrowly scoped service tokens, so
  a prompt-injected agent can do only what its own tokens allow.
- **Gate high-stakes actions.** Put irreversible or outward-facing steps behind
  workflow `await` gates that require a human to approve before the workflow
  continues.
- **Prefer deterministic workflows** for regulated processes, so the order of steps
  is fixed and only the reasoning within a step is model-driven.
- **Observe.** Full session transcripts and the event-and-decision log let you
  replay exactly what an agent saw and did.

## Trusted code: installing a team

A team pack is **trusted code**. Beyond prompts, a team can run shell on your
machine through `requires.check` commands (`config.py`), monitor `command:` entries
(`monitors/scheduler.py`), and `build:` hooks (`build_render.py`). Installing a team
therefore runs untrusted-author code against your credentials.

- **Review a team before installing it**, the same way you would review a dependency
  - especially one from a non-default registry or an arbitrary URL.
- The installed `run/package/` image is a frozen build artifact: regenerated
  verbatim on every install, never hand-edited. `bobi agent <name> doctor` flags
  drift against the install manifest.
- Bobi makes Bobi-owned runtime package roots read-only before handing control to
  an agent brain. Agents keep read/search access and keep existing execute bits
  for packaged scripts, while assigned repos, `run/workspace/`, `run/state/`,
  logs, and handoffs stay writable. The default local backend is POSIX `chmod`:
  it catches accidental writes from Claude, Codex, gateway-backed Claude, MCP
  tools, and future brains through the shared launch boundary, but it is not a
  hard sandbox when the agent process owns the files because the same UID can
  deliberately restore write bits. Managed deployments that need a stronger
  boundary should use read-only mounts or split ownership.

## Deployed instances

- Run **non-root** (uid 10001); Claude Code refuses `bypassPermissions` as root, so
  the container drops privileges with `gosu`.
- **Identity lives in the volume + env**, not the image - all instances share one
  image, and the per-instance bubble key and tokens are mounted, never baked.
- **Egress is not yet filtered.** A deployed agent can reach the open internet, so a
  prompt-injected agent could exfiltrate its own instance's tokens. This is an
  accepted internal-only risk today, mitigated by scoped per-instance tokens; an
  egress proxy is tracked for any non-employee/tenant use (epic #395).

## What's enforced vs. v1 boundaries

Enforced: bubble HMAC on publish/join, per-resource proof-of-access grants
(fail-closed at delivery), bubble-scoped outbound Slack, internal Worker-to-DO auth,
loopback-only local server, and webhook signature verification (GitHub, Slack,
Linear, WhatsApp - one pipeline with a structural verify slot per source, #639).

Known v1 boundary: inbound global webhook topics are grant-gated but tenancy is
coarse - a `linear:<team>` key can collide across two organizations. Binding inbound
webhooks to a specific account is the multi-tenant hardening tracked in **#239**.
Running a shared or public event server is a deliberate step: serve over TLS and
set every provider webhook secret (`WEBHOOK_SECRET`, `SLACK_SIGNING_SECRET`,
`LINEAR_WEBHOOK_SECRET`, `WHATSAPP_APP_SECRET` - plus `WHATSAPP_VERIFY_TOKEN`
for Meta's GET handshake) so every inbound route verifies. An unset github/
slack/linear secret admits that provider unverified, visible on `/health` as
`webhook_unverified`; WhatsApp FAILS CLOSED instead (an unverified WhatsApp
event would open an outbound send path, not just inject a notification).
