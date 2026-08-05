# Changelog

## 0.56.0 - 2026-08-05

Minor release: the single-agent page that 0.55.0 gave the local dashboard now
works on a **hosted** fleet too — the runtime that connects the published
pieces moves into the framework, and the six read/write commands the hosted
page was missing are added to the admin protocol. Plus the review-remediation
sweep's Phase 4 security and user-surface batches.

### ⚠ Breaking for out-of-tree `TeamRuntime` implementations

Seven `TeamRuntime` methods — `runs`, `overview`, `run_details`, `resume_run`,
`remind_run`, `close_run` and the widened `transcript` — are now
`@abstractmethod`, and their base-class fallbacks are deleted. A subclass that
does not implement all seven **cannot be instantiated**. Both in-tree
implementations (`LocalRuntime`, `EventBusRuntime`) satisfy the ABC; anyone
carrying a private subclass must implement them or delegate to the shared
builders, which are importable for exactly that reason.

### Added
- **`EventBusRuntime` is part of the framework (#967, #963).** This repo
  shipped the sidecar (`bobi/supervisor/`), the Cloudflare event bus
  (`event-server/worker/`) and the console UI (`bobi/webapp/`) while
  withholding the ~470 lines that connect them, so every published piece could
  be self-hosted and a hosted console still could not be assembled. The class
  now lives at `bobi/webapp/event_bus.py` as a pure move — the only diff
  against its former home is five docstring phrases that became false once the
  file changed repos, and two import lines in its tests.
- **The hosted single-agent page gains history and composition (#968, #963).**
  `/overview` and `/runs` answered 409 on a hosted agent while `/health`,
  `/status`, `/spend` and the lifecycle verbs answered 200 — an agent could be
  watched and recovered, but what it had *done* and what it was *made of* were
  missing. Six new admin commands (`runs`, `overview`, `run_details`,
  `resume_run`, `remind_run`, `close_run`), each a thin delegate to the same
  pure builder `LocalRuntime` already calls, so there is one read
  implementation and the two runtimes cannot drift. `SUPERVISOR_VERSION` moves
  **0.1.0 → 0.2.0** (additive, per the compatibility promise).
- **`transcript` takes an optional `detail: true`.** The chat view (`messages`)
  has already discarded every tool call, so a hosted debugging transcript built
  by reshaping it would silently omit most of what an agent *did* between
  speaking. The reply gains `entries` + `usage`; a supervisor too old to know
  the argument replies without `entries`, and that is reported as unavailable
  rather than rendered as a debugging view with the tool calls missing.

### Fixed
- **A `prune:` entry could delete host files (#961).** Prune names an item on a
  surface, never a path, but nothing validated it: an absolute name collapsed
  the staging join to that absolute path, and a `..` segment walked out of the
  staging directory, so `bobi agents install` `rmtree`'d host directories.
  Compose now raises `ComposeError`.
- **`/api/credential/value` served ambient secrets (#961).** The setup wizard's
  endpoint fell back to `os.environ` for any requested name, so anything
  holding the page's per-launch nonce could read a secret merely exported in
  the launching shell — never saved through setup and outside the endpoint's
  own justification. `run/.env` is now the whole surface.
- **The setup picker confined paths to the wrong tree (#961).** The boundary
  was BOBI_HOME rather than the user's home directory that the comment,
  DESIGN.md and the 400 message all claimed, so `/api/mcp/detect` rejected
  every real project folder with a message the path already satisfied.
- **An unescaped Slack app name broke the manifest (#961).** `Bobi: Staging`
  rendered an unparseable manifest and `Bobi #1` silently created an app named
  `Bobi`; names are now emitted through the YAML emitter, byte-identically for
  ordinary ones.
- **A commented-out line in `agent.yaml` took down every start/status/dispatch
  path (#965).** A key present with an empty value is YAML null, so
  `raw.get(key, default)` returned None and the default never applied —
  `services:`, `requires:`, `event_server:` and `spend_cap:` all crashed with a
  traceback naming neither the key nor the file.
- **The setup wizard silently discarded an MCP edit made during a probe
  (#965).** The handler captured the entry, awaited a probe of up to 60s, then
  wrote that stale snapshot back — and a result for the *old* command would
  have marked the *new* one connected, rendering a never-tested config green.
- **`event-server stop` wedged on a truncated pid file (#965).** It raised on
  garbage and on `PermissionError`, leaving the stale files behind so every
  later stop failed identically.
- **`agents update` exited 0 when every pack failed (#965)**, while the
  named-pack form exited 1 for the identical failure.
- **`agents browse` died on an unquoted `version: 1.0` (#965)**, taking down
  the whole listing over one row; the same coercion fixes a str-vs-float
  comparison that made an installed pack read as an upgrade to itself.
- **One malformed session crashed a team's cost rollup (#965).** The fold's
  comment promised it must not 500 on one bad session and the token fields were
  guarded; the cost fields were not, and a string is truthy.
- **Remove-then-re-add crashed `bobi agents install` (#965)** — the natural
  idiom for wholesale-replacing an inherited keyed entry deep-merged into the
  tombstone and raised a raw TypeError.
- **An open-mode pack could install from unvalidated source (#965).** The
  validation-freshness gate sat behind `if state.mode == "create":` although
  DESIGN.md already called a fresh validation to install a hard floor.
- **Switching a team off Slack left it running the Slack adapter (#965).**
  `chat` is a setup-managed overlay key but the overlay only ever wrote it.
- **One binary file 500'd the setup review viewer (#965)** — `/api/file` had no
  decode handling while `/api/files` lists binaries with no suffix filter.

### Changed
- **`--resume` is removed from `bobi setup` (#965).** The webapp resumes an
  unfinished session unconditionally, so the flag named the default. All four
  documentation sites are corrected, including the disconnect overlay that
  printed the now-erroring command to users.
- **The public/private line is documented where it is enforced (#967).**
  `AGENTS.md` and `docs/ADMIN_PROTOCOL.md` record which side each piece lives
  on and point at the reference client.

## 0.55.0 - 2026-08-04

Minor release: the dashboard gains a real per-agent page, and the
review-remediation sweep lands its first three Lane A phases — session
lifecycle honesty, persistence atomicity, and agent-pack routes that had been
failing silently.

### Added
- **The single-agent view (#948, MOD-261).** The machine-scoped dashboard
  becomes a real per-agent page. Behind it is a **unified runs read model**: a
  monitor firing, a chat, and a workflow run are one list of runs rather than
  three unrelated event sprays — which is also why a monitor now records **one
  run per firing** instead of a spray of events. Agent state becomes a
  **tri-state** (running / stalled / stopped) rather than a bare pid check, so
  "the process exists" stops being mistaken for "the agent is working", and the
  page can surface a stalled workflow run and offer to resume it.

  New per-agent nouns under `/api/agents/{name}`: `GET runs`, `overview`,
  `health`, `sessions`, `details`, `spend`, and `POST
  workflows/runs/{run_id}/resume`. The overview also reports what the script
  cache **did not** spend. Integrates #906, #912, #913, #914, #915, #916, #919
  and the #941 restyle, which standardizes the surface on the product palette.

### Fixed
- **A session could not be stopped while its startup turn was still in flight
  (#949).** `_keep_alive` is created only after the startup drain, so `stop()`
  silently no-opped, `join(15)` expired, and the thread plus its brain
  subprocess kept running — a long-lived orchestrator leaked a live,
  token-burning agent for every timed-out phase while reporting "session failed
  to start". Setting an event cannot interrupt a turn parked in
  `await client.receive_response()`, so `_run` is now a cancellable task that
  `stop()` cancels through the loop, and teardown disconnects the client
  explicitly rather than relying on a `finally` the startup path never reaches.
- **`start()` waited out the full timeout after the session thread had already
  crashed (#949).** A launch that failed in milliseconds measured **30.006s**,
  and launch paths pass timeouts up to **3600s** — so a phase that could not
  start stalled dispatch for an hour. Liveness is re-checked only *after*
  re-reading `_ready`, since a thread may set it and then exit and a naive
  `is_alive()` poll reports a perfectly good session as failed.
- **A supervised agent's terminal failure was never persisted (#949).** The
  `except asyncio.TimeoutError` handler was unreachable — the caller's
  `wait_for` cancels from outside, and `CancelledError` is a `BaseException` —
  so `TERMINAL_FAILED` never landed and `state.json` recorded neither the
  failure nor the timeout reason, leaving the reconciler nothing to re-emit.
- **Every durable writer now shares one atomic-write helper (#951).** The
  serialize / write-temp-sibling / `os.replace` pattern had been
  re-implemented in **six** places with six temp-naming schemes, while a
  comparable set of state writers used a bare `write_text` and could be
  truncated by exactly the crash the six copies each guarded against. Because
  every loader here treats unparseable state as *empty*, the cost is never
  "lose one field": a torn monitor state file re-fires every monitor, a torn
  spend file zeroes the window the runaway-loop backstop counts, a torn
  `setup.json` makes `bobi setup --resume` discard the whole wizard session,
  and a torn `config.toml` loses the operator's foreign codex keys for good.
  `bobi/fsutil.py` is now the single implementation (`atomic_write_text`,
  `atomic_write_json`, `file_lock`); the setup wizard and the spend governor
  additionally take a lock, and corrupt monitor state now loads as "resetting".
- **Agent-pack routes that silently matched nothing (#957).** Three shipped
  packs carried rules that could never fire, and all failed the same way —
  nothing logged, nothing errored, the deterministic behavior just never
  happened and the work fell through to whatever the LLM decided.
  `eng-team`'s `github.issues.assigned` matches a type no adapter emits (GitHub
  emits `github.<header>` with the action in `fields.action`), so
  `issue-lifecycle` never fired on assignment — **this is why issue pickup has
  needed a Slack directive**. The dogfood workflow's `issues_count > 0` used an
  operator the condition parser does not support and fell through to a bare
  truthy check, routing a 3-issue audit to `done` and **closing the issue as
  having passed review**; the replacement is fail-safe, routing a missing count
  to `fix`. The dogfood pack also declared `chat: slack` with no slack service.
  Validation now catches this class via a **per-source** event-type table —
  arity is only meaningful per source, since Linear emits
  `linear.<dataType>.<action>` with the action *in* the type — and fails open on
  any adapter it does not recognize. Both new checks are warnings, not startup
  blockers: an unmatchable rule is inert, but refusing to start every deployed
  team whose installed pack still carries the old spelling would turn a silent
  dead rule into a fleet outage on upgrade. `eng-team` 1.5.2 → **1.5.3**,
  `dogfood-content-review` 1.2.1 → **1.2.2**.
- **A raise from `_make_session` escaped the workflow orchestrator's
  terminal-honesty handler (#957).** The registry entry stuck at `running` with
  no `session.failed` or `workflow.failed` until the dead-man reconciler
  mis-reported it.
- **The Worker deploy smoke gated on a version carrying the *previous* run's
  credentials (#954).** `wrangler deploy` and the `wrangler secret bulk` after
  it publish two Worker versions and Cloudflare's rollover between them is not
  atomic — but both report the same `BOBI_RELEASE_SHA`, so a sha-only readiness
  gate was structurally unable to tell them apart, and the deploy version
  inherits the prior run's secrets. Readiness is now "the version carrying
  **this run's** credentials is live, consistently": on 5 consecutive probes the
  health sha matches, an operator-authenticated route answers 200 to the freshly
  minted token, and `worker.version_id` holds still. The logic moved out of
  inline shell into `scripts/await_worker_ready.py` — the gate had survived two
  previous fixes as untestable shell — and is now driven over real HTTP against
  a Worker that fakes the rollover. A `concurrency` group also stops two runs
  from stomping each other's secrets on the shared smoke Worker.

### Changed
- **`AGENTS.md` states the dated plan-filename convention the repo already
  follows (#950)** — `plans/<YYYY-MM-DD>-<slug>.md`, the shape the installed
  stage pack validates and all 11 existing plans already use. Existing undated
  paths stay valid.
- **Lane C of the review-remediation plan is 2 PRs, not 3 (#947).** A dated
  amendment records the 2026-08-04 decision to defer its six remaining web-UI
  items, since the single-agent work above rewrites those surfaces. None of the
  six is a security hole; Phase 7's real security cluster shipped in 0.54.0.

## 0.54.0 - 2026-08-04

Minor release: the MCP fleet-control surface gains the tools that *act*, and a
security + reuse sweep lands across the event server.

### Added
- **MCP fleet control gains its write half (#944).** `POST /mcp` on the
  event-server Worker adds `bobi_read_transcript`, `bobi_send_message`, and
  `bobi_lifecycle` alongside 0.53.0's three read tools, so an agent can act on a
  fleet rather than only observe it. **No new authority and no new admin
  vocabulary** — the same `FLEET_OPERATOR_TOKEN` gate runs before any tool body,
  and the tools drive the same nine commands the sidecar already answers. The
  route widens the interface, not the authorization.

  Commands resolve through a **bounded server-side wait** (5s,
  `MCP_COMMAND_WAIT_MS`, `0` disables): fast commands return in one tool call
  instead of three, slow ones return `status: "pending"` plus a `command_id` for
  `bobi_command_result`. `pending` means not-yet-answered, never failed. The wait
  polls by command id and **never a KV prefix listing** — keyed reads are
  strongly consistent and listings are not, so a listing-based wait would report
  `pending` for a command that had already resolved. Verified by folding a
  supervisor result from a separate request while a tool call is in flight,
  rather than assumed.

  Three behaviors are stated in the tool descriptions because an agent would
  otherwise believe the opposite. **`bobi_send_message` does not return the
  agent's reply and does not wait for one** — the supervisor runs `chat` on a
  detached thread that resolves only when the whole turn ends, minutes later, so
  the reply is read back with `bobi_read_transcript`. **Transcript output is
  untrusted third-party content**, returned in its own content block between an
  explicit opening and closing warning, because it is a concentrated feed of
  attacker-controllable Slack/GitHub/email text handed to a client that also
  holds lifecycle tools. **`bobi_lifecycle` requires a `reason`**, recorded on
  the command as an audit control; self-targeting a restart is allowed and
  annotated rather than refused, so self-heal stays available.

  `issueAdminCommand` moved out of the `/fleet/*` route body into `fleet.ts` and
  now backs both surfaces, so the deliver-**before**-record ordering exists once
  rather than twice: a recorded-but-undelivered command is a pending row that can
  never resolve. `MCP_SERVER_VERSION` is `1.1.0` — it versions the tool surface,
  and this release is additive to it.

### Fixed
- **Attribute injection in the agent-reply markdown renderer (#942).** The
  dashboard interpolated a link URL into `href="${safe}"` while escaping only
  `&`, `<`, `>`, so agent output could close the attribute and land a live event
  handler — and agent replies are prompt-injectable from any page, email, or
  tool result the agent reads. Quotes are now escaped before any transform runs,
  closing the sink for every attribute at once. The renderer moved to its own
  module so the fix is provable: the suite executes it under Node and parses the
  output at DOM level rather than grepping the source.
- **Unvalidated register payload on the unauthenticated mint path (#942).**
  `handleRegisterDeployment` cast `subscriptions` to `string[]` and checked only
  truthiness, so a bare string registered one subscription per character and a
  non-string element threw *after* an orphan bubble had been persisted. Shape is
  now validated before anything is minted or written.
- **Unbounded request-body buffering (#942).** `readBody` concatenated every
  chunk with no cap, and the existing gate could only run against an
  already-fully-read body — so the 413 arrived after the memory was spent. On a
  non-loopback bind the webhook route reads the body before signature
  verification, making the peak unauthenticated. The reader now rejects on the
  chunk that crosses the cap (8 MiB, `BOBI_ES_MAX_BODY_BYTES`) and answers 413.
- **Slack file uploads in DMs and thread replies never arrived (#945).** Slack
  stamps the `file_share` subtype on every message that shares a file, and the
  bridge skipped every subtyped message before classification — so those never
  became events at all. Channel @mentions survived only because `app_mention`
  carries no subtype, and the existing test passed only because its synthetic
  payload omitted what real Slack always sends.
- **The circuit breaker's pause buffer was unbounded and never released
  (#945).** It grew by a full event per event for the whole cooldown with no cap,
  and resume is lazy — a conversation that tripped and then went quiet held its
  state and buffer for the life of the process, contradicting the module's own
  "buffered, not dropped" and "auto-resume" claims. Now capped at 50 (newest
  wins, drops counted) and swept on the local server's existing eviction
  interval.
- **`/channels/send` accepted required text and delivered it nowhere (#945).**
  The file + `edit_ref` path performed its placeholder edit only where the
  channel supports editing, and uploaded files with no comment on either branch —
  so on WhatsApp the user got the PDF and never the sentence explaining it. The
  text now rides as the file comment where there is no placeholder to move it
  into.
- **Concurrent Slack workspace registrations raced away a bot's signing secret
  (#945).** The record is updated read-merge-write across an `await`, so two bots
  registering the same workspace — a fleet roll restarting both agents — each
  merged onto a stale snapshot and the second put dropped the first app's entry,
  401ing its inbound events. Writes are now serialized per storage key. This
  closes the window within one runtime instance, which is where the reported
  failure lives; a cross-isolate race on the Worker's KV backing needs a
  storage-layer change and is documented rather than claimed fixed.

### Changed
- **The release workflow publishes the pins the image dispatch needs (#943).**
  `release-image.yml` requires the exact claude CLI version the release resolved
  from the floating `stable` channel, but since the repo reorg that value existed
  only inside a finished job's shell variable — so dispatching the image meant
  re-deriving the channel by hand and hoping it had not moved, defeating the
  point of resolving it once. It is now published as a ready-to-paste command in
  the run summary and as a `::notice::` that outlives summary retention.
- **Event-server reuse sweep (#945).** Eight duplicated or dead surfaces
  converged: the doubled `ingest/` topic spelling, an impossible `skip` state on
  the Slack normalizer, three hand-rolled Slack Web API calls, three near-identical
  copies of the deliver/buffer/broadcast block, an eviction path re-implementing
  the deregistration bodies defined above it, the attachment→files extraction
  written twice across adapters, and two identical unreachable guards.

## 0.53.0 - 2026-08-03

Minor release: the fleet control plane grows an agent-shaped surface, and Claude
token telemetry stops reading zero.

### Added
- **The fleet control plane is served as MCP from the event-server Worker
  (#923).** `POST /mcp` on the Worker exposes the same control plane the hosted
  console already drives, so an agent binds to named tools with declared schemas
  instead of re-deriving the `/fleet/*` URL shapes, the 202-then-poll contract,
  and the per-command argument shapes. The read half only —
  `bobi_fleet_status`, `bobi_instance_detail`, `bobi_command_result` — each
  calling **in-process** the same builder its HTTP route calls, so there is no
  second implementation of the read model and no HTTP hop. Stateless: one
  `McpServer` per request, no Durable Object, no session id. Auth is the
  existing `FLEET_OPERATOR_TOKEN` via `requireOperator`, checked in the route
  before any tool body runs — **the MCP route widens the interface, not the
  authority.** Nothing here can restart, stop, or message an instance; write
  tools are Lane B of `plans/2026-07-30-mcp-fleet-control.md`. A team consumes
  the endpoint with no framework change, via `mcp_servers` `type: http` plus
  `headers`.

  **Self-hosters redeploying their own Worker: `wrangler.jsonc` now sets
  `compatibility_flags: ["nodejs_compat"]`, and it is not optional.**
  `createMcpHandler` carries its per-request context in an `AsyncLocalStorage`,
  so the bundle imports `node:async_hooks`; without the flag workerd refuses to
  start and **every route on the script goes down with it**, bus included. The
  compatibility date is past 2024-09-23, so the flag is additive rather than
  replacing workerd's own APIs. Conformance is asserted by replaying the literal
  request bodies a real `claude-code/2.1.220` session sent, and the deployed
  `/mcp` route is exercised for real by `worker-deploy-smoke.yml` — the unit
  pool injects its own Node flags, so a pool test alone could never have proven
  this one.
- **`bobi agent <name> costs backfill` recovers lost token telemetry (#935,
  #936).** Reads Claude's retained JSONL transcripts and fills the token
  counters the defect below left at zero. Dry run by default, `--write` to
  apply, idempotent across runs. It only ever **fills**: recorded tokens and
  provider dollars are never overwritten, `last_activity` is never bumped, and a
  session whose transcript is gone stays honestly unknown rather than estimated
  into place. Against a real 151-session registry it repaired 112 with provider
  dollars byte-identical before and after. Four hazards are handled that only
  running it surfaced — assistant lines repeat per content block under one
  message id (naive summing overcounts ~2x), the `<synthetic>` pseudo-model
  never converges, live sessions would double-count against the recorder, and
  recovered usage must land on the *existing* model key rather than splitting
  one model across two rows. `costs` became a command group; the bare `costs`
  invocation is unchanged.

### Fixed
- **Claude token counts were recorded as zero while the dollars arrived (#935,
  #936).** `claude_agent_sdk` 0.2.128 passes the CLI's `modelUsage` through
  verbatim, so its keys are **camelCase**, but `_one_model_usage_to_cost` read
  only the legacy snake_case spellings — every token field came back `0` while
  `total_cost_usd` arrived on a separate attribute, producing exactly the
  reported shape of rows with dollars and no tokens. Each field is now read
  across both spellings, first match wins, so a payload carrying both is read
  once rather than summed. `AssistantMessage.usage` is deliberately untouched:
  it is the raw Anthropic API shape, which is genuinely snake_case.

### Changed
- **Documentation re-verified against the tree it describes (#937, #938,
  #939).** The review-remediation plan re-checked all 229 items against `main`
  and its Lane D swept 24 of them: repo-root and pack docs, stale specs, and the
  engine, monitor, event-server, and setup-skill references that had drifted
  from the code.

## 0.52.0 - 2026-08-03

Minor release: Bobi looks like Bobi, and public CI proves the product rather
than trusting it. This is also the first release published entirely from this
repo — the reference container image now builds and pushes from `bobi-agent`,
not from the archived private deploy repo, which completes the public half of
the 2026-07 reorg (`plans/2026-07-29-repo-reorg.md`).

### Added
- **Public CI proves both brains, against the real image (#909, #911).** CI
  verified none of the three things Bobi actually is. The `container-image` job
  in `container.yml` now runs the built image through one real **Claude** ask
  and one real **Codex** ask, against an ephemeral event server started from the
  real Worker sources. It is off by default — nightly, on `workflow_dispatch`,
  or on a PR carrying the `ci:live` label — and never on a fork PR.
- **A real `wrangler deploy` is exercised before yours is (#909, #911).**
  `wrangler dev` proves the Worker's code but never a deployment: the KV
  binding, the `v1` `new_sqlite_classes` Durable Object migration, and
  account-side provisioning were covered nowhere, and the only real
  `wrangler deploy` in existence went straight to production.
  `worker-deploy-smoke.yml` deploys a dedicated `bobi-events-ci-smoke` Worker in
  a **separate Cloudflare account** and runs a health check plus a
  publish→subscribe round-trip against the deployed URL. Cloudflare grants
  Workers and KV permissions at account scope only, so a CI-only account is the
  strongest isolation available; `scripts/render_worker_ci_config.py` derives
  the CI config from the shipped `wrangler.jsonc` so the migration and
  compatibility date cannot drift from what production deploys.
- **The live lanes must prove they RAN (#909, #911).** Every test in both lanes
  carries a `skipif`, so a renamed secret or an unavailable harness would have
  skipped to green — which is exactly how this repo shipped four
  green-but-vacuous lanes. Each lane now has a fail-fast step when a credential
  is empty plus `scripts/assert_junit_ran.py`, which reads the junit report and
  rejects any skip, any wrong count, and any missing named test.
  `tests/test_ci_live_wiring.py` asserts the wiring itself is still in place and
  fails if a live step is deleted.
- **The reference image is published from this repo (#898).** `Dockerfile`,
  `docker/`, `release-image.yml`, `container.yml`, and the container contract
  tests moved here from the private deploy repo, with a new
  `docs/REFERENCE_IMAGE.md` covering the run contract, the `--init` requirement,
  the runtime env contract, and the `TEAM_DEPS` bake hook. A self-hoster runs
  Bobi from a public pull instead of a repo grant.

### Changed
- **`bobi setup` and `bobi app` are reskinned onto the Bobi design system
  (#883).** Both local web UIs now look like the Bobi that ships on buildmoda.ai
  rather than the amber/CRT identity that predated the brand. The design system
  is vendored as `docs/design-system/` and is the source of truth for anything
  visual on any Bobi surface. Violet is **state** — live, enforced, gated,
  focused — never decoration; everything decorative is clay, and a connected
  integration reads violet like anything else live rather than green. Geist,
  Geist Mono, and Inter are vendored as woff2 subsets under
  `bobi/webui_common/static/fonts/`, so the offline constraint is intact: no
  CDN, no network at runtime, no build step. Both UIs are full-bleed — the
  simulated title bar, traffic lights, and address chip are gone. Legacy token
  names are kept as aliases and remapped by meaning, so nothing resolves to an
  off-brand value.
- **The supervisor arrives via the wheel, not a `COPY` (#898).** The container
  no longer carries `ARG BOBI_SUPERVISOR_SRC`, the supervisor `COPY`, or the
  `/usr/local/bin/bobi-supervisor` PYTHONPATH shim — since 0.51.0 the sidecar
  ships inside the wheel as `bobi.supervisor`. The entrypoint execs
  `bobi agent "${AGENT_NAME}" supervise -- --foreground`, and its misbuilt-image
  guard asks the CLI whether `supervise` is really present rather than testing
  for a binary that no longer exists.
- **The setup wizard's Cloud card points at something readers can obtain
  (#907).** It previously told new users to read a runbook from the private
  `bobi-deploy` distribution — an archived repo. It now points at
  `ghcr.io/moda-labs/bobi` and `docs/REFERENCE_IMAGE.md`. README Cloud
  Deployment and `docs/QUICKSTART.md` Option B were rewritten around the
  published multi-arch image for the same reason, and `skills/linear-setup.md`
  now sends readers to `event-server/worker/` in this repo (#905).
- **`docs/RELEASE_RUNBOOK.md` describes the two-repo train (#900).** It states
  the direction the reorg established — `moda-agents` consumes released
  `bobi-agent` versions, and this repo owns the gating that keeps its releases
  from breaking them — and says out loud that the fleet canary gates the fleet
  **roll**, not the image publish, because the canary builds `FROM` the
  published base and a gate cannot precede the artifact it consumes.

### Fixed
- **Subagent launch chains are bounded before a process is spawned (#849,
  #888).** Launches recorded no parent or ancestry, so a workflow step launching
  its own workflow was undetectable and unbounded: on 2026-07-25 one scheduled
  trigger became **50 runs in 44 minutes**, stopped only by the spend governor —
  whose own docstring calls it a classification-free backstop, i.e. the last
  resort. Here it was the only resort. Every launch now carries the ordered
  chain of runs that led to it in `BOBI_LAUNCH_LINEAGE`, and a launch is refused
  up front when it is self-recursive (a named workflow already in its own chain;
  `adhoc` is exempt, since `-w adhoc --wait` delegation is ordinary work and
  depth governs it) or deeper than `max_launch_depth` (default 8, settable in
  `agent.yaml` or per deployment via `BOBI_MAX_LAUNCH_DEPTH`). The refusal
  message tells the agent this is a deterministic block rather than a rate
  limit, so retrying with a new run key or after waiting is not a workaround.
- **Two supervisor test files that tested a private copy now test the shipped
  code (#904).** The reorg moved the sidecar to `bobi/supervisor/` and brought
  six of its eight test files with it; `test_supervisor_alerting.py` and
  `test_supervision_operator.py` stayed behind, exercising a second
  implementation that would stay green while the real one moved — the same
  failure mode that left entrypoint tests asserting a `bobi-supervisor` shim
  that had not existed for a release. The port is a pure import rewrite and all
  23 pass unchanged, which is itself the evidence the copies never diverged.
- **Three integration tests failed on ambient state rather than on the code
  (#908).** A module-scoped fixture unset the session's `BOBI_HOME`, letting
  tests reach the real `~/.bobi` — it failed deterministically after any
  dual-brain module and passed deterministically alone, which reads as
  flakiness but is shared state.

## 0.51.1 - 2026-07-31

Patch release: an agent no longer needs a build-time secret in order to run.

### Fixed
- **Build-time-only `${VAR}` refs stop gating a runtime (#886).** A variable
  referenced only by a `build:` step was classified a required *runtime* secret,
  so `bobi agents install --non-interactive` refused to install an agent whose
  dependency was already baked into its image. It took a team down during the
  0.51.0 fleet roll: the deploy side deliberately withholds build secrets from
  the runtime env-file (and enforces them host-side before a build instead),
  while this side refused to proceed without one — and because the deploy pauses
  the old runtime before pushing, the box was left frozen rather than merely
  un-updated. The dependency the secret would have installed was already
  present, and its `success` check passed.

  The fix is structural rather than a name list: a ref found only under
  top-level `build:` — the `apt`/`npm`/`run_root`/`run` image layer — is marked
  `build_only` and excluded from the install and startup gates. A name used both
  under `build:` and anywhere else stays required, because the runtime use is
  real. An unparseable `agent.yaml` yields no build-only names at all, so a
  classification bug over-requires a secret rather than quietly ceasing to
  require one. `docs/TOOL_LIBRARY.md` now states the rule where dependency
  authors will meet it.

## 0.51.0 - 2026-07-31

Minor release: the product surface goes public. The Cloudflare event-server
Worker and the admin sidecar were previously reachable only through a grant on
the private deploy repo; both now ship in this repo, and the admin wire format
they speak is a documented, versioned contract rather than an internal
agreement between two halves of one team's infrastructure. Self-hosting the
durable event tier no longer requires anything private. This is Lane 1 of the
repo reorg (`plans/2026-07-29-repo-reorg.md`, Phases 1-5); the reference
container image and the ops-repo consolidation follow in later releases.

### Added
- **The Cloudflare Worker event server is public (#880).** `event-server/worker/`
  is a third npm workspace package alongside `src/` (local) and `core/`. It is
  the durable variant of the same protocol the Node servers speak - same webhook
  verification, same bubble model, same topics - with per-deployment sessions in
  Durable Objects and replay in KV, so registrations and cursors survive a
  restart. It lands as its own workspace rather than more files under
  `event-server/src/` because the two compile units are mutually exclusive: the
  local unit is Node-only (`lib es2024` + `@types/node`), the Worker needs
  Cloudflare's globals and `worker-configuration.d.ts`, and their vitest configs
  are node-environment vs. `defineWorkersConfig`. `wrangler.jsonc` ships with a
  placeholder KV namespace id that fails loudly at `wrangler deploy` - a fresh
  clone cannot silently deploy against someone else's namespace. Deployment
  walkthrough in `docs/SELF_HOSTED_EVENT_SERVER.md` ("Deploying the Worker").
  Requires a Workers Paid plan, because the Durable Objects are SQLite-backed.
- **`bobi agent <name> supervise` (#880).** The admin sidecar, restored as a
  first-class public CLI surface. It spawns and probes the manager, publishes
  heartbeat and lifecycle telemetry, and listens on the admin topic so a wedged
  manager can still be restarted from outside the box. Everything after `--`
  forwards to the manager's start command
  (`bobi agent <name> supervise -- --foreground`). This is what a container
  entrypoint runs as PID 1, not an interactive command. The agent name is a CLI
  positional; the private `bobi-supervisor` shim that read it from `BOBI_ROOT`
  is gone.
- **`docs/ADMIN_PROTOCOL.md` (#880).** The admin channel as a documented,
  versioned contract - topics, envelopes, command and telemetry schemas, and the
  auth model. This, not the code move, is what an external consumer binds to.
  `bobi/supervisor/admin.py` binds the two sides byte-for-byte, so the server
  half of a documented contract cannot stay private.

### Changed
- **Positioning: the Worker is a self-host option, not a paid tier (#880).**
  `README.md` and `docs/SELF_HOSTED_EVENT_SERVER.md` now present three
  self-hosted shapes - tunnel, standalone Node, Worker - and tell you to pick on
  durability rather than on price. The in-memory restart caveat carries an
  explicit pointer to the durable answer.
- **Packaging: neither the wheel nor the sdist carries the Worker sources, and
  now that is stated rather than incidental.** The wheel's event-server inputs
  are declared as `src/**/*.ts` + `core/src/**/*.ts` (`bobi/events/artifact.py`),
  which cannot match `worker/`. The sdist takes `event-server` as a whole
  directory, so the Worker workspace *was* in scope and fell out only by
  accident: Hatch's `safe_walk` follows the npm workspace symlink
  `node_modules/bobi-events-worker -> ../worker` and then prunes the real
  directory as an already-visited inode, which made the sdist's contents depend
  on whether `npm ci` had run (`core` escaped only by sorting ahead of
  `node_modules`). `event-server/worker` is now an explicit sdist `exclude`,
  pinned by `tests/test_sdist_contents.py`. No published bytes change - this is
  what the release workflow already emitted - but the result is now
  deterministic. Nothing in the Python distribution builds or runs the Worker;
  its deploy path is a git clone plus `wrangler deploy`.
- **The event-server npm tree audit is default-deny (#880).** Only `extraneous:`
  is treated as non-fatal in `bobi/events/server.py`; everything else blocks.
  npm 10.8.2 (bundled with Node 20) reports the Worker's optional dev deps as
  extraneous from a lockfile npm 11 calls clean, and the audit must not be
  loosened wholesale to absorb that.

### Removed
- **The repo-split bridges (#880).** The npm publish path for
  `@moda-labs/bobi-events-core` (`core/scripts/{pack,smoke}.mjs`) and the
  `worker-integration.yml` cross-repo CI existed only to carry code across the
  private/public boundary. With the Worker home, they dissolve.

### Fixed
- **`validate_team` no longer invalidates its own hash (#873).** Finalizing a
  team could land in a state where install was permanently unreachable -
  "the team source changed since validate_team last passed" - and re-running
  `validate_team` re-armed the same trap. When the setup's `run/` directory sits
  inside the source tree, saving `run/state/setup.json` rewrites a file the
  freshness hash just covered, so the frozen digest is stale the instant it is
  recorded. `source_tree_hash` gains an `exclude` set and both call sites pass
  the setup state file. Two guards close the way in: create mode now applies the
  same empty-target bar the registry branch enforced, and the `run/` containment
  check rejects a location that *encloses* `run/` (the agents root) and not only
  one nested inside it.
- **Plan-artifact CI reads heading-style phase markers (#885).** The check
  recognized list-style markers only, so a plan whose phases are headings passed
  vacuously.
- **The unit-test matrix reports under a stable required check (#877).** A
  skipped matrix job never expands its matrix, so required `Unit tests (3.12)`
  contexts never reported and the PR hung forever. A non-matrix gate job now
  checks the matrix result explicitly.
- **A plan's review surface freezes at approval, not before (#878).**

## 0.50.0 - 2026-07-30

Minor release: long agent jobs stop dying silently. A turn-cap kill now names
itself instead of surfacing as `turn failed`, the cap is configurable and
survivable rather than a hardcoded 200-turn wall, and a re-dispatched worker can
start clean instead of resuming a dead transcript. Ships the generic
checklist-execution worker protocol - the framework half of the checklist
execution model (#852), which replaces an execution engine with a committed
markdown checklist rather than adding one; the framework logic it required is
four lines.

### Added
- **Configurable, resumable turn budget (#845, PR #847).** `max_turns` resolves
  step override -> `roles.<role>.max_turns` -> `brain.max_turns` ->
  `DEFAULT_MAX_TURNS` (1000, up from a hardcoded 200), applied at every site
  that previously carried the literal (`spawn_adhoc`, `run_phase_blocking`,
  `_run_agent_supervised`, and the orchestrator session). A step override now
  actually wins on steps after the first - it previously no-opped silently while
  documented as working. Hitting the cap no longer throws away the run: the
  transcript survives, so the orchestrator restarts the step on the saved
  session id with a fresh budget (bounded by `MAX_TURN_BUDGET_RESUMES`, 3),
  reusing the existing native-resume path that already preserves conversational
  continuity across a model/effort change. The final continuation tells the
  agent to write its handoff now.
- **`skills/checklist-execution.md` - the generic worker protocol (#852, PR
  #865).** How an agent works a long job from a committed markdown checklist:
  read once at session start, then one item at a time, verify, commit per item.
  Persist-per-item is the durability primitive - there is no engine, no run
  record, and no framework module behind it (pinned by
  `tests/test_no_checklist_engine.py`). A `verify:` line is a proposed proof for
  a human or an agent to run, never something the framework executes; the
  security model and its standing tripwire are in `docs/SECURITY.md`.
- **`--fresh` on agent dispatch (#852, PR #865).** Threaded through `Session` ->
  `spawn_adhoc` -> `launch_agent` -> `run_workflow`, so a human can re-dispatch
  a wedged worker without inheriting its dead transcript. The default is
  unchanged: resuming a failed or stale run stays the documented retry contract.
- **Plan-artifact CI check (#852, PR #865).** `.github/scripts/check-plan-artifact.sh`
  plus its own workflow (deliberately not a job in `ci.yml`, whose docs/plans
  skip gate would render it a silently-passing required check on exactly the
  PRs it exists to check). It asserts the review-surface freeze, an append-only
  appendix, `[f]` state tags, and gate-line classification - and never executes
  a `verify:`, pinned by a canary test.

### Fixed
- **Honest turn errors (#845, PR #847).** `turn failed` was a literal fallback:
  the brain had already diagnosed `max_turns_reached (max=..., turns=...)` into
  `TurnResult.error_message`, but the workflow drain read only `result_text` -
  empty on exactly that path - and substituted the literal, discarding the cause
  of two real engineer-session deaths. One shared composition
  (`bobi.brain.turn_error_text`) now backs the drain, `Session.last_error()` and
  `_run_agent_supervised`, and the last resort names the error kind and API
  status rather than `unknown error`. The session log's `stop` record carries
  every terminal fact the brain reported, so no future diagnosis needs the
  vendor CLI's transcript.
- **A re-dispatch no longer collides by construction (#852, PR #865).**
  `spawn_adhoc` derives its session name from `sha256(task)[:8]`, so
  re-dispatching an identical task string - the checklist shape exactly, where
  the task is a stable pointer to an artifact - reused the previous session.
  Varying the name is not the fix: `orchestrator._setup_worktree` sets
  `branch = session_name`, so it would fork a new git branch per dispatch and
  break launch-admission dedupe and image rotation. The name stays stable and
  the resume became optional (`--fresh`, above).

### Changed
- **Engineer role prompt and `--wait` docs (#845, PR #847).** The engineer role
  gains "Parallel Work and Your Turn Budget": launch fan-out units backgrounded
  and join them with a bare `wait` in the same Bash invocation, so a fan-out
  costs two turns instead of one per poll - one observed session spent 79 of its
  201 turns polling `tail -1`. `--wait`'s adhoc-only limit is now documented
  where it is hit (flag help, runtime error, `skills/bobi.md`, role prompt)
  rather than advertised as unqualified. eng-team pack 1.5.0 -> 1.5.1.

## 0.49.0 - 2026-07-24

Minor release: the local event server now ships as a prebuilt immutable
bundle so installed startup no longer tries to `npm install`/build inside
read-only `site-packages` (fixing the EACCES crash loop), plus two
config/dispatch bug fixes and a CI velocity change.

### Added
- **Immutable prebuilt local event-server artifact (#798, PR #841).** Frozen
  wheels previously shipped event-server source without `dist/local.js` or
  runtime modules, so installed startup attempted an `npm install` and a
  TypeScript build inside read-only `site-packages` — the reported EACCES
  crash loop. The build now compiles and audits a single immutable local
  event-server bundle in external staging and ships it (with its input
  manifest and third-party license notices) in both sdists and wheels.
  Installed startup validates and executes the bundle with no npm and no
  writes; writable source checkouts still rebuild as before. Running the
  local event server now requires **Node.js 20+**, with actionable CLI,
  `bobi doctor`, installer, and documentation diagnostics; remote-server
  operation needs no local Node. Direct and sdist-derived wheels are
  byte-identical, and CI/release now cover the exact wheel boundary, hostile
  inherited Node environments, WebSocket delivery, archive purity, and
  frozen-package immutability.

### Fixed
- **Per-rule auto-dispatch roles (#796, PR #830).** `AutoDispatchRule` gains
  an optional per-rule `role`. Omitted, empty, and YAML-null roles normalize
  to `""` (a roleless launch, so workflow `step.agent` resolution stays
  authoritative); an explicit non-empty role passes through unchanged. This
  restores parity with `subagents launch` and removes the reactor's
  team-specific `engineer` fallback.
- **Workflow templates preserved during env interpolation (#797, PR #831).**
  The shared `${VAR}` environment-reference matcher treated the `${` prefix
  of a `${{...}}` workflow placeholder as an environment reference, so
  install-time scanning reported bogus required secrets and runtime
  interpolation reduced the template to `}` before the workflow engine saw
  it. A negative lookahead now excludes `${{...}}` placeholders from the
  matcher while still resolving real env references (including nested
  `${{ ${VAR} }}`).

### Changed
- **CI skips the heavy test matrix on docs/plans-only PRs (#838, PR #839).** A
  cheap `changes` gate job diffs the PR base against HEAD; PRs whose changed
  files are confined to `docs/`/`plans/` skip the five heavy jobs (a *skipped*
  required check counts as passing) and stay mergeable in seconds, while any
  PR touching a file outside those trees runs the full suite unchanged.
  push/schedule/dispatch always run the full suite, so the dev channel and
  nightly runs are unaffected.

## 0.48.0 - 2026-07-22

Minor release: Slack gains an opt-in Socket Mode transport for local
deployments (no public request URL required), workflow resume survives the
dead-man reconciler after long suspensions, dead-transport turns become
honest failures that replay instead of silently completing, session rotation
stays responsive under load with unacked-event replay, and session state
publishes atomically.

### Added
- **Slack Socket Mode transport, opt-in (#808/#809, PRs #811/#812).** The
  local event server can now receive Slack events over Socket Mode instead
  of a public webhook URL. Lane A adds the sans-I/O Socket Mode session core
  (exact acknowledgement frames, bounded cross-connection deduplication,
  event normalization, explicit reconnect/fatal policy) and the local driver
  (signed-only `app_token` activation on `POST /slack/workspaces`, REST
  bootstrap timeouts, fresh reconnect URLs, hello/staleness watchdogs,
  bounded delivery concurrency, secret-safe `slack_socket` health entries),
  reusing the existing Slack delivery and bot-filtering pipeline. Lane B adds
  the operator surface: an optional `SLACK_APP_TOKEN` in setup/config
  (forwarded only in bubble-signed registrations after the target reports
  local runtime mode), `bobi create-slack-bot --socket-mode` (no-flag
  rendering unchanged), app-specific Socket Mode doctor diagnostics with
  transport-aware ingress checks, `xapp-` secret redaction, and setup docs.

### Fixed
- **Workflow resume no longer killed by the dead-man reconciler (#826, PR
  #827).** `resume_workflow` only flipped registry `status`/`phase`, leaving
  the dead launch process's `pid`, `started_at`, and `timeout` in place - so
  after a long `await:` suspension the reconciler judged the healthy resumed
  run against the dead pid (phantom TERMINAL_CRASHED + `agent/session.failed`)
  or the expired launch deadline (cancelled + TERMINAL_FAILED). Resume now
  re-stamps `pid`/`started_at`/`timeout` in the same atomic registry update
  that flips the status to `running`. Operator note: the resumed leg runs
  under the resume `--timeout` (CLI default 3600) - the launch value is not
  persisted, so long converge legs need it passed again explicitly.
- **Dead-transport turns are honest failures (review-remediation D001+D002,
  PR #825).** A turn whose brain transport died mid-drain (subprocess killed,
  broken pipe) set the session to `error` but still read as a clean turn:
  the triggering message was acked (lost on restart instead of replayed,
  violating the #688 invariant) and a crashed phase persisted
  TERMINAL_COMPLETED and announced `agent/session.completed` - the exact
  signal a headless orchestrator reads as lane-done. The dead-transport
  branch now marks the turn failed, skips the ack so the event server
  replays the message after supervisor restart, and a crashed phase comes
  back `success=False` + TERMINAL_FAILED + `agent/session.failed`.
- **Session rotation stays responsive and unacked events replay (#799, PR
  #800).** Rotation called `receive_response()` on Claude's promptless SDK
  connect (which emits no model turn), wedging the sole inbox coroutine for
  the 240-second reconnect bound and queueing mentions behind it; rotation
  preparation is now blue-green (the old healthy client serves inbox work
  while the candidate connects in the background). The local event server
  now replays buffered events from `last_seen=0`, so an unacknowledged first
  event survives restart; the event cursor advances only after a successful
  terminal model result; disconnect gets a 10-second hard bound plus a
  synchronous `abort()` escape hatch on the brain contract; a cursor ACK
  exceeding its bound marks the session terminally errored for supervisor
  recovery instead of wedging the inbox.
- **Session state publishes atomically (PR #810).** Each session
  `state.json` now writes through a complete sibling temp file and atomic
  `os.replace`, and `register`/`update`/`record_cost` serialize through one
  persistent per-session file lock - closing the truncation window that made
  readers see a zero-byte file mid-write (CI `JSONDecodeError`) and a
  pre-existing lost-update race between status and cost writers.

### Changed
- **eng-team spec phase exempts plan-born specs from the scope lens
  (eng-team 1.3.0 -> 1.4.0, #815, PR #824).** Plan-born work's scope is
  settled by the plan's approval merge; the engineer's spec-phase scope lens
  ("too narrow? too wide?") no longer re-litigates it. Append-only edits at
  the two scope-lens sites in the engineer ROLE.md; standalone-ticket
  behavior unchanged.


## 0.47.0 - 2026-07-21

Minor release: the eng-team package's spec phase becomes plan-artifact-aware,
the gstack tool-library entry narrows to a prefixed browser-QA toolbox, Codex
`max` reasoning effort validates cleanly, and the repo's development
lifecycle docs are rebuilt around plan artifacts and repo-anchored
conventions.

### Changed
- **eng-team spec phase is plan-artifact-aware (eng-team 1.2.0 -> 1.3.0, PR
  #804).** The engineer's Spec Phase detects plan-born tickets (body
  references a `plans/<slug>.md` artifact, or the title's bracket prefix
  matches an existing plan file), reads the plan on `main` as the design
  source of truth, and specs only the ticket's slice without re-deriving
  recorded plan decisions. Plan-born issues link the plan artifact instead of
  duplicating it; plan changes land as dated amendments in the implementing
  PR. The `plan_review` workflow step consumes an optional `plan_path`
  handoff field so the adversarial reviewer checks the spec against the plan
  rather than re-litigating it.
- **gstack catalog becomes a prefixed browser-QA toolbox (PRs #804, #805).**
  The gstack tool-library entry installs with `--host auto --prefix -q`
  (skills land namespaced as `gstack-*` for every agent CLI present, not just
  Claude), and a post-setup step removes the six displaced lifecycle skills -
  `gstack-ship`, `gstack-review`, `gstack-land-and-deploy`,
  `gstack-landing-report`, `gstack-codex`, and `gstack-upgrade` (#805, which
  would resurrect the others by re-running gstack's setup) - from both the
  Claude and codex skill dirs. The `success:` contract enforces the prefixed
  core pair present and the six removed skills absent (including dangling
  symlinks), and `fix:` is idempotent so doctor can repair a resurrected
  install. The engineering lifecycle on a team box is owned by the team's own
  stage skills; gstack is browser-QA only.
- **Development lifecycle docs rebuilt (PRs #793, #801, #803, #807).**
  Initiative-sized work now designs in a `plans/<slug>.md` artifact merged
  via PR with a lightweight tracking issue; split tickets are thin dispatch
  pointers into the plan (`docs/TICKETING_POLICY.md`). The CLAUDE.md
  lifecycle section carries repo-anchored conventions only (plan markers and
  amendments, design-in-issue for single-unit work, e2e verification with
  docs in the same PR, deliberate per-PR landing, handoff continuity). The
  release runbook's Slack E2E validation message doubles as a human-readable
  release announcement (#793).

### Fixed
- **Codex `max` reasoning effort validates cleanly (#794, PR #795).** The
  Codex engine's declared effort capabilities now include `max`, so
  validation and `bobi doctor` stop warning for a value the runtime already
  passed through; the live effort test asserts the spawned
  `-c model_reasoning_effort=...` argv pair via Bobi's real runner seam
  instead of a private Codex rollout field.


## 0.46.0 - 2026-07-16

Minor release: brain configuration is restructured around engines (gateway
mode becomes endpoint config, fixing silently dropped reasoning effort on
codex gateways), codex gateways move to the Responses wire API, the sleep
cycle gains cold-memory recall, and reliability fixes land for curator
retries, worker concurrency accounting, and workflow notify steps.

### Changed
- **`brain.kind` names the engine; gateway mode is `base_url` (#789, PR
  #790).** `kind: claude|codex` selects the CLI engine, and setting
  `brain.base_url` points that engine at a gateway endpoint (`small_model`
  for claude engines, `wire_api` for codex engines). The old
  `kind: gateway`/`gateway-openai` spellings remain accepted aliases (doctor
  suggests the new form), ambient `BOBI_BRAIN` pins and
  `GatewayBrain`/`GatewayOpenAIBrain` imports keep resolving, and on-disk
  session provenance records keep matching, so no session continuity is lost
  on upgrade. This structurally fixes #778 reasoning effort being silently
  dropped on `gateway-openai` teams (the old subclass re-implemented session
  construction and missed the effort plumbing) and a latent miss where
  gateway-openai teams never rendered their shipped instructions to
  `$CODEX_HOME/AGENTS.md`. Doctor/validate now check gateway teams' `effort:`
  against the engine's real vocabulary instead of a cross-vendor union.
  Behavior changes: `effort:` on a codex-gateway team now reaches the backend
  (remove the key to restore the old accidental behavior); an operator
  `BOBI_BRAIN=claude` override on a `kind: gateway` team no longer forces a
  native run (same engine now - remove `base_url` to run natively). A
  declared gateway whose `base_url` `${VAR}` resolves empty now fails loud:
  child spawns refuse, and process startup pins an RFC 2606 `.invalid`
  sentinel so `doctor`/`stop`/`status` keep working while sessions raise the
  actionable config error instead of dialing the real vendor with gateway
  credentials.
- **Codex gateways default to `wire_api: responses` (#791, PR #792).** Newer
  codex builds reject `wire_api = "chat"` at config load (openai/codex#7782),
  so the default flips to `responses` and doctor warns (never blocks) on an
  explicit `chat` - front chat-only OpenAI-compatible gateways with LiteLLM's
  Responses translation, or use `kind: claude` + `base_url` for
  Anthropic-compatible ones. The knob stays pass-through for pinned older
  codex builds.

### Added
- **Cold-memory recall (#773, PR #780).** The sleep cycle indexes
  `workspace/memory/reference.md` into a team-scoped `long_term_memory`
  knowledge base (exact + semantic dedup with provenance, stale/partial index
  repair), and a read-only `bobi agent <name> recall-memory` retrieves over
  it; cold recall is documented in the base prompt.

### Fixed
- **Curator max-turn failures surface instead of retrying forever (#770, PR
  #772).** Claude max-turn terminal details are normalized from the raw
  session JSONL when the SDK result lacks them, and the structured error kind
  is preserved on `AgentResult` so curator/check/gate retry logic stops
  retrying deterministic max-turn failures.
- **Entry-role workers count toward the concurrency cap (#785, PR #786).**
  Only the persistent coordinator (by resolved session name) is excluded from
  the cap; ordinary workers launched with the entry role now count, and
  launch admission reuses the same predicate so both paths agree.
- **Undeliverable notify fails the run before an await (#787, PR #788).**
  Workflow notify steps return an explicit delivery outcome, emit
  `notify.undeliverable` when Slack delivery cannot resolve a target or
  token, and fail the workflow before arming an immediately following
  `await` - runs no longer park forever waiting for a reply to a message that
  was never sent.


## 0.45.0 - 2026-07-15

Minor release: reasoning-effort selection lands as the sibling of per-role
model selection, teams can ship global engineering instructions that render
brain-natively at boot (and eng-team now ships the house rules), and a new
`gateway-openai` brain kind targets OpenAI-compatible gateways.

### Added
- **Reasoning-effort selection (#778, PR #782).** Agents choose model + effort
  per delegation with the same precedence shape as #617 model selection:
  `--effort` launch flag > workflow step `effort:` > `roles.<role>.effort` >
  `brain.effort` (`BOBI_BRAIN_EFFORT`) > provider default. Values are
  pass-through and brain-native: codex renders
  `-c model_reasoning_effort=<v>` (works on `exec` and `exec resume`), claude
  passes `ClaudeAgentOptions(effort=...)`. Brains declare accepted efforts on
  `BrainCapabilities.efforts`, and doctor/agent-start validation warns (never
  blocks) on config, role, and workflow-step values the configured brain does
  not accept — the only early signal for claude brains, whose CLI
  warns-and-ignores invalid efforts. Effort is exempt from the cross-model
  resume guard: an effort-only step change reconnects the same session
  natively. `--effort` under `--as-check` is rejected with a clear error
  (previously `--model` was silently ignored there too; now both error).
- **Team-shipped global instructions (#779, PR #783).** A team package ships a
  root-level `AGENTS.md` (frozen to `run/package/AGENTS.md`), and at process
  bootstrap it renders into every path the active brain auto-loads:
  `~/AGENTS.md` always, `$CODEX_HOME/AGENTS.md` for codex,
  `$CLAUDE_CONFIG_DIR/CLAUDE.md` for claude/gateway. Rendering uses a
  sentinel-delimited managed block — foreign content (including Claude's own
  `#`-memory writes) survives verbatim, writes are atomic and idempotent, a
  team dropping the file removes its block, and a brain-kind switch cleans the
  previous brain's target. Compose semantics are per-file REPLACE: the last
  layer shipping `AGENTS.md` wins wholesale; an overlay ships an empty file to
  neutralize inherited rules.
- **eng-team ships the house engineering rules (#779, PR #784).** The general
  engineering standards (bug-fix discipline, testing standards, proof-of-work
  requirements, writing style, commit/release rules) move from the operator's
  unversioned `~/AGENTS.md` into `agents/eng-team/AGENTS.md`, so every
  `from: eng-team` overlay inherits them under version pins. eng-team
  `1.1.4` -> `1.2.0`.
- **`gateway-openai` brain (#777, PR #781).** New `brain.kind: gateway-openai`
  backed by Codex CLI provider overrides, for OpenAI-compatible gateways.
  Threads the gateway base URL and wire-API pins through config/env/process
  setup with validation for a missing base URL or invalid wire API, and
  updates codex tool-library verification, subscription bootstrap, and chat
  history dispatch for the new kind.

## 0.44.1 - 2026-07-14

Patch release: unblock the 0.44.0 fleet rollout. The 0.44.0 private release
train failed at the canary gate because the new runtime write guard crashed
the manager session on hosted containers; 0.44.0 was never rolled to the
fleet, so this is the version deployments should adopt.

### Fixed
- **Runtime write guard tolerates unowned files (#774).** #752's guard chmods
  Bobi-owned install roots read-only before brain sessions, tolerating only
  missing files. Hosted containers bake the venv as root while the runtime
  runs unprivileged, so the first `os.chmod` raised `PermissionError` and
  killed the manager session at startup (caught by the release canary; 0.44.0
  never reached the fleet). The read-only sweep now records unchmoddable paths
  in `GuardReport.skipped`, logs a warning, and continues — such files are
  unwritable to the runtime uid anyway. The mutable (+w) sweep stays strict so
  an install can never open a destructive mutation window over a tree it
  cannot fully unlock. Follow-up #775 tracks the `doctor` check's ownership
  awareness on hosted boxes.

## 0.44.0 - 2026-07-14

Minor release: Discord lands as a first-class channel, the #733 fleet
observability epic completes in the webapp (health badges, session log,
needs-attention panel), codex-brained teams get honest spend estimates instead
of $0, and a batch of sleep-cycle, history, and runtime hardening fixes.

### Added
- **Discord channel v1 (#2).** Inbound over a persistent Gateway WebSocket held
  by the local event server, outbound over plain REST through the existing
  channel gateway. Local runtime only; the remote/Durable-Object driver and
  polish are follow-ups. Setup guide at `skills/discord-setup.md`.
- **System-health badges + per-team health/lifecycle panel (#733).** New
  `TeamRuntime.health_summary(name)` seam and `GET /api/agents/{name}/health`
  route. The webapp shows reachability, the manager verdict
  (idle/running/stopped/starting), session roster, restart history, and the
  48h lifecycle trail — hosted data the `/fleet` read model already carried,
  now rendered, with a local-runtime fold from this machine's files.
- **First-class session log with honest terminal outcomes (#733).** New
  `TeamRuntime.session_log(name)` seam and
  `GET /api/agents/{name}/sessions` route over
  `SessionRegistry.list_all(reap_dead=True)`, so a history render never shows
  a dead session as running. The webapp gains a session-log panel with outcome
  counts, terminal rows (status, age, role, cost, error line), and read-only
  transcript drill-in; the composer hides for ended sessions.
- **Needs-attention panel surfacing harness errors (#733).** The agent view
  merges trouble lifecycle events (probe failures, manager restarts, budget
  exhaustion) with recent failed/crashed sessions into one alarm surface,
  hidden when all is well. Closes out the #733 epic.
- **Honest codex spend: fold-time dollar estimates + token volume (#760).**
  Codex-brained teams showed $0 everywhere because codex reports tokens, never
  dollars. `BrainCost` now records the cached/uncached input split as raw
  facts (fixing a double-count of cached input), and `rollup_costs` prices
  token-only entries against a per-model table at fold time. Estimates render
  as `~$Y est`, always distinguishable from recorded dollars; unpriceable
  models show token volume instead.
- **Dev pre-release channel (#740 Track A).** Every fully-green push to `main`
  fast-forwards the `dev` branch, which `bobi-deploy` CI/staging track — so
  private-side work no longer waits on a formal public release. Production
  cuts still pin exact PyPI versions via this runbook.

### Fixed
- **Sleep-cycle memory cap enforced (#747).** The 24k long-term-memory cap is
  enforced deterministically during artifact handling with section-aware
  compaction (`## Decisions` preserved first) instead of silent truncation
  later.
- **Sleep-cycle working budget (#765).** A 16k working budget below the 24k
  hard cap dispatches compaction early, with a lossless demotion tier at
  `workspace/memory/reference.md` treated as a checked artifact.
- **Sleep-cycle prompt echoes filtered from history (#763).** Curator/harness
  transcripts and framework startup re-injections are excluded from history
  indexing, and already-indexed echoes are filtered out of `messages_since()`,
  so agents stop re-reading their own boilerplate.
- **History FTS delete trigger repaired (#764).** The `messages_ad` trigger
  used the FTS5 special-delete form against a regular table; it now performs a
  normal row delete and legacy databases are repaired on open.
- **Runtime installs guarded from agent writes (#751).** Installed
  `run/package/` images are read-only outside Bobi-owned mutation windows via
  the new `bobi.runtime_guard` policy, with doctor checks for writable roots
  and wheel `RECORD` drift.
- **`subagents launch --wait` blocks on agents (#753).** The public `--wait`
  flag now uses the real blocking-agent path (`--as-check` covers the
  monitoring harness), and Claude max-turn terminals surface as actionable
  `max_turns_reached` errors instead of `unknown error`.
- **Monitor checks start fresh (#750).** `run_check_blocking()` passes
  `fresh=True` so checks stop resuming stale transcripts.
- **Workflow boolean conditions normalized (#758).** Capitalized `True`/`False`
  are parsed as boolean literals in workflow conditions (with token-boundary
  care so `TrueValue` stays a bare word).
- **Setup picker stale selection (#745-adjacent flake).** The folder picker
  blocks selection while an async browse navigation is pending, so a quick
  click can no longer submit the parent folder instead of the selected child.

## 0.43.0 - 2026-07-10

Minor release: fleet spend observability in the hosted webapp, headless
record-to-GIF tooling baked into the gstack catalog, an env-configurable allowed
Host for the hosted webapp, and a Codex-brain transcript fix for the fleet chat
panel. The spend panels ship the `TeamRuntime.spend_summary` / `fleet_spend`
read-model seam that the private hosted spend companion pins against.

### Added
- **Fleet + per-team spend observability (#734).** The webapp dashboard shows a
  fleet-wide cumulative spend total plus per-team tiles, and the agent view
  gains a spend panel with per-subagent cost. `TeamRuntime` gains
  `spend_summary` and `fleet_spend`; `LocalRuntime` folds each session's
  `state.json` via `costs.rollup_costs`, and `CostSummary.to_dict` defines the
  wire shape once for both runtimes. Figures are lifetime-cumulative and labeled
  as such (they reset only on a state wipe, so deployed teams need `state/` on a
  persistent volume or spend zeroes on redeploy).
- **ffmpeg baked into the gstack tool-library entry (#738).** The `gstack`
  catalog entry now bakes `ffmpeg` alongside Playwright/Chromium behind a
  success-gate check, so agent teams declaring `tool_library: [gstack]` get
  headless record-to-GIF on their next rebuild.
- **Env-configurable allowed Host for the hosted webapp (#732).** The shared
  web-UI security middleware admitted loopback only, 403ing any non-loopback
  Host. `install_security` now also admits bare hostnames named in
  `BOBI_WEBUI_ALLOWED_HOSTS` (comma-separated, read once at install time), so
  the hosted fleet-admin webapp can serve the same build behind its auth gate
  and reverse proxy. Unset in the local product, so loopback-only behavior is
  unchanged.

### Fixed
- **Codex-brain transcripts render in the fleet chat panel (#737).** The fleet
  UI showed no chat history for codex-brain agents because the transcript reader
  only understood Claude Code JSONL. `read_transcript_messages` is now
  brain-aware: it reads a Codex "rollout" under `$CODEX_HOME/sessions` for
  codex-brain agents (falling back to a rollout when the brain is unrecorded, as
  the hosted supervisor leaves it), so a codex director's history renders
  instead of a blank panel.

## 0.42.0 - 2026-07-09

Minor release: retire the in-repo watchdog now that the supervisor sidecar owns
container supervision, plus three reliability fixes for Codex prompts, session
drain, and turn keepalive. Rolling this to the fleet clears the Codex E2BIG
wedge that stalled long-prompt turns.

### Removed
- **In-repo watchdog (#720).** `bobi supervise` and the `bobi/watchdog.py` /
  `bobi/manager_health.py` modules are gone. Container supervision moved to the
  supervisor sidecar in the private deploy repo, a faithful port that keeps the
  `WATCHDOG_*` env var names so operator config carries over. The local product
  no longer ships `bobi supervise`; nothing else in the local runtime used it.

### Fixed
- **Codex prompts sent over stdin (#714).** A large prompt no longer blows the
  argv size limit (E2BIG): the Codex brain pipes the prompt in over stdin
  instead of passing it as a command-line argument, so long-context turns run
  instead of failing to spawn.
- **Session drain hardened against oversized/undecodable messages (#719).** A
  single oversized frame or a transient decode error no longer aborts the
  drain; the session skips the bad frame and keeps consuming, so a wedged turn
  cannot silently drop later events.
- **Keepalive during in-flight turns (#721).** `last_activity` is refreshed
  while a turn is still running, so a long-running turn is not mistaken for an
  idle session and torn down mid-flight.

## 0.41.1 - 2026-07-09

Patch release: ship the programmable stub brain so the private `bobi-deploy`
control-plane sidecar e2e can pin a public release instead of an editable
checkout. Test-only surface; no runtime behavior change for the local product.

### Added
- **Programmable stub brain (#716).** A test-only `BrainSession` (`bobi.brain.stub`)
  that speaks the provider-agnostic brain contract with no vendor CLI or
  network, so a real manager/subagent runtime can be driven to a deterministic
  state without the `claude` CLI. Registered as `stub` but gated: `make_session`
  refuses to run unless `BOBI_STUB_BRAIN` is set, so an accidental
  `BOBI_BRAIN=stub` in production fails loud. Scriptable over the inbox
  (`__stub__:hang|exit|reply|error|idle`) so tests trigger runtime state and
  observe the result. It is the shared test double for both the public
  integration suites and the private deploy-package sidecar e2e.

### Changed
- **Integration suites run both brains (#716).** The runtime-plumbing suites
  (manager start/stop/status/restart, webhook event flow, subagent launch) now
  run parametrized over the stub brain (fast lane, always) and real Claude
  (gated on the CLI), on one provisioning code path. Previously claude-gated and
  skipped in CI, so this adds real-runtime coverage that runs without Claude
  while keeping the with-Claude tier.

### Fixed
- **kb tests skip cleanly without the `[kb]` extra (#716).** Running
  `pytest tests/` on a `.[dev]`-only install no longer errors at collection
  (numpy) or fails on the store/cli tests (sqlite-vec); the kb-runtime tests
  skip gracefully. No-ops when `[kb]` is installed, so CI is unchanged.

## 0.41.0 - 2026-07-08

Minor release: the repo split lands. Deployment (containers, Fly fleet, the
Cloudflare Worker event tier) moves to the private `moda-labs/bobi-deploy`
repo, installed as a plugin; this repo now ships the open local product only.
First release cut from the two-repo layout.

### Added
- **Self-hosted event server guide (#710).** `docs/SELF_HOSTED_EVENT_SERVER.md`
  documents running your own webhook ingress: a tunnel in front of the local
  server, or the standalone Node event server, with TLS, provider wiring, and
  restart semantics.
- **Publishable events-core package (#711).** `@moda-labs/bobi-events-core`
  gains a pack pipeline (compiled ESM + type declarations) so external
  consumers can pin it from npm instead of vendoring sources.

### Changed
- **Repo split (#713).** Deploy, fleet, and Cloudflare Worker code moved to the
  private `moda-labs/bobi-deploy` repo. `bobi deploy`, `bobi deploy-init`,
  `bobi destroy`, and `bobi build` are now delivered by the separately
  installed `bobi-deploy` package via the CLI plugin seam (#699, #709); a
  plain `bobi` install no longer carries them. CI workflows split along the
  same boundary (#704), with an import-direction guard making the one-way
  rule (private imports public, never the reverse) permanent (#701).
- **events-core workspace boundary (#702).** The event protocol core
  (normalized events, webhook pipeline, channel adapters, circuit breaker)
  lives in `event-server/core/` as an npm workspace package behind a real
  import boundary.
- **TeamRuntime seam (#706).** The webapp runs against an explicit runtime
  interface instead of the `BOBI_ROOT` root binder, decoupling the web UI
  from process-global state (#708).
- **Memory subsystem rename.** The curator subsystem is now the sleep cycle;
  `policy.md` becomes `long_term_memory.md`.

### Fixed
- **Curator spawn contract (#695).** The sleep-cycle curator gets a dedicated
  monitors command and an entry-role fallback, fixing the wedged-curator
  stack (E2BIG, `--role` misrouting, check-runner hijack) (#697).
- **Slack reply formatting defaults (#703).** Better default formatting for
  Slack replies (#705).

## 0.40.0 - 2026-07-08

Minor release: WhatsApp joins the channel gateway, event delivery becomes
restart-durable with chat priority, the local web UI surfaces converge on
`bobi app`, and webhook/setup paths get release hardening.

### Added
- **WhatsApp channel adapter (#656).** Add reactive-only WhatsApp support via
  the Meta Cloud API, including setup docs and gateway integration tests
  (#665).
- **Durable inbox delivery and chat priority (#688).** Event drain ACKs now wait
  for session processing, human chat messages jump bulk webhook backlog, and
  dropped messages log enough detail to diagnose delivery loss (#691).
- **Local ingest tokens from env (#661).** Local development can seed scoped
  ingest tokens from environment configuration (#685).

### Changed
- **Unified local web surfaces (#614).** Bare `bobi`, `bobi setup`, and local
  `bobi agent <name> ui` now route through the unified `bobi app` surface; the
  old standalone agent UI and stale design/dashboard assets are gone (#694).
- **Shared signed JSON envelopes (#664).** Signed event-server responses now use
  one shared envelope path across clients (#679).
- **Slack gateway cleanup (#652).** Removed legacy Slack send shims after the
  Chat SDK migration (#684).

### Fixed
- **Setup connection refresh during chat (#683).** Streaming setup chat updates
  now refresh connection cards, coalesce duplicate refreshes, and reconcile
  redacted user bubbles (#693).
- **Curator monitor task delivery (#682).** Curator monitor tasks now deliver via
  files instead of fragile inline payloads (#689).
- **Config dotenv interpolation isolation (#596).** Dotenv interpolation is
  isolated so one config load cannot leak substitutions into another (#687).
- **Event grant and release drift warnings (#669, #670).** Unbacked event grants
  now warn, and release automation verifies the fleet event-server identity
  after deploy (#681, #686).
- **Linear webhook reliability (#650, #671, #672).** Linear setup docs now cover
  webhook secret provisioning, comment webhooks route by issue team, and Linear
  webhooks ACK before fan-out (#675, #678, #683).
- **Inbound ingress and onboarding polish (#590, #635).** Bobi now warns when
  inbound ingress is unreachable and clarifies setup/onboarding paths (#676,
  #677).
- **Slack placeholder behavior.** Automatic Slack placeholders are removed for
  cases where typing/status UX should own progress indication (#674).

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
