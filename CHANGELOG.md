# Changelog

## 0.29.0 — 2026-06-23

A stability release. The headline is **#443**: a single transient `529 Overloaded`
on a turn permanently wedged a persistent session — the agent stayed alive but
went deaf until a process restart. Turn-level API errors are now non-terminal and
self-heal, so a momentary overload no longer bricks a running fleet.

### Fixed
- **A transient turn error no longer wedges a session (#443).** A single
  `529 Overloaded` (or any `ResultMessage.is_error`) on a turn set the terminal
  `error` state that nothing ever cleared: `_process_message` then silently
  dropped every subsequent event while `is_alive()` reported the session dead, so
  the agent stayed up but went deaf until a process restart — observed live on
  `moda-eng-team` (director idle 2h15m after a 15:14 UTC 529). Turn-level API
  errors are now non-terminal: the error is surfaced but the session returns to
  `waiting_input` so the next event is served (the SDK client stays connected — the
  failure is scoped to the turn). Transient statuses (408/409/429/5xx/529) get a
  bounded in-band retry with exponential backoff so the triggering event
  self-heals; non-transient 4xx recover without retrying. The Slack "thinking…"
  refresh loop is now cleared on the dropped-message paths too, fixing the
  indicator that refreshed forever (the visible symptom). Reproduced failing-first
  by `tests/test_session.py::TestTurnErrorRecovery`.

### Added
- **Versioned immutable team packages (#440, Phase 1).** `build-team-tarballs.sh`
  now also emits an immutable `<team>-<version>.tar.gz` alongside the rolling
  `<team>.tar.gz`. A new `team-version.py` helper is the single authority on what
  is pinnable (strict `MAJOR.MINOR.PATCH` only; absent/prerelease/malformed →
  rolling-only). `publish-team-tarballs.sh` uploads the versioned tarball without
  `--clobber` so immutability is fail-closed (a re-publish 422s and is skipped as a
  no-op), and a new `check-team-versions.py` CI step asserts `registry.yaml` agrees
  with each team's `agent.yaml` and that the pinned version is strict semver. This
  is the publishing half only — inert at runtime; no consumer reads the new assets
  yet (fetch/deploy land in later phases).
- **`modastack deploy-init` scaffolds bring-your-own-repo CI (#439).** A new
  command that turns the bring-your-own-repo setup (DEPLOYMENT.md §7.2 B) into one
  step: from an agent-teams repo root it writes a standalone, actionlint-clean
  `deploy-agent-teams.yml` (installs `modastack` from PyPI, pinned to the running
  version) plus a `deployments/` skeleton, then prints the exact `fly`/`gh`
  commands to wire `FLY_API_TOKEN` and the per-tenant GitHub Environment — with
  each team's per-key secret list derived from its declared `${VAR}`s. Non-
  destructive (`--force` to overwrite).

## 0.28.0 — 2026-06-22

A stability release. The headline is **#409**: agent sessions (project leads and
sub-agents) were dying at init roughly every 1–2h when the cloud event-server
registration handshake timed out. Registration is now non-fatal with background
retry, so a transient timeout no longer takes out a running fleet.

### Fixed
- **Event-server registration is non-fatal at startup (#409).** Agent sessions
  died during init when the cloud registration handshake timed out — a failed
  registration re-raised, the session went to `error` state, and the process
  died, taking out project leads and sub-agents about every 1–2h. Events are
  cloud-queued, sequenced, and resumable, so a late registration just resumes the
  stream from the saved cursor. The boot path now does one fast probe so a slow
  event server can't stall `start()` and trip liveness probes; on failure a daemon
  thread retries with capped exponential backoff (2s→60s), logging without
  terminating. A lock guards hand-off of a background-registered subscription to
  `stop()` so shutdown never leaks a live client/drain thread. Server-side, the
  registration read-timeout goes 15s→30s and the `ensure_bubble` mint-wait budget
  30s→45s to stay above it.
- **Reviewer follow-up comments no longer get silently dropped (#326).** The
  reactor's dedup key was PR-level (`workflow:topic:number`) with a 1800s
  cooldown, so a reviewer's follow-up comments on the same PR collapsed onto the
  first comment's key and were dropped. The per-delivery event id is now appended
  to the dedup key, so distinct comments each dispatch while genuine redelivery
  still dedups.
- **pr-feedback no longer posts duplicate comments (#321).** The engineer
  addressing review feedback no longer comments on the PR itself; it reports
  what it changed in a new `resolution_summary` handoff field. The lead — which
  already posts the acknowledgment before dispatching — now posts the single
  resolution comment from that handoff, keeping one voice per feedback cycle.

### Added
- **Graceful preflight degradation for non-required services (#329).** Declared
  services gain a `required: true|false` flag (default false). `modastack start`
  and `doctor` now block only on the entry point and required-service failures;
  other failed service checks render as warnings (⚠) and start proceeds in
  degraded mode. Essential services in the shipped packs are marked
  `required: true`, so this doesn't silently loosen them. Preflight status glyphs
  (⚠/✗/✓) fall back to `[WARN]/[ERROR]/[OK]` on unicode-stripped terminals.
- **Auto-fix CI failures on any open PR (#323).** The project lead's "Auto-fix CI
  failures" standing instruction now covers any open PR — agent- or
  human-authored — not just agent-authored PRs. A failing check on any open PR
  blocks the merge queue, so all branches get auto-fixed.

### Changed
- **Unified, canary-gated release pipeline (#401).** The release is now one gated
  `release.yml`: subscription-login smoke → build the wheel once → build the
  canary from that wheel + `CANARY-OK` smoke (the gate) → then publish the same
  wheel to PyPI (+ Cloudflare event-server + Homebrew) and roll the Fly fleet in
  parallel → reconcile team packages + secrets.

## 0.27.0 — 2026-06-21

Close out the **containerization epic (#344)**: the EC2 → Fly migration is
complete and eng-team runs as a team-flavored Fly instance. This release ships
the last close-out items and the secret-injection revamp that makes a deploy a
single declarative reconcile, then validates the whole path end-to-end by rolling
the live fleet.

### Added
- **Per-key secret reconcile to the agent.yaml declared set.** `modastack deploy`
  now treats the team's `agent.yaml` `${VAR}` refs as the authoritative secret
  surface: it sets every declared secret it's given and prunes any live Fly secret
  the team no longer declares. The GitOps Action (`deploy-agent-teams.yml`)
  materializes a tenant's per-key `<TEAM>__<KEY>` secrets (from a GitHub
  Environment) into an `--env-file`, and the engine reconciles them to Fly. A
  subscription-mode deploy refuses a supplied `ANTHROPIC_API_KEY` and prunes any
  stray live one. (#385)
- **`gh` CLI auth declared as a first-class secret.** eng-team's GitHub service
  declares `credentials.token: ${GH_TOKEN}`, so the gh-CLI token is materialized
  on deploy and never pruned by the reconcile. (#385)
- **Slack file send/receive and thread reading.** Agents can attach and receive
  files and read full Slack threads. (MOD-208)
- **aichat as the CLI-first model gateway.** Bake `aichat` into the image as the
  model gateway; retire the bespoke gateway connection kind. (MDS-48/MDS-49)

### Fixed
- **Build the Fly image locally on macOS/Docker-Desktop laptops.** `modastack
  deploy` detects the Docker Desktop socket via `docker context` and builds
  locally when the remote builder isn't reachable. (#387)
- **`resolve_root` honors its `start` arg after self-bind.** (#375)

### Changed
- Renamed the GitOps team-reconcile workflow `gitops-teams.yml` →
  `deploy-agent-teams.yml`. Subscription-login bootstrap now smoke-tested through
  the real Slack adapter shape, gating the release fleet roll. (#388)

## 0.26.0 — 2026-06-21

Reskin the `modastack setup` web UI to **bobi**: a single clay accent palette and
the probe-mark logo. Terminal layout and behavior are unchanged — only the color
tokens, the brand mark, and brand wording move. (MOD-190)

### Changed
- **bobi rebrand (setup UI).** Collapsed the amber/green accent switch to one
  clay accent, repointed the paper neutrals and the warm-void CRT slab to the
  bobi token set, and swapped the titlebar/rail glyph for the probe mark (paper
  body + dashed orbit + a single violet probe dot — the only violet in the
  product). Shipped `bobi-mark.svg` as the favicon, retitled the page, and
  renamed all user-facing brand copy `modastack → bobi` (CLI commands, install
  paths, and docs URLs stay `modastack`). Source of truth:
  `docs/design/BOBI_STYLE_GUIDE.md`.

## 0.25.0 — 2026-06-21

Author and live-test custom **stdio (command-based) MCP servers** in the setup/Bobi
connections UI. The runtime already supported stdio servers; this fills the
authoring gap (previously HTTP/URL-only), and adds folder-detection, an in-chat
connection test, and a per-row connection-status indicator.

### Added
- **Stdio MCP connections.** Add a local command-based MCP server (name +
  command + args + env) in the connections UI; persisted to `agent.yaml` as a
  `{type: stdio, command, args, env}` entry with secrets captured as `${VAR}`
  refs in `.modastack/.env`, never inline. (MOD-209)
- **Detect from a local folder.** Point at an MCP server's project folder and
  modastack infers the launch recipe — command/args from `pyproject.toml` /
  `package.json`, and env vars (required vs optional, secret vs plain) by AST
  scan, with a confidence guard for highly-configurable servers. Home-confined,
  read-only static analysis.
- **In-chat connection test.** Ask Bobi to test a connection; it launches the
  server, proposes a safe read-only tool, and — on your confirmation — calls it
  to verify the connection end-to-end. Never proposes or runs a write tool.
- **Connection-status indicator.** A subtle per-row dot: connected (verified) /
  needs-config / error / added.

### Internal
- Canonical-key dedup so a guessed service and the MCP added for it collapse to
  one row; edit repopulates the stored config; `serialize_state` exposes
  `mcp_servers` (names/refs only, never secret values).
- Hardening from a cross-model (Claude + Codex) review: default-deny read-only
  tool picker, decline-first chat confirmation, minimal child env on probe,
  secret-scrubbed probe output, coarse-only test-verdict persistence.

## 0.24.0 — 2026-06-20

Team-flavored images: a team can bake its own host tools into a per-team
container image, so a real team (eng-team's `gstack`/`codex`) actually runs and
dispatches on Fly. The EC2 release path is retired in favor of a functional Fly
canary gate.

### Added
- **Team-flavored images (C24).** A team declares a `build:` block in
  `agent.yaml` (`apt` / `npm` / `run_root` / `run`, `verify: requires`); the
  framework renders a team-deps hook into the one Dockerfile — a stable layer
  *below* the framework wheel, so a code-only release rebuilds only the wheel —
  and builds it on Fly during deploy. `~`-relative tools (e.g. gstack's skills)
  are seeded onto the volume `$HOME` at boot so they survive the volume remap;
  `run_root` covers root steps `apt` can't express (e.g.
  `npx playwright install-deps chromium`). A no-`build:` team is byte-identical
  to the generic image. (#368)
- **Functional Fly canary gate.** `gitops-release` builds the canary first and
  asserts it answers a blocking `ask` end-to-end through the production event
  server (`CANARY-OK`) before rolling the rest of the fleet — the on-Fly
  replacement for the retired EC2 release smoke.
- **Team-aware fleet roll.** A framework release rebuilds each team-flavored
  instance's own image (its baked tools, on the new framework wheel) instead of
  rolling the generic image onto it.

### Changed
- **Retired the EC2 release path.** Removed the self-hosted release smoke /
  promote-to-prod-director (`publish-pypi`) and real-Claude integration (`ci`)
  jobs; the EC2 director is replaced by `moda-eng-team` running on Fly.
- `modastack deploy` honors a declared-but-empty optional referenced var (e.g.
  `channels: ${SLACK_CHANNELS}`, empty = whole workspace) instead of failing on
  it; auth-critical keys are still enforced at provision and boot.

## 0.23.0 — 2026-06-19

Containerized instances land: modastack now runs as an immutable image on Fly,
deployable from the binary alone, with a fast-rebuilding layered Dockerfile.

### Added
- **Containerized instance image (C8).** One Dockerfile, two build modes
  (`MODASTACK_BUILD={source|pypi}`): `source` builds the wheel from a checkout
  (dev + repo CI), `pypi` installs a published, version-pinned `modastack` so a
  deploy needs no repo. Runs the agent non-root, ships the native `claude` CLI
  (no Node), and bakes the embedding model in for cold-start speed. (#338)
- **`modastack deploy` / `destroy` primitive + binary-only deploy (C22).**
  Idempotent provision-or-update with config precedence (flags ›
  `deployments/<name>.yaml` › `defaults.yaml` › built-ins). Deploy assets
  (Dockerfile, scripts, entrypoints) ship as wheel package data, so
  `uv tool install modastack` is enough to deploy — no checkout. (#342)
- **Fly provisioning + install-team-from-URL (C10).** `provision-instance.sh`
  and `modastack install <url>` deliver a team to a fresh instance. (#340)
- **Subscription-login bootstrap (C23).** First-boot subscription auth for a
  dark container. (#343)
- **GitOps thin clients.** Release / `deploy-*` tag workflows that are thin
  `modastack deploy` callers; `deployments/` holds per-instance config; a
  permanent `moda-canary` instance is the pipeline smoke. (#342)
- **First-class foreground / PID-1 mode + manager health endpoint.** `modastack
  start --foreground` as the container entrypoint, with a health port the
  Docker `HEALTHCHECK` probes. (#333)
- **`modastack install --non-interactive`** for unattended/container installs.
  (containerized-5)
- **Subagent concurrency semaphore** bounding parallel agent launches. (#334)

### Changed
- **fastembed/ONNX replaces the torch embedding sidecar.** The CPU instance no
  longer pulls torch + ~2 GB of CUDA wheels; embeddings run on the lightweight
  ONNX embedder. (#346)
- **Faster, layered Dockerfile.** Layers are ordered stable → volatile so a
  code-only rebuild is seconds instead of minutes: the fastembed model bake
  moves to a dedicated `model-baker` stage keyed only on the fastembed version,
  the `claude` CLI install sits above the framework, and the `modastack` venv is
  the last heavy layer. `source` mode now splits a pyproject-keyed deps layer
  from a thin `--no-deps` wheel layer (dep list read from
  `[project.dependencies]` via stdlib `tomllib`, no drift). This is the layer
  ordering team-flavored images (C24) inherit — see
  `docs/design/CUSTOM_AGENT_DEPS.md` §"three clocks".
- **`[kb]` extra avoided in images.** Both builders install `fastembed` +
  `sqlite-vec` explicitly, since some published `[kb]` extras stale-list
  `sentence-transformers` → torch.

### Fixed
- **State format version marker** so an upgraded CLI detects and migrates stale
  on-disk state instead of failing against it. (#337)
- **Skip the local Node event server when `event_server_url` is remote** — a
  containerized instance talks to the remote event server, not a local one.
  (containerized-6)
- **Release promote no longer leaves prod down** on a post-stop failure. (#347)
- **Container-safe `claude` CLI path resolution.** (containerized-1)
- **Leaked asyncio event loop** that failed ~53 unit tests in full-suite runs.
  (#318)

### Internal
- Phase-0 containerization review fixes (C3/C4/C5). (#356)
- Design docs for containerized instances and custom agent dependencies (C24).
  (#368, #369)

## 0.22.0 — 2026-06-18

Codex-as-a-tool, a methodical setup connections flow, and a round of comms /
release-pipeline hardening on top of the v0.21.0 auth+comms foundation.

### Added
- **Codex as a tool.** MCP connection + inject wiring so agents can call the
  Codex CLI; preflight that resolves Codex subscription vs API-key auth and
  fails fast when neither is available. (#288, #320)
- **Setup connections flow.** MCP cascade, a guided Venn connection flow, and
  add-your-own custom MCP; plus setup UI updates, bundled team templates, and a
  build idle-timeout. (#298, #291)
- **Cheap detector + escalate-on-hit monitor** — a low-cost first-pass detector
  that escalates to a full check only on a hit. (#294)

### Fixed
- **Release smoke repaired.** The orphaned local `:8080` server and an unsigned
  Smoke 1 event (403) no longer fail the smoke and block auto-promote. (#315)
- **Cloudflare upgrade restart.** On upgrade, a stale pre-bubble
  `deployment_state` is detected and the client re-registers instead of issuing
  a doomed stale PUT — no more manual `--fresh`. (#316)
- **Slack event de-duplication** prevents double placeholder messages. (#324)
- **Quieter cold-start + reconnect logs.** Lifecycle emits that fire before the
  bubble is minted no longer POST an unsigned event guaranteed to 403; the event
  client logs routine Cloudflare DO reconnects at debug and warns only on a real
  flap streak. (#317)

### Internal
- Event-server integration tests now run against the real Worker via
  `wrangler dev` in CI. (#312)

## 0.21.0 — 2026-06-18

The inter-agent comms + event-bus security foundation: agents talk over the event
server inside isolated, authenticated trust bubbles. (Also ships the previously
unreleased 0.20.0 setup-UI work.)

### Added
- **Inter-agent comms over the event server (comms-v1).** Agents message each
  other as `inbox/<session>` events; the per-session HTTP inbox transport is
  retired. Blocking `modastack ask` / `message --wait` is async request/reply
  correlated over a transient `reply/<uuid>` topic. (#268, #269)
- **Bubble-scoped isolation + HMAC signing (auth-v1).** `modastack start` mints
  one trust bubble; every agent joins it. Publishes and join-registrations are
  HMAC-signed and events are scoped to a bubble, so they can't be read or injected
  across instances sharing one event server. Local server binds loopback by
  default. (#240, #241)
- **Loop-safety backstops.** Delivery-path circuit breaker pauses runaway
  agent↔agent loops in a conversation (legitimate `inbox/*` exempt); spend governor
  caps agent invocations per rolling hour. (#299, #300)
- **Observability.** `modastack events` surfaces `inbox/*` messages; `doctor` and
  `/health` report bubble + auth status. (#301, #242)
- **Auto-rotate persistent sessions at the token cap.** (#274)

### Fixed
- `resolve_root` trust model hardened: ownership check + manager-set
  `MODASTACK_ROOT` env pin, so a planted ancestor `agent.yaml` can't capture a
  process. (#249)
- Transient `reply/<uuid>` deployments deregistered on `ask` teardown, plus a
  crash-time eviction backstop. (#277, #279)
- Same-name re-register dedup + cursor ACK-after-delivery durability. (#278)
- `pr-feedback` no longer auto-dispatches on `review_requested`. (#255)

### Internal
- Integration test revamp (anti-rot CI, real-Claude flakiness fixes, registry
  coverage); Cloudflare Worker/miniflare suite now runs in CI. (#261, #307)
- Project-lead role prompt hardened with standing operational instructions. (MDS-55)

### Security
Bubble isolation is enforced in local-server mode. **Cloudflare mode is gated** on
follow-up hardening (Durable Object internal-RPC auth, KV CAS) tracked in
`docs/SECURITY-FINDINGS.md` — do not enable it until those land. Cross-tenant
inbound-webhook fan-out remains accepted v1 behavior (→ #239).

## 0.20.0 — 2026-06-17

The `modastack setup` web UI's team panel becomes a methodical interview and an
editable workspace: modastack walks each role one at a time, and every card opens
for inspection and editing.

### Added
- Methodical, one-agent-at-a-time interview: the digestion brain interviews each
  role in turn, announces phase transitions, and gathers four dimensions per role
  (what it does, what good looks like, systems it accesses, what triggers it). A
  phase banner in the panel shows where the interview is; each role tracks
  in-progress vs complete.
- Editable team panel: click a role or automation to open a modal and edit it;
  add roles, automations, and connections by describing them or via a button.
  New routes `/api/role/update`, `/api/automation/update`, `/api/service/remove`,
  and `/api/build-integration` (a placeholder for building an MCP/CLI integration
  on the fly).
- Connections: a Venn upsell when no key is set (`venn_configured` on
  `/api/connect`), per-connection trash, and an unmistakable connected state
  (filled green pill; the Venn modal shows a success seal instead of a "Re-check"
  CTA once everything is connected).
- A celebratory pulse when each of the five slots completes, plus gentler
  state-change motion (per-card reconcile, phase ease-in, meter tick).

### Changed
- The Connections slot counts as gathered only once every implied service is
  actually connected, not merely named.
- The assistant directs the user to the Connections card to set up services, then
  returns to chat once they are connected.

### Fixed
- The streaming chat reply no longer flashes a trailing blank line while the
  hidden spec block loads.
- The chat column no longer leaves a dead gap before the team panel.

### Removed
- The quick-add suggestion chips (they disrupted the conversation flow).

## 0.19.0 — 2026-06-12

Single `.modastack/` per installation, and event delivery scoped to what
each session actually subscribed to.

### Changed
- One `.modastack/` directory per installation, holding both config and
  state (#245): `modastack/paths.py` is the only module that constructs
  `.modastack` paths; `resolve_root()` (agent.yaml walk-up) is the single
  filesystem resolver; every process binds its root exactly once at its
  entry point — the manager at start, children from the `root` their
  spawner passes in the args blob, CLI commands on first resolve. All
  cwd-based fallback chains are gone: an unbound process raises instead
  of inventing a root, and `bind_root` refuses to re-identify a running
  process
- One event-server deployment per session (#244): subscriptions are no
  longer unioned across agents sharing a project root, so project leads
  stop receiving (and answering) the director's Slack DMs; per-session
  event cursors replace the shared cursor file
- CLI commands fail with a clean usage error outside an installation
  (previously: silent cwd binding, or raw tracebacks from `transcript`
  and `workflows` subcommands); `doctor` warns instead of reporting
  green when no installation is found
- `modastack doctor` gains a single-root check: recursive scan for stray
  `.modastack/` dirs below the installation, classifying agent.yaml-
  bearing strays (root-capture risk) separately from removable
  state-only leftovers

### Fixed
- Engineer dispatch died with "Workflow 'issue-lifecycle' not found"
  when a state-only `.modastack/` in a repo checkout captured root
  resolution (prod 2026-06-12) — the marker is now `agent.yaml`, which
  only `install` writes
- `modastack start` (default daemonized path) crashed with NameError
  after the state-dir refactor; only `--foreground` was exercised in CI
- Image rotation was silently disabled for workflow/worktree sessions:
  manifest hashing ran against cwd (no manifest there) instead of the
  installation root; role prompts and monitor check subprocesses had the
  same cwd-as-identity bug
- A child spawned with an args blob missing `root` (old manager + new
  code during an upgrade window) raises a diagnostic naming the fix —
  restart the manager — instead of a bare KeyError; a root without
  agent.yaml is refused before any state is written
- The event drain thread survives reactor exceptions instead of dying
  silently while its queue grows unbounded
- Slack workspace registration sends `bot_id` explicitly, hardening the
  self-reply filter that let leads' own replies re-ingest as DMs

## 0.18.0 — 2026-06-12

Unified monitor event path: every monitor flavor publishes through the
event server on one detect → reconcile → publish chain.

### Changed
- All monitor flavors (notify, command, native check, description-only)
  are now pure condition detectors feeding a single dedup + publish path
  in the scheduler (#237): findings publish through the event server
  instead of the in-process queue, gaining events.jsonl visibility,
  seq/replay durability, and delivery to any subscriber
- Description-only check agents only observe: the scheduler captures
  the check's verdict, converts it to conditions (keyed on details.key /
  details.id / summary hash), and dedups deterministically — agent-side
  dedup-by-judgment is gone, and the check prompt forbids it
- Topic contract: the event server routes path-topic events onto both
  the bare type and the source-qualified topic (monitor/<type>), so
  subscriptions written as the full event string match natively —
  removing the quirk the #235 hotfix had to encode

### Fixed
- A monitor condition is recorded active only after its event actually
  publishes — an unreachable event server means retry next interval,
  never a silently lost finding
- Indeterminate detection (failed command, check exception, missing
  verdict) leaves dedup state untouched instead of clearing active
  conditions (extends #236 into the state layer)

## 0.17.0 — 2026-06-11

Auto-dispatch for issue assignment and a monitor subscription fix.

### Added
- Issue assignment auto-dispatches to the issue-lifecycle workflow
  (#226): `github.issues.assigned` events route deterministically to the
  workflow instead of relying on the manager LLM to route them
- Integration test for Slack self-reply loop prevention (#218):
  workspace registration accepts an optional `bot_id` so tests can
  bypass Slack `auth.test`

### Fixed
- Monitor event subscription is unconditional via MonitorRegistry
  (#219): packs using only native adapters (slack/linear/github) never
  subscribed to monitor topics, and `cfg.monitors` was empty for
  install-model packs since monitors live in `monitors/defaults.yaml`

## 0.16.0 — 2026-06-11

Slack routing fixes: channel-scoped team routing and the self-reply loop.

### Added
- Channel-scoped Slack routing (#208): events emit `slack:TEAM:CHANNEL`
  alongside the workspace topic; a service's `channels:` (list or
  comma-separated `${SLACK_CHANNELS}`) scopes its subscription so several
  teams can share one bot in one workspace, each waking only for its own
  channel(s). No channels configured = whole workspace, as before. DMs
  stay workspace-level.

### Fixed
- Slack self-reply loop (#209): the workspace bot identity is registered
  with the event server so the bot's own messages no longer come back
  around as inbound events
- Release smoke runs against the in-repo pack with no external repo —
  posts a synthetic event to a subscribed topic and requires a blocking
  `modastack ask` round-trip; promote regenerates prod config from the
  released pack (the v0.15.0 stale-config lesson). modastack-dogfood is
  archived.

## 0.15.0 — 2026-06-11

Event contract v2 — hard cutover, no compatibility shims (#177–#181).
Existing installs must re-run `modastack install <team>` and
`modastack start --fresh` after upgrading (see
docs/design/EVENT_CONTRACT_V2.md §6 for the runbook).

### Changed (breaking)
- v2 event envelope in both runtimes; legacy top-level `repo`/
  `team_key`/`workspace`/`channel`/`installation_id` fields removed (#177)
- Config loader reads credentials only from `services:` descriptors —
  legacy `slack:`/`linear:` blocks are ignored; `modastack install`
  regenerates agent.yaml (#178)
- Lifecycle topics `engineer/*` → `agent/*`; session names are
  role-parameterized; run identity is an explicit `run_key`
  (`agents launch --id`), no more issue-regex extraction (#179, #165)
- Runtime resolution uses only the installed pack — framework
  fallbacks removed (#176); monitor defaults likewise (#172)

### Added
- Agent decision log (memory primitive): per-agent persistent notes at
  `.modastack/state/memory/<session>/`, loaded at session start —
  decisions survive `--fresh` and session rotation (#174)
- Session rotation when the installed image changes (#173)
- Deterministic `auto_dispatch` rules: event→workflow routing that fires
  before the manager LLM sees the event (#205)
- support-manager agent pack (#200)
- dogfood-content-review pack absorbed in-repo; release battery installs
  into throwaway temp projects; modastack-dogfood retired (#180)
- Slack placeholder + typing status indicator (#189); Slack
  notification steps in issue-lifecycle (#192)
- Director onboarding and reconciliation from the decision log (#175)
- Chat SDK bridge adapter spike, Cloudflare Workers validated (#191)

### Fixed
- events.jsonl interleaved-write corruption; `modastack events` no
  longer crashes on malformed lines (#182)
- Project lead prompt delegates all work, stays responsive (#149)
- market-research pack migrated to v2 service-descriptor credentials —
  legacy blocks would silently resolve to empty tokens
- Release smoke job installs the in-repo pack from the tagged checkout
  (the renamed path didn't exist in the dogfood clone)

## 0.14.2 — 2026-06-11

Same code as 0.14.0 plus pipeline and diagnosability fixes.

### Fixed
- Production promotion installs CPU-only torch (#161) — the prod box
  has no GPU; CUDA wheels were ~7GB of disk for zero runtime benefit
  and overflowed the runner during `uv tool install`
- Local event-server launch surfaces npm output on failure — npm errors
  (e.g. disk full) were captured but never logged, leaving a bare
  CalledProcessError in manager.log

## 0.14.1 — 2026-06-11

Same code as 0.14.0; re-released to get a working release pipeline.

### Fixed
- Release smoke test and Claude CI jobs install CPU-only torch — the
  CUDA wheel stack (~7GB) repeatedly filled the self-hosted runner disk
  (#161), failing the dogfood gate before promotion

## 0.14.0 — 2026-06-11

Agent teams can now ship runtime files, and the first non-engineering
pack lands: market-research. Trialed end-to-end in a fresh project
(install, all three research workflows, manager + inbox + monitors,
live Linear API).

### Added
- `context/` pack subdir — team-shipped reference files, installed
  frozen to `.modastack/context/` (manifest-tracked, doctor-covered).
  Agents get an index (path + first line) in their prompt and read
  files on demand; contents are never inlined
- `workspace/` pack subdir — seed templates for user-owned domain files.
  Install copies to `<project>/workspace/` only if absent; reinstall
  never overwrites user or agent edits
- market-research agent team: persistent `research_manager` coordinating
  `topic_researcher`, `landscape_scanner`, and `pmf_navigator`; five
  workflows; KB-backed research corpus with typed entries
  (`topic::`, `voice::`, `company::`, `snapshot::`, `pmf::`)
- Prompt-lint test (`tests/test_tool_guides.py`): pack prompts may only
  reference modastack CLI commands that exist

### Fixed
- `modastack ask`/`message` resolve the coordinator by the installed
  `entry_point` role — previously hardcoded the literal role "manager",
  breaking the interactive loop for any pack with a different
  coordinator name
- Tool guides taught nonexistent CLI commands (`modastack slack-send`,
  a fictional `modastack linear` group); Linear guides rewritten against
  the real GraphQL API and verified live

### Changed
- Tool-guide authoring doctrine: guides carry team policy; CLI syntax
  lives in drift-proof surfaces (`--help`, `modastack skill`); raw-API
  mechanics only for services the framework doesn't wrap
- Authoring and onboarding docs cover `context/`, `workspace/`, and the
  function-vs-policy rule

## 0.13.0 — 2026-06-10

Full-codebase simplify pass: net −1,300 lines with no behavior changes
beyond the fixes below. Verified by the unit, integration, event-server,
and dogfood batteries.

### Fixed
- `modastack start --fresh` and `transcript show manager` now resolve the
  real manager session name (`moda-<entry_point>-<project>`) — previously
  they targeted a nonexistent `moda-mgr-*` name, so `--fresh` cleared nothing
- `modastack agents show` / `agents cancel` now work from the CLI — they
  read the on-disk session registry instead of an in-process dict that was
  always empty (cancel terminates the agent's detached process)

### Removed
- Legacy fire-and-forget executor (`run_phase`, `run_phase_sync`,
  `inject_message`) and its private event loop — the supervised session
  path is the single executor
- Orphaned modules: `relay`, `scanner`, `board_setup`, `setup`
- `WorkflowRun` node-DAG API (`find_active`, `find_completed`,
  `retry_failed`, `NodeState`) — the orchestrator is a linear step
  executor; `workflows status` shows step/awaiting instead of node counts
- Phantom `agent_name` parameter across config/validate/subscriptions/
  monitors; `ProjectConfig`/`Config.from_file` aliases; the unused
  built-in roles tier

### Changed
- Event publishing moved to `modastack.events.publish.post_event` with a
  memoized server URL — library code no longer imports the CLI module
- Shared helpers consolidated into `sdk` (`pid_alive`, `read_pid`,
  `state_dir`, cached runtime-root resolution), `events.server.health()`,
  and `config.parse_env_file`
- Agent prompts list workflows via the same dispatcher as
  `modastack workflows list` (same tiers and dedup)
- Performance: workflow run files parsed once per read, KB store reuses
  one SQLite connection, embedder caches the sidecar port, Cloudflare
  worker fans out to KV/Durable Objects in parallel, local event-server
  buffer eviction is O(1)

## 0.7.1 — 2026-06-05

### Added
- CI pipeline: unit tests + fast integration on GitHub-hosted, Claude integration tests on self-hosted EC2 runner
- Release pipeline: dogfood smoke test — installs from PyPI, starts modastack in dogfood repo, files a ticket, waits for modastack to close it, then restarts all configured repos with the new version
- `deploy/setup-ci-runner.sh` for provisioning new self-hosted runner instances

### Changed
- `--repo` flag removed from all CLI commands — modastack always detects the repo from cwd

## 0.7.0 — 2026-06-05

### Breaking
- **All runtime state moved to per-repo `.modastack/`** — PID files, logs, sessions, event server state now live under `<repo>/.modastack/state/` instead of `~/.modastack/`. Credentials moved to `~/.config/modastack/credentials.yaml` (XDG standard); existing credentials are migrated automatically on first load
- **`--repo` flag removed from all CLI commands** — modastack always detects the repo from the current directory. Commands like `agents launch`, `monitors add/pause/remove`, and `roles list` no longer accept `--repo`
- **`GlobalConfig` class removed** — machine-wide config via `Config` (`~/.modastack/config.yaml`); `RepoConfig` and `LocalConfig` later consolidated into `Config`

### Removed
- Legacy tmux session management (`modastack/tmux.py`, `modastack/session.py`) — all sessions now use the Claude Agent SDK
- `~/.modastack/` global directory dependency — the framework no longer reads or writes to the home directory for runtime state

### Fixed
- Detached agent subprocesses now call `set_repo_root()` so they can find workflows and write session state to the correct per-repo directory
- `workflows validate` command updated for the current step-based workflow schema (was referencing removed DAG attributes)
- `monitors remove` now correctly finds monitors in the current repo when `--repo` is not specified
- `modastack start` info display now shows per-repo log path instead of global

### Added
- Auto-resolve merge conflicts: `monitor/pr.conflict_detected` now triggers the manager to auto-spawn an engineer that follows a `merge-conflict` skill (#117)
- Comprehensive integration test suite (55 tests) running against a fully isolated temp install — CLI commands, agent launching, event server lifecycle, manager start/stop/message/ask, and full end-to-end webhook-to-manager pipeline

## Unreleased

## 0.4.1 — 2026-06-01

### Added
- Engineer lifecycle events: `modastack spawn` and workflow-managed engineers now emit `engineer/session.started`, `engineer/session.completed`, and `engineer/session.failed` to the event bus, so the manager can narrate engineer activity without polling (#103)
- Events post fire-and-forget over HTTP (`POST /api/event`) on a daemon thread, reusing the same path monitor checks use, so delivery never blocks or breaks an engineer run
- Manager event formatter now surfaces `phase`, `duration`, `summary`, and `error` fields from lifecycle events

## 0.4.0 — 2026-06-01

### Added
- Background monitoring system: scheduled polling tasks that fill webhook gaps by detecting conditions and injecting synthetic events into the manager's event stream (#100)
- Three-tier monitor storage (built-in `monitors/defaults.yaml` → user `~/.modastack/monitors.yaml` → repo `.modastack.yaml`), merged with later tiers overriding by `name` and repo-level `enabled: false` opt-out
- Built-in default monitors: PR conflict check (15m) and stale-PR check (1h), both working out of the box
- `modastack monitor add/list/pause/remove` CLI for managing monitors across tiers
- Native check runners (`pr_conflicts`, `stale_prs`) with per-condition deduplication; description-only monitors fall back to manager interpretation

## 0.3.3 — 2026-05-27

### Added
- Documentation: composable skills principle, workflow resolution chain (repo > user > default), and event normalization table (GitHub Issues + Linear to task.* format)

## 0.3.2.1 — 2026-05-27

### Fixed
- README phase routing table and handoff example now use the correct `implement_complete` phase name (was `implementation_complete`)

## 0.3.2 — 2026-05-26

### Added
- Mermaid flowchart diagrams in README: event flow, issue lifecycle, skill composition, and deploy pipeline

## 0.3.1 — 2026-05-26

### Changed
- CLI help text for `workflow` and `history` subcommands now includes descriptions and usage examples

## 0.2.2 — 2026-05-23

### Added
- Stall detection: heartbeat tracking via output hashing detects sessions idle >5 min (nudge) or >10 min (kill)
- Permission prompt detection: sessions blocked on interactive approval are identified and reported
- Process liveness checks: dead claude processes inside live tmux sessions emit `worker.process_dead`
- Auto-routing: manager prompt now routes engineers to the next phase based on handoff state

## 0.2.1 — 2026-05-23

- Self-updating: version check poller, Slack notification, user-approved update
- Slack threading fix — conversations inline, only proactive updates threaded

## 0.2.0 — 2026-05-20

- Event-driven architecture with persistent manager session
- Linear + GitHub Issues task tracking
- Slack Socket Mode for real-time events
- Engineer lifecycle: pickup, spec, implement, prepare-pr, feedback
- Orphan session detection
