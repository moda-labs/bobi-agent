# Changelog

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
