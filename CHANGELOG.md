# Changelog

## 0.39.0 - 2026-07-05

Minor release: the channel gateway lands (Slack rebuilt on the Chat SDK, inbound
and outbound), the unified `bobi app` web app, per-role and gateway-brain model
flexibility, scoped webhook ingest, and release-owned GHCR base images.

### Added
- **Channel gateway (#190).** Phase 1: durable conversation refs, a signed
  `/channels/send` API, and `bobi reply` (#620). Phase 2: Slack inbound and
  outbound moved onto the Chat SDK with placeholder/typing UX and capability
  degradation (#636). Over-budget sends now chunk at natural boundaries
  (#660), and a gated live-Slack soak suite guards the path (#644).
- **Unified web app (#525).** `bobi app` serves a dashboard, hosted
  onboarding, and chat from one place (#587).
- **Per-role model selection (#617).** `roles.<role>.model` picks the model
  per role, with a cross-model resume guard (#619).
- **Brain-level session continuation (#642).** Sessions continue across model
  switches instead of restarting cold (#646).
- **`kind: gateway` brain (#655).** Run a team on a local SLM through an
  Anthropic-compatible gateway (#659).
- **Scoped ingest tokens (#640, #641).** Mint per-topic tokens for
  `/webhooks/ingest/<topic>` plus an `ingest-token` CLI (#657); the client
  rides the shared signed transport (#663).
- **Signed event publish CLI (#606).** Publish arbitrary events from the CLI
  with bubble signing (#626).
- **`bobi build` (#610).** Render an agent team into a ready-to-run container
  image (#624).
- **GHCR base image on release (#609).** Releases publish
  `ghcr.io/moda-labs/bobi:<version>` (#631, #632).
- **Setup wizard growth.** Ingress wizard (#593); workflows card, event
  automations, and a next-steps finish flow (#627).
- **Two-tier semantic gate (#630)** for relevance-gated poll monitors (#633).
- **Manager health probe configuration (#604)** (#623).
- **`auto_dispatch` task templates** (#621).
- **Quickstart guide and concepts overview** docs (#594).

### Changed
- **One verified inbound-webhook pipeline (#639).** All inbound webhooks share
  a single verification path (#645).
- **One signed-request transport (#653).** Every Python event-server client
  signs and sends through `signed_request` (#658).
- **Slack parity normalizer deleted (#647)** after the Chat SDK soak completed
  clean (#648).

### Fixed
- **Queued session messages survive reconnect (#588)** (#589).
- **Passive Slack thread placeholders suppressed (#567)** (#616).
- **Workflow loop re-entry bounded (MOD-250)** (#597).
- **Question-only PR comments get answered** (#613).
- **MCP preflight timeout configurable** (#622).
- **Explicit subscriptions interpolate config refs (#607)** (#625).
- **Template-built teams appear on the setup home screen** (#595).

## 0.38.0 — 2026-07-02

Patch/minor release: fixes a production Codex OAuth device-login flood, plus
a per-step brain model override and other runtime fixes.

### Added
- **Per-step brain model override (MOD-240).** A team can set a `brain.model`
  default alongside `brain.kind`, and individual workflow steps can override
  `model:` to start a fresh brain session when the effective model changes.
  Claude aliases (`haiku`, `sonnet`, `opus`), full Claude model IDs, and Codex
  model IDs (e.g. `gpt-5-codex`) all pass through unchanged. (#550)

### Fixed
- **Codex OAuth device-login flood.** A subscription (Codex OAuth) instance
  re-posted a device-login code to its Slack login channel on every reboot.
  Both the entrypoint and the codex catalog `success` check misread a valid
  OAuth `auth.json` (which carries a null `OPENAI_API_KEY` field alongside its
  `tokens`) as a stale API-key file, wiping it and forcing a fresh login each
  boot. Now treated as API-key auth only when there is a real `OPENAI_API_KEY`
  value and no OAuth `tokens`. (#586)
- **Fastembed cache path baked into the image.** The Dockerfile set `HF_HOME`
  but never created the directory before the fastembed model download, so the
  subsequent `chmod` failed. The bake and runtime now share one explicit
  `/opt/bobi/models/fastembed` cache path, created ahead of time. (#579)
- **Surfaced agent failure causes (MOD-246).** Opaque "connection lost" session
  failures now distinguish network-drop, subprocess-timeout, and tool-crash
  causes; Codex subprocess stderr is preserved, and workflow draining surfaces
  clean brain error results instead of turning them into later handoff
  failures. (#570)
- **Fresh installs default to the local event server (#584).** Config
  interpolation's `${VAR:-default}` looked up the literal string `VAR:-default`
  in the environment instead of applying the default, so optional refs never
  fell back. This nudged fresh eng-team installs onto the Moda Cloudflare event
  server instead of the bundled local one. (#585)

## 0.37.0 — 2026-07-01

Minor release: gstack joins the tool-library catalog.

### Added
- **gstack in the tool-library catalog (#428).** The headless-browser QA /
  dogfooding toolchain (the `browse`, `qa`, `ship`, and `review` skills) is now a
  reusable catalog entry — a team pulls it in with `tool_library: [gstack]`, with
  the pins living once in the catalog instead of a hand-written per-team `build:`.
  The entry is self-contained: it declares its own `nodejs`/`npm` (the base image
  is Node-free) so its `bun` / Playwright / `./setup` install works standalone. (#583)

## 0.36.0 — 2026-07-01

Minor release: the unified tool-library dependency model (#428, epic #515),
Codex as a first-class base-image brain, plus runtime and web-UI hardening.

### Added
- **Unified agent-bootstrapped dependency model (#428).** A team declares CLI
  tools, skill libraries, and MCP servers as one concept — a dependency with a
  required `success` contract and optional `guide` / `install` / `host` / `mcp`.
  Guide-only deps are materialized by a bootstrap agent at image build, verified
  against `success` per brain, and snapshotted; a declared-set hash drives
  re-bootstrap. (#571, #577, #578)
- **Codex baked into the base image** as a first-class brain alongside Claude;
  `brain: codex` teams no longer bake Codex per-team. (#573)
- **MCP per-brain rendering.** A dependency's `mcp:` spec renders into Claude
  session options and Codex `~/.codex/config.toml`, verified by the `initialize`
  handshake. Direct `mcp_servers:` declarations keep working and win over a
  dependency's `mcp:` for the same server. (#580)
- **`bobi agents install --with-deps`.** Materialize a team's declared
  dependencies on the local machine: the on-machine brain installs them, adapting
  to the host, idempotently skipping already-satisfied ones, confirm-gated, and
  never running `sudo` silently. (#581)
- **Codex release-gate canary at parity.** `ci-codex-smoke` smokes the Codex
  brain from the release wheel as a hard gate alongside the Claude canary. (#574)
- **Resource-aware launch admission** to bound concurrent agent starts. (#565)

### Changed
- Extracted a runtime service core and a web-UI transport harness, and shared
  web-UI design tokens. (#560, #563, #559)
- Documentation pass: README rewrite, docs consolidation, Apache 2.0 license,
  and ticketing policy. (#546, #551, #555)

### Fixed
- **Deploy resolves a team's brain from the deployment yaml for `team-url`
  canaries**, so an api_key-mode Codex canary provisions with `OPENAI_API_KEY`
  instead of defaulting to Claude and demanding `ANTHROPIC_API_KEY`. (#582)
- Retry Claude `initialize` timeouts, exempt the agent lifecycle from the circuit
  breaker, and report session-rotation retry errors. (#564, #562, #569)
- Dropped the broken `openai` CLI catalog entry. (#575)
- Made the unit suite hermetic against env and subprocess-PATH leaks. (#576)

## 0.35.4 — 2026-06-27

Patch release for fleet deploys after the Bobi repo rename.

### Fixed
- **Inherited agent teams resolve from the live public registry.** The default
  team registry now points at `moda-labs/bobi-agent` instead of the old
  `moda-labs/bobi` slug, fixing `bobi deploy` for teams such as `eng-team` and
  `personal-assistant` that inherit from public base teams.

## 0.35.3 — 2026-06-27

Patch release for the remote Agent UI release gate.

### Fixed
- **Remote Agent UI checks work from clean machines.** `bobi agent <name> ui
  --app <fly-app> --check` no longer requires the named agent to be installed in
  the caller's local `BOBI_HOME`; local runtime binding is still enforced for
  local UI mode and other agent-scoped commands.
- **Remote Agent UI tunnels avoid local port collisions.** When `--port` is not
  supplied, the tunnel now picks a free localhost port instead of assuming the
  remote UI port is also free locally. This fixes release runners or operator
  machines that already have something listening on `localhost:8080`.

## 0.35.2 — 2026-06-27

Patch release for the Bobi release gate.

### Fixed
- **Release canary UI smoke uses the scoped CLI.** The canary workflow now runs
  `bobi agent canary ui --app "$canary" --check`, matching the named-agent CLI
  introduced in the Bobi cutover. The previous top-level `bobi ui` command no
  longer exists, so the v0.35.1 GitHub release gate stopped after the functional
  canary ask and before PyPI/Homebrew publishing.

## 0.35.1 — 2026-06-27

Patch release for the Bobi cutover release path.

### Changed
- **Framework releases are canary-specific (#544).** The `bobi-agent` release
  workflow now builds and smokes only the permanent `ci-canary`, then publishes
  PyPI/Homebrew. Generic `deployments/*.yaml` reconciliation is example-only in
  this repo and remains the responsibility of fleet repos such as
  `moda-agents`.
- **Codex test is example-only (#543, #544).** The former active `codex-test`
  deployment is now a manual example so Codex-brain validation can be separated
  from SSH/local-team delivery validation.

### Fixed
- **Forced rebuild deploys cover the team-url path (#542).** Existing
  `team-url:` deployments now rebuild the image when `bobi deploy --rebuild` is
  requested before reinstalling the package.
- **Release deploy reconciliation can request rebuilds (#541).** The generic
  deployment workflow example accepts and passes through a `rebuild` input for
  fleet repos that intentionally reconcile package content after a framework
  image update.

## 0.35.0 — 2026-06-27

Breaking Bobi cutover release: the framework is now published and operated as
`bobi`, and installed runtimes are named Bobi Agents under one machine-scoped
home directory.

### Breaking Changes
- **Renamed Modastack to Bobi (#524, #535, #537).** The Python package,
  console command, imports, environment variables, docs, skills, tests, and
  release automation now use `bobi`/`BOBI_*` names. This release intentionally
  does not carry backwards-compatibility aliases for the old Modastack package
  or command names.
- **Moved runtimes to named Bobi Agents (#538).** Runtime operations no longer
  bind implicitly to the current working directory. `BOBI_HOME` is the single
  low-level home root, defaults to `~/.bobi`, and is configurable only by
  environment variable. Each installed agent lives under
  `$BOBI_HOME/agents/<name>/` with source in `src/`, generated package files in
  `run/package/`, mutable state in `run/state/`, workspace files in
  `run/workspace/`, and credentials in `run/.env`.
- **Rebuilt the CLI around install-scoped and agent-scoped commands (#538).**
  Installation/package management lives under `bobi agents ...`; runtime
  operations live under `bobi agent <name> ...`; child executions are now
  `bobi agent <name> subagents ...`. The old CWD-scoped command shape was
  removed.

### Added
- **Setup harness status and local/cloud finalization (#514).** The setup UI
  now shows which harness runs the team, whether it is authenticated, and gives
  explicit local (`bobi agent <name> start`) and cloud deployment paths.
- **Machine-wide Bobi Agent docs and skills (#538).** README, packaged skill
  guides, setup instructions, and integration tests now describe the `src/` and
  `run/` model, environment-only `BOBI_HOME`, and named-agent command flow.

### Changed
- **Release and downstream repos now target Bobi (#539).** Release automation
  dispatches to the renamed Homebrew tap (`moda-labs/homebrew-bobi-agent`) and
  Moda team package repo (`moda-labs/moda-agents`), with the PyPI/Homebrew
  package name set to `bobi`.
- **Setup error handling is more direct (#514).** `/api/message` now blocks on
  an uninstalled CLI with a clear install message, surfaces actionable auth
  hints for unauthenticated harnesses, and redacts setup errors before they
  reach the SSE stream or history.

### Fixed
- **Monitor breaker keys are finding-specific (#523).** One breaker no longer
  suppresses unrelated findings from the same monitor.
- **Codex API key auth is materialized for child executions (#522).** Codex
  brain launches receive the expected API-key auth material instead of relying
  on ambient process state.

### Removed
- **Legacy Modastack compatibility paths and command names (#524, #535, #537,
  #538).** The release is a clean cutover to Bobi naming and the named runtime
  model.
- **Setup's "Start it for me" path (#514).** Users start installed agents from
  their terminal with the named-agent CLI.

## 0.34.12 — 2026-06-25

Bugfix release that supersedes the failed 0.34.11 canary run.

### Fixed
- **Internal Durable Object POST auth on Cloudflare.** Worker-to-DO `/init` and
  `/event` requests now include the internal auth token as a private query
  parameter in addition to the existing internal header. This matches the
  production-safe WebSocket fallback and fixes `POST /deployments` returning
  `500 Internal Server Error` while `bobi ask` tried to open a temporary
  reply channel for the canary smoke.
- **WebSocket transport fixes.** Includes the 0.34.10 and 0.34.11 fixes for
  production WebSocket upgrades and protocol negotiation.

## 0.34.11 — 2026-06-25

Bugfix release that supersedes the failed 0.34.10 canary run.

### Fixed
- **WebSocket protocol negotiation.** Event clients no longer send the
  deployment bearer token as a `Sec-WebSocket-Protocol` value. The Worker still
  authenticates WebSocket subscriptions with the normal `Authorization` bearer
  header, and removing the auth subprotocol avoids `websocket-client` rejecting
  otherwise-successful handshakes with `Invalid WebSocket Header` when the
  server does not select that subprotocol.
- **Production WebSocket session upgrades.** Includes the 0.34.10 fix that
  trusts the public Worker's deployment authentication for WebSocket upgrades
  while keeping internal `/init` and `/event` writes protected by the internal
  secret.

## 0.34.10 — 2026-06-25

Bugfix release that supersedes the failed 0.34.9 canary run.

### Fixed
- **Production WebSocket session upgrades.** `DeploymentSession` now trusts the
  public Worker's deployment authentication for WebSocket upgrades instead of
  requiring a second internal Durable Object auth token on that hop. Internal
  `/init` and `/event` writes still require the internal secret. This targets
  Cloudflare production handshakes that returned an empty `403 Forbidden` even
  after deployment auth succeeded for HTTP registration and subscription
  updates.
- **Production WebSocket upgrade preservation.** Includes the 0.34.9 request
  preservation fix, plus the earlier public and internal WebSocket auth
  fallbacks from 0.34.7 and 0.34.8.

## 0.34.9 — 2026-06-25

Bugfix release that supersedes the failed 0.34.8 canary run.

### Fixed
- **Production WebSocket upgrade preservation.** The Worker now wraps the
  original public WebSocket upgrade request when forwarding to
  `DeploymentSession`, changing only the internal URL token. This preserves
  Cloudflare's production upgrade metadata while still authenticating the
  Worker-to-Durable-Object hop.
- **Internal Durable Object WebSocket auth on Cloudflare.** Includes the 0.34.8
  private query-token fallback for Worker-created WebSocket requests.
- **Public WebSocket auth on Cloudflare.** Includes the 0.34.7 deployment-key
  subprotocol fallback for public event clients.

## 0.34.8 — 2026-06-25

Bugfix release that supersedes the failed 0.34.7 canary run.

### Fixed
- **Internal Durable Object WebSocket auth on Cloudflare.** Worker-created
  WebSocket requests to `DeploymentSession` now carry the internal DO secret in
  a private query parameter instead of relying on WebSocket headers surviving
  `DurableObjectStub.fetch()`. This targets the remaining bodyless `403
  Forbidden` seen after the public deployment key had already authenticated.
- **Public WebSocket auth on Cloudflare.** Includes the 0.34.7 client fallback
  that sends deployment bearer auth as a WebSocket subprotocol in addition to
  the `Authorization` header.
- **Release ordering for event-server hotfixes.** Includes the 0.34.6 release
  workflow change that deploys the Cloudflare event server before the canary
  smoke.

## 0.34.7 — 2026-06-25

Bugfix release that supersedes the failed 0.34.6 canary run.

### Fixed
- **Public WebSocket auth on Cloudflare.** Event clients now send their
  deployment bearer token in a dedicated WebSocket subprotocol in addition to
  the `Authorization` header, and the Worker accepts either form. This fixes
  Cloudflare WebSocket upgrades that returned `403 Forbidden` even though the
  same deployment key worked for HTTP subscription updates.
- **Release ordering for event-server hotfixes.** Includes the 0.34.6 release
  workflow change that deploys the Cloudflare event server before the canary
  smoke, while still keeping PyPI publish and fleet roll gated behind the
  canary.
- **Codex CLI auto-bake (#511, fixes #498).** Teams configured with
  `brain.kind: codex` now automatically bake the Codex CLI even when the team
  omits an explicit `tool_library: [codex]`, removing a deploy-time footgun for
  Codex-backed managers.
- **Worker-to-Durable-Object WebSocket auth.** Includes the server-side
  subprotocol auth path for internal Worker-to-DO WebSocket upgrades.

## 0.34.6 — 2026-06-25

Bugfix release that supersedes the failed 0.34.5 canary run.

### Fixed
- **Release ordering for event-server hotfixes.** The release workflow now
  deploys the Cloudflare event server before the canary smoke, while still
  keeping PyPI publish and fleet roll gated behind the canary. This lets
  server-side event-bus fixes be validated by the canary instead of being
  blocked by the older live Worker.
- **Codex CLI auto-bake (#511, fixes #498).** Teams configured with
  `brain.kind: codex` now automatically bake the Codex CLI even when the team
  omits an explicit `tool_library: [codex]`, removing a deploy-time footgun for
  Codex-backed managers.
- **Worker-to-Durable-Object WebSocket auth.** The Worker now authenticates
  internal WebSocket upgrades to `DeploymentSession` through a WebSocket
  subprotocol token, with the existing internal header retained for HTTP
  `/init` and `/event` calls. This fixes deployed managers that could register
  and update subscriptions successfully but then received repeated `403
  Forbidden` WebSocket handshakes and missed Slack events.

## 0.34.5 — 2026-06-25

Bugfix release for the event-server WebSocket auth path introduced in 0.34.4.

### Fixed
- **Codex CLI auto-bake (#511, fixes #498).** Teams configured with
  `brain.kind: codex` now automatically bake the Codex CLI even when the team
  omits an explicit `tool_library: [codex]`, removing a deploy-time footgun for
  Codex-backed managers.
- **Worker-to-Durable-Object WebSocket auth.** The Worker now authenticates
  internal WebSocket upgrades to `DeploymentSession` through a WebSocket
  subprotocol token, with the existing internal header retained for HTTP
  `/init` and `/event` calls. This fixes deployed managers that could register
  and update subscriptions successfully but then received repeated `403
  Forbidden` WebSocket handshakes and missed Slack events.

## 0.34.4 — 2026-06-25

Bugfix release for the Codex-backed Fly fleet, Slack event routing, and
event-server hardening after the 0.34 rollout.

### Added
- **Webhook resource grants (#491, closes #488).** Deployment registrations now
  declare the upstream Slack/GitHub/Linear resources they are allowed to
  subscribe to, and the event server enforces those grants before accepting
  webhook topic subscriptions.
- **Internal Worker-to-Durable-Object auth (#492, fixes #489).** Cloudflare
  Worker calls into deployment Durable Objects now use an internal shared secret
  instead of forwarding client bearer auth through the internal boundary.

### Fixed
- **Slack DM login channel resolution (#506, fixes #499).** Slack login channel
  values can be specified as readable user/channel references and are resolved
  against the configured bot token workspace.
- **Slack mention deduping (#508, fixes #496).** `app_mention` and
  `message.*` deliveries for the same Slack message are coalesced so mentioned
  bots do not create duplicate placeholder replies.
- **Homebrew release gate (#509, fixes #493).** Release validation now checks
  Homebrew bottle URLs so a green release cannot silently ship broken formula
  artifacts.
- **Codex skill exposure (#510).** Codex-backed sessions now receive baked
  skill paths so `/review`, `/qa`, `/browse`, and related gate commands work in
  deployed teams.
- **Codex launched-lead brain selection (#512).** Launched project leads honor
  the parent team's configured Codex brain instead of falling back to the
  default brain.
- **Child agent environment propagation (#516, fixes #513).** Child agent
  launches now inherit the documented runtime environment needed for brain,
  tool, and credential compatibility.

## 0.34.3 — 2026-06-25

Bugfix release for Codex-backed managers handling large streamed responses.

### Fixed
- **Large Codex JSON stream events (#505).** Raises the Codex subprocess stream
  limit so large single-line `codex exec --json` events do not crash the
  manager session with `Separator is not found, and chunk exceed the limit`.

## 0.34.2 — 2026-06-25

Bugfix release for Slack routing and event subscription recovery in the
Codex-backed Fly fleet.

### Fixed
- **Slack app topic isolation follow-up (#504).** Slack webhooks with
  `api_app_id` now fan out only to app-qualified topics, preventing stale
  workspace/channel subscriptions from cross-delivering events between bots.
- **Stale event-server credentials (#504).** A saved deployment key that gets a
  403 during subscription sync now triggers re-registration instead of leaving
  the manager connected with a dead WebSocket key.
- **Subscription cleanup on upgrade (#504).** Subscription updates can replace
  the desired topic set, removing legacy Slack topics such as `slack:<team>`
  after a deployment moves to `slack:<team>:app:<app>`.
- **Slack webhook URL verification (#504).** The Worker accepts both
  `/webhooks/slack` and `/webhooks/slack/` for Slack request URL verification.

## 0.34.1 — 2026-06-25

Bugfix release for the Codex-backed Fly fleet cutover.

### Fixed
- **Codex shell PATH in containers (#500).** Exposes `bobi` from both
  `/usr/local/bin` and `/home/bobi/.local/bin`, covering Codex tool shells
  that sanitize `PATH` and drop `/opt/venv/bin`.
- **Slack app cross-delivery (#503, fixes #502).** Slack events and
  subscriptions now use app-qualified topics (`slack:<team>:app:<app>` and
  app+channel variants), so Bobbers, eng-team, and other bots in the same Slack
  workspace do not receive each other's DMs after redeploy.
- **Fly volume secret drift (#503, fixes #501).** Existing-app deploy reconcile
  now syncs resolved secret values into `/data/project/.bobi/.env` and
  removes pruned keys from that file, preventing tool shells that lose inherited
  env from falling back to stale volume credentials.

## 0.34.0 — 2026-06-24

Adds the pluggable agent brain layer so a team can run on Claude Code or Codex
behind the same Bobi session interface, including Codex headless auth,
deploy wiring, and a `codex-test` team for smoke testing the new path. This is
the release to switch `eng-team` over to Codex-backed operation.

### Added
- **Pluggable agent brain (#495, closes #485).** Agent execution now goes
  through a `BrainSession` interface with Claude and Codex adapters, moving
  session, subagent, workflow, setup, and validation paths off direct
  Claude-only assumptions.
- **Codex brain support (#495).** Adds a `CodexBrain` backed by `codex exec`,
  normalized message handling, usage accounting, ANSI-stripped login scraping,
  and context-rotation behavior that avoids false storm detection from Codex
  turn-aggregate usage.
- **Brain-aware deploy and auth (#495).** Deploys can provision the right CLI
  and authentication flow for the selected brain, including container preflight
  checks that fail fast when required auth is missing.
- **`codex-test` team (#495).** Ships a minimal Codex-backed team and deployment
  config for validating the new brain path before cutting over production teams.

### Fixed
- **Agent UI transcript replay (#494).** Fixes replay behavior so the UI can
  show existing transcript history consistently when reconnecting.
- **Outbound Slack send auth (#490, closes #487).** Slack sends are now scoped
  to the active bubble/auth context so agents do not send through the wrong
  credentials.

### Changed
- **Slack login channel setup (#495).** Headless login can accept a readable
  `#channel-name` instead of requiring a raw channel ID.
- **Event server replay and auth paths (#494, #490).** Tightens replay and
  bubble-scoped authentication handling around the local and Worker event
  server implementations.

## 0.33.0 — 2026-06-24

Adds an installable **personal-assistant** team and makes `from:`-overlay teams
fully deployable — an overlay can now bake its `tool_library` CLIs into the image
and ship its per-principal `workspace/` to the instance. Also lands a
manager self-heal watchdog, the policy-curator as a framework default, and the
`eng-team-core` → `eng-team` rename.

### Added
- **`personal-assistant` team (#486).** A general-purpose, customizable personal
  assistant: a single generalist `assistant` role managing email, calendar, and
  to-dos through the bundled `venn` CLI over a Slack chat surface, with a
  configurable autonomy line in `workspace/assistant-context.md`. Declares its
  CLI via `tool_library: [venn]`. Derive a per-principal instance with
  `from: personal-assistant`.
- **`create-slack-bot` CLI (#486).** Renamed from `slack-manifest`; opens the
  one-click app-create link in the browser and ships an `im:write` scope.
- **Manager self-heal watchdog (#476, closes #464).** Defense-in-depth supervisor
  that restarts a wedged manager child.
- **Policy curator is now a framework default (#475, closes #471).** Opt-out.
- **Self-learning `script_cache` monitor runner (#478, closes #327).**

### Fixed
- **`tool_library` CLIs now bake into deploy images (#486).** The team-deps
  renderer read the raw leaf `agent.yaml`, so a team declaring its CLI via
  `tool_library:` (no inline `build:`) got nothing baked — the dispatch
  `requires:` gate then blocked every agent on the instance. The renderer now
  reads the composed build (from:-chain + tool_library).
- **`from:` overlays carry `workspace/` to the instance (#486).** The deploy
  flatten now merges the chain's workspace (leaf-wins), so an overlay's
  per-principal `assistant-context.md` reaches the box; local install seeds
  leaf-first to match.

### Changed
- **`eng-team-core` → `eng-team` rename + relocated teams removed (#483, closes
  #480).** The public engineering base is now `eng-team`; modernizes the dogfood
  team to `tool_library`.
- **`deploy-source` `max_concurrent_agents` default 4 → 8 (#482, closes #481).**

## 0.32.0 — 2026-06-24

Ends the recurring eng-team **rotation wedge** at its root: the
agent-maintained decision log is gone, replaced by a curated `policy.md`. Also
makes a Slack workspace safe for **more than one bobi bot** (the
self-reply spam loop), and adds a **reusable tool library** plus a **web UI**
for a running team.

### Added
- **Policy curator replaces the decision log (#460, closes #456).** The
  append-only, agent-written decision log — the root cause of the recurring
  context-rotation wedge — is replaced by a `policy-curator` monitor
  (`curator: true`) that distills new transcripts into a team-scoped, capped,
  rewritten-in-place `.bobi/state/policy.md`, injected read-only into every
  agent's prompt as `## Team Policy`. Agents no longer write their own log;
  durable knowledge persists via transcript → curator → policy. Publishes
  `policy.updated` (passive re-read by default, inbox push only for `urgent`).
  eng-team `director`/`project_lead` role prompts migrated to the model. See
  `docs/specs/456-policy-curator.md`.
- **Reusable tool library (#465, #416).** `tool_library:` in `agent.yaml` is an
  opt-in catalog of baked CLI tools (`bobi/tool_library/`). A team lists
  entries by id (`tool_library: [codex, venn]`) and `compose.py` expands each
  into its `requires:` + `build:` + a `tools/<id>.md` guide at build time — one
  pinned definition, reusable across teams, de-duped across `from:` layers. Ships
  `codex`, `venn`, `openai` (`kind: cli`). See `docs/specs/416-tool-library.md`.
- **Web UI for a running agent team (#461).** Cards for each agent with
  click-to-chat against the live team.
- **Slack app factory (#462).** A manifest generator + one-click create link for
  standing up a dedicated Slack app, plus a `url_verification` retry fix.

### Fixed
- **Multiple Slack bots per workspace (#466, #467, #468).** Two bobi bots in
  one workspace clobbered each other's self-filter (event server keyed Slack
  state by `team_id`, last-writer-wins) → bots replied to their own placeholders,
  a runaway spam loop. Re-keyed Slack state by `api_app_id` with per-app signing
  secrets and a `bot_id`-aware circuit breaker (#466); workspace registration now
  **merges** rather than **replaces** per-app records, so a secret-less
  re-register can't wipe a live signing secret (#467); and the self-filter skips
  **any** of the workspace's bots, not just the receiving app's, closing the
  cross-app loop (#468).
- **stdio MCP preflight (#463, MDS-63/MDS-64).** Fixed a poll race and an env
  mismatch in the stdio MCP server preflight.

### Changed
- **Ticket state reconciled + `/sync-tickets`.** `docs/TICKET_STATE.md` brought
  in line with live issues, with a `/sync-tickets` helper to keep it current.

## 0.31.0 — 2026-06-23

Agent teams become a **composable package ecosystem**. A team can declare
`from: <base-team>` and inherit it, contributing only its delta — Docker-style
composition at install/deploy time. Completes the bobi side of epic **#453**
(Team distribution & composition): #446 (resolution) + #451 (merge) + the #452
`eng-team` extraction. Ships the framework support the private
`moda-agents` cutover needs.

### Added
- **`from:` team inheritance, composed at install/deploy (#446, #451).** A team
  declares `from: <base-team>` (a `name`, `name@version`, or a local path) and
  `bobi install` (and `deploy`) walk the chain (`base → … → leaf`) and
  freeze one flat `.bobi/` image — nothing downstream learns about layers.
  Resolution is **local-always-wins** (checked-in `agents/<name>` → cache →
  registry) with **fail-fast** on a pin/local-version mismatch (a Cargo-quality
  error, never a silent fall-through), cycle + depth guards, and a recorded
  `compose-lock.json`. `install --pinned` resolves registry-only at locked
  versions for reproducible CI/deploy. Merge rules: **prose** surfaces
  (`agent.md`, `roles/*/ROLE.md`) concatenate in chain order (`replace: true`
  frontmatter overrides wholesale); **structured** surfaces (`tools/`,
  `workflows/`, `monitors/`, `context/`, `agent.yaml`) deep-merge by key —
  services/requires by name, `build` deps append + de-dupe, `auto_dispatch`
  appends with `id`-keyed replace, scalars last-wins; `prune:` drops inherited
  items; `workspace/` stays seed-if-absent. `deploy` flattens the chain on the
  host, so a dark instance never resolves a chain at first boot. New module
  `bobi/compose.py`.

### Changed
- **`eng-team` → pristine `eng-team` (#452).** The reference team is split
  into a portable, **tool-agnostic** `eng-team` (GitHub issues + Slack, a
  generic engineering lifecycle stated in terms of seams — your tracker, your
  review/test/QA gate) so any org can derive a house team with
  `from: eng-team` instead of forking ~2,000 lines. Moda's operational team
  moves to a thin `moda-eng-team` overlay (Linear, the gstack/codex toolchain,
  TS/Next house style, release policy) in the private `moda-agents` repo.
- **Agent teams are no longer bundled into the framework wheel.** Teams are
  versioned registry packages now; baking a frozen copy into the wheel pinned a
  team to the framework release and fought independent team versioning.
  `bobi setup` lists teams from the registry (a source checkout still lists
  the local `agents/` dir for dev). `bobi install <name@version>` and a
  `from:`-bearing team both compose by fetching their base from the registry.

### Packaging
- A team that declares a **path-based `from:`** is rejected at packaging
  (`scripts/check-publishable.py`, wired into `build-team-tarballs.sh`) — a path
  override is local-only and would arrive broken at a consumer (Cargo `[patch]`
  / Go `replace` ethos: overrides never leak into published artifacts).

## 0.30.0 — 2026-06-23

A stability release. The headline is **#454**: a rotation-metric over-count that
fired a perpetual false "rotation pending" and wedged a persistent session — the
same deaf-manager symptom as #443, different cause. Observed live on
`moda-eng-team` (director frozen ~2h40m mid-rotation, Slack "thinking…" refreshing
forever, user messages unanswered). Ships alongside a sub-agent
completion-delivery fix and Phase 2 of the versioned-team-package work.

### Fixed
- **Rotation metric no longer over-counts `cache_read` across a turn (#454).**
  `_context_fill_tokens()` was applied to the **`ResultMessage`'s cumulative turn
  usage**. In a multi-step turn (model → tool → model → tool → …) the cached prefix
  is re-read on every model call, so the aggregate summed `cache_read_input_tokens`
  across all N calls → reported context = **`real_context × N`** (a fresh ~65k-token
  session read `context=583061` ≈ `65k × ~9 steps`). That fired a perpetual **false**
  `rotation pending`, and the fragile auto-rotation it triggered wedged the session.
  Fill is now measured from a **single representative call** — the last
  `AssistantMessage`'s per-call usage — not the turn aggregate. The rotation path is
  also hardened: an over-cap rotation sets `_rotate_force` to bypass the
  `Flush no-op — INDEX.md unchanged, skipping rotation` guard (so a real over-cap
  self-heals even when the decision log is unchanged), and the flush is wrapped in a
  hard timeout (`ROTATION_FLUSH_TIMEOUT`) with bounded attempts
  (`ROTATION_MAX_FLUSH_ATTEMPTS`) so it can no longer hang or no-op-livelock. This is
  the over-correction of #433/#434 and is distinct from #443 (no 529 here).
  Reproduced failing-first against **real** `ResultMessage`/`AssistantMessage`
  objects in `tests/test_rotation_metric.py` (the `MagicMock` shape is what let #433
  ship).
- **Sub-agent completions now reliably reach the requester (MDS-65).** Detached
  sub-agents finished silently and crashes were recorded as `done`, so completed or
  failed work never reached the requester unless the launcher blocked on `--wait`
  (pinning a concurrency slot). The entry point now subscribes to
  `agent/session.{completed,failed}` and delivers lifecycle events to the inbox like
  monitor findings; terminal status uses an honest `completed`/`failed`/`crashed`
  vocabulary (never `done` on an error), is persisted to `state.json` *before* and
  independent of the best-effort bus POST, and a dead-pid sweep marks `crashed`. A
  new reconciler (`bobi/reconcile.py`), run on manager wake, re-emits
  unconfirmed terminals, marks dead-pid runs `crashed`, and times out hung runs —
  idempotent via `emit_confirmed` so healthy completions deliver exactly once.
  `requested_by` is threaded through the blocking, orchestrator, and resume paths so
  completions route to the requester's thread.

### Added
- **Versioned team fetch / install / deploy resolution (#440, Phase 2).** Consumes
  the Phase 1 immutable per-team packages (#442). A team **version** is now the unit
  of distribution: `bobi install <name>[@version]` and
  `bobi agents update <name>[@version]` accept a pin, and `deploy` resolves
  `team: <name>@<version>` through one seam. A single parse rule
  (`registry.split_team_ref()`, split on the last `@`) and one resolver
  (`deploy.resolve_team_dir()`, routing all four production call sites) back it. A
  **pinned** ref downloads only the immutable, token-authed asset and a **404 on a
  pin is a hard error** — never a silent fallback to latest; an **unpinned** ref
  resolves the registry's latest (a version-less team uses the rolling tarball) and
  falls back to the whole-repo path if assets aren't published yet. `version` is
  keyword-only (default `None`), so every existing caller and local/URL install is
  byte-for-byte unchanged. No fleet migration here.

### Changed
- **The release canary gate tolerates a cold image-swap boot (#449).** The v0.29.0
  gate false-failed on a good wheel: the `CANARY-OK` ask raced a cold boot (volume
  ownership + team install + session spin-up) under a too-tight 3 × 30s = 90s budget.
  The gate is now a dedicated `scripts/canary-smoke.sh` that starts the canary
  up-front and polls with a generous, bounded wall-clock budget
  (`CANARY_SMOKE_MAX_WAIT`, default 300s); a genuinely broken wheel still never
  answers and fails the gate.

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
- **`bobi deploy-init` scaffolds bring-your-own-repo CI (#439).** A new
  command that turns the bring-your-own-repo setup (DEPLOYMENT.md §7.2 B) into one
  step: from an agent-teams repo root it writes a standalone, actionlint-clean
  `deploy-agent-teams.yml` (installs `bobi` from PyPI, pinned to the running
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
  services gain a `required: true|false` flag (default false). `bobi start`
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
- **Per-key secret reconcile to the agent.yaml declared set.** `bobi deploy`
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
- **Build the Fly image locally on macOS/Docker-Desktop laptops.** `bobi
  deploy` detects the Docker Desktop socket via `docker context` and builds
  locally when the remote builder isn't reachable. (#387)
- **`resolve_root` honors its `start` arg after self-bind.** (#375)

### Changed
- Renamed the GitOps team-reconcile workflow `gitops-teams.yml` →
  `deploy-agent-teams.yml`. Subscription-login bootstrap now smoke-tested through
  the real Slack adapter shape, gating the release fleet roll. (#388)

## 0.26.0 — 2026-06-21

Reskin the `bobi setup` web UI to **bobi**: a single clay accent palette and
the probe-mark logo. Terminal layout and behavior are unchanged — only the color
tokens, the brand mark, and brand wording move. (MOD-190)

### Changed
- **bobi rebrand (setup UI).** Collapsed the amber/green accent switch to one
  clay accent, repointed the paper neutrals and the warm-void CRT slab to the
  bobi token set, and swapped the titlebar/rail glyph for the probe mark (paper
  body + dashed orbit + a single violet probe dot — the only violet in the
  product). Shipped `bobi-mark.svg` as the favicon, retitled the page, and
  aligned all user-facing setup copy with the Bobi brand. Source of truth:
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
  refs in `.bobi/.env`, never inline. (MOD-209)
- **Detect from a local folder.** Point at an MCP server's project folder and
  bobi infers the launch recipe — command/args from `pyproject.toml` /
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
- `bobi deploy` honors a declared-but-empty optional referenced var (e.g.
  `channels: ${SLACK_CHANNELS}`, empty = whole workspace) instead of failing on
  it; auth-critical keys are still enforced at provision and boot.

## 0.23.0 — 2026-06-19

Containerized instances land: bobi now runs as an immutable image on Fly,
deployable from the binary alone, with a fast-rebuilding layered Dockerfile.

### Added
- **Containerized instance image (C8).** One Dockerfile, two build modes
  (`BOBI_BUILD={source|pypi}`): `source` builds the wheel from a checkout
  (dev + repo CI), `pypi` installs a published, version-pinned `bobi` so a
  deploy needs no repo. Runs the agent non-root, ships the native `claude` CLI
  (no Node), and bakes the embedding model in for cold-start speed. (#338)
- **`bobi deploy` / `destroy` primitive + binary-only deploy (C22).**
  Idempotent provision-or-update with config precedence (flags ›
  `deployments/<name>.yaml` › `defaults.yaml` › built-ins). Deploy assets
  (Dockerfile, scripts, entrypoints) ship as wheel package data, so
  `uv tool install bobi` is enough to deploy — no checkout. (#342)
- **Fly provisioning + install-team-from-URL (C10).** `provision-instance.sh`
  and `bobi install <url>` deliver a team to a fresh instance. (#340)
- **Subscription-login bootstrap (C23).** First-boot subscription auth for a
  dark container. (#343)
- **GitOps thin clients.** Release / `deploy-*` tag workflows that are thin
  `bobi deploy` callers; `deployments/` holds per-instance config; a
  permanent `moda-canary` instance is the pipeline smoke. (#342)
- **First-class foreground / PID-1 mode + manager health endpoint.** `bobi
  start --foreground` as the container entrypoint, with a health port the
  Docker `HEALTHCHECK` probes. (#333)
- **`bobi install --non-interactive`** for unattended/container installs.
  (containerized-5)
- **Subagent concurrency semaphore** bounding parallel agent launches. (#334)

### Changed
- **fastembed/ONNX replaces the torch embedding sidecar.** The CPU instance no
  longer pulls torch + ~2 GB of CUDA wheels; embeddings run on the lightweight
  ONNX embedder. (#346)
- **Faster, layered Dockerfile.** Layers are ordered stable → volatile so a
  code-only rebuild is seconds instead of minutes: the fastembed model bake
  moves to a dedicated `model-baker` stage keyed only on the fastembed version,
  the `claude` CLI install sits above the framework, and the `bobi` venv is
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
  retired. Blocking `bobi ask` / `message --wait` is async request/reply
  correlated over a transient `reply/<uuid>` topic. (#268, #269)
- **Bubble-scoped isolation + HMAC signing (auth-v1).** `bobi start` mints
  one trust bubble; every agent joins it. Publishes and join-registrations are
  HMAC-signed and events are scoped to a bubble, so they can't be read or injected
  across instances sharing one event server. Local server binds loopback by
  default. (#240, #241)
- **Loop-safety backstops.** Delivery-path circuit breaker pauses runaway
  agent↔agent loops in a conversation (legitimate `inbox/*` exempt); spend governor
  caps agent invocations per rolling hour. (#299, #300)
- **Observability.** `bobi events` surfaces `inbox/*` messages; `doctor` and
  `/health` report bubble + auth status. (#301, #242)
- **Auto-rotate persistent sessions at the token cap.** (#274)

### Fixed
- `resolve_root` trust model hardened: ownership check + manager-set
  `BOBI_ROOT` env pin, so a planted ancestor `agent.yaml` can't capture a
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

The `bobi setup` web UI's team panel becomes a methodical interview and an
editable workspace: bobi walks each role one at a time, and every card opens
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

Single `.bobi/` per installation, and event delivery scoped to what
each session actually subscribed to.

### Changed
- One `.bobi/` directory per installation, holding both config and
  state (#245): `bobi/paths.py` is the only module that constructs
  `.bobi` paths; `resolve_root()` (agent.yaml walk-up) is the single
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
- `bobi doctor` gains a single-root check: recursive scan for stray
  `.bobi/` dirs below the installation, classifying agent.yaml-
  bearing strays (root-capture risk) separately from removable
  state-only leftovers

### Fixed
- Engineer dispatch died with "Workflow 'issue-lifecycle' not found"
  when a state-only `.bobi/` in a repo checkout captured root
  resolution (prod 2026-06-12) — the marker is now `agent.yaml`, which
  only `install` writes
- `bobi start` (default daemonized path) crashed with NameError
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
  `bobi ask` round-trip; promote regenerates prod config from the
  released pack (the v0.15.0 stale-config lesson). bobi-dogfood is
  archived.

## 0.15.0 — 2026-06-11

Event contract v2 — hard cutover, no compatibility shims (#177–#181).
Existing installs must re-run `bobi install <team>` and
`bobi start --fresh` after upgrading (see
docs/design/EVENT_CONTRACT_V2.md §6 for the runbook).

### Changed (breaking)
- v2 event envelope in both runtimes; legacy top-level `repo`/
  `team_key`/`workspace`/`channel`/`installation_id` fields removed (#177)
- Config loader reads credentials only from `services:` descriptors —
  legacy `slack:`/`linear:` blocks are ignored; `bobi install`
  regenerates agent.yaml (#178)
- Lifecycle topics `engineer/*` → `agent/*`; session names are
  role-parameterized; run identity is an explicit `run_key`
  (`agents launch --id`), no more issue-regex extraction (#179, #165)
- Runtime resolution uses only the installed pack — framework
  fallbacks removed (#176); monitor defaults likewise (#172)

### Added
- Agent decision log (memory primitive): per-agent persistent notes at
  `.bobi/state/memory/<session>/`, loaded at session start —
  decisions survive `--fresh` and session rotation (#174)
- Session rotation when the installed image changes (#173)
- Deterministic `auto_dispatch` rules: event→workflow routing that fires
  before the manager LLM sees the event (#205)
- support-manager agent pack (#200)
- dogfood-content-review pack absorbed in-repo; release battery installs
  into throwaway temp projects; bobi-dogfood retired (#180)
- Slack placeholder + typing status indicator (#189); Slack
  notification steps in issue-lifecycle (#192)
- Director onboarding and reconciliation from the decision log (#175)
- Chat SDK bridge adapter spike, Cloudflare Workers validated (#191)

### Fixed
- events.jsonl interleaved-write corruption; `bobi events` no
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
  frozen to `.bobi/context/` (manifest-tracked, doctor-covered).
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
  reference bobi CLI commands that exist

### Fixed
- `bobi ask`/`message` resolve the coordinator by the installed
  `entry_point` role — previously hardcoded the literal role "manager",
  breaking the interactive loop for any pack with a different
  coordinator name
- Tool guides taught nonexistent CLI commands (`bobi slack-send`,
  a fictional `bobi linear` group); Linear guides rewritten against
  the real GraphQL API and verified live

### Changed
- Tool-guide authoring doctrine: guides carry team policy; CLI syntax
  lives in drift-proof surfaces (`--help`, `bobi skill`); raw-API
  mechanics only for services the framework doesn't wrap
- Authoring and onboarding docs cover `context/`, `workspace/`, and the
  function-vs-policy rule

## 0.13.0 — 2026-06-10

Full-codebase simplify pass: net −1,300 lines with no behavior changes
beyond the fixes below. Verified by the unit, integration, event-server,
and dogfood batteries.

### Fixed
- `bobi start --fresh` and `transcript show manager` now resolve the
  real manager session name (`moda-<entry_point>-<project>`) — previously
  they targeted a nonexistent `moda-mgr-*` name, so `--fresh` cleared nothing
- `bobi agents show` / `agents cancel` now work from the CLI — they
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
- Event publishing moved to `bobi.events.publish.post_event` with a
  memoized server URL — library code no longer imports the CLI module
- Shared helpers consolidated into `sdk` (`pid_alive`, `read_pid`,
  `state_dir`, cached runtime-root resolution), `events.server.health()`,
  and `config.parse_env_file`
- Agent prompts list workflows via the same dispatcher as
  `bobi workflows list` (same tiers and dedup)
- Performance: workflow run files parsed once per read, KB store reuses
  one SQLite connection, embedder caches the sidecar port, Cloudflare
  worker fans out to KV/Durable Objects in parallel, local event-server
  buffer eviction is O(1)

## 0.7.1 — 2026-06-05

### Added
- CI pipeline: unit tests + fast integration on GitHub-hosted, Claude integration tests on self-hosted EC2 runner
- Release pipeline: dogfood smoke test — installs from PyPI, starts bobi in dogfood repo, files a ticket, waits for bobi to close it, then restarts all configured repos with the new version
- `deploy/setup-ci-runner.sh` for provisioning new self-hosted runner instances

### Changed
- `--repo` flag removed from all CLI commands — bobi always detects the repo from cwd

## 0.7.0 — 2026-06-05

### Breaking
- **All runtime state moved to per-repo `.bobi/`** — PID files, logs, sessions, event server state now live under `<repo>/.bobi/state/` instead of `~/.bobi/`. Credentials moved to `~/.config/bobi/credentials.yaml` (XDG standard); existing credentials are migrated automatically on first load
- **`--repo` flag removed from all CLI commands** — bobi always detects the repo from the current directory. Commands like `agents launch`, `monitors add/pause/remove`, and `roles list` no longer accept `--repo`
- **`GlobalConfig` class removed** — machine-wide config via `Config` (`~/.bobi/config.yaml`); `RepoConfig` and `LocalConfig` later consolidated into `Config`

### Removed
- Legacy tmux session management (`bobi/tmux.py`, `bobi/session.py`) — all sessions now use the Claude Agent SDK
- `~/.bobi/` global directory dependency — the framework no longer reads or writes to the home directory for runtime state

### Fixed
- Detached agent subprocesses now call `set_repo_root()` so they can find workflows and write session state to the correct per-repo directory
- `workflows validate` command updated for the current step-based workflow schema (was referencing removed DAG attributes)
- `monitors remove` now correctly finds monitors in the current repo when `--repo` is not specified
- `bobi start` info display now shows per-repo log path instead of global

### Added
- Auto-resolve merge conflicts: `monitor/pr.conflict_detected` now triggers the manager to auto-spawn an engineer that follows a `merge-conflict` skill (#117)
- Comprehensive integration test suite (55 tests) running against a fully isolated temp install — CLI commands, agent launching, event server lifecycle, manager start/stop/message/ask, and full end-to-end webhook-to-manager pipeline

## Unreleased

## 0.4.1 — 2026-06-01

### Added
- Engineer lifecycle events: `bobi spawn` and workflow-managed engineers now emit `engineer/session.started`, `engineer/session.completed`, and `engineer/session.failed` to the event bus, so the manager can narrate engineer activity without polling (#103)
- Events post fire-and-forget over HTTP (`POST /api/event`) on a daemon thread, reusing the same path monitor checks use, so delivery never blocks or breaks an engineer run
- Manager event formatter now surfaces `phase`, `duration`, `summary`, and `error` fields from lifecycle events

## 0.4.0 — 2026-06-01

### Added
- Background monitoring system: scheduled polling tasks that fill webhook gaps by detecting conditions and injecting synthetic events into the manager's event stream (#100)
- Three-tier monitor storage (built-in `monitors/defaults.yaml` → user `~/.bobi/monitors.yaml` → repo `.bobi.yaml`), merged with later tiers overriding by `name` and repo-level `enabled: false` opt-out
- Built-in default monitors: PR conflict check (15m) and stale-PR check (1h), both working out of the box
- `bobi monitor add/list/pause/remove` CLI for managing monitors across tiers
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
