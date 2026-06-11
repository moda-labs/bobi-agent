# Changelog

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
