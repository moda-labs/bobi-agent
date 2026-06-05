# Changelog

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
- **`GlobalConfig` class removed** — all configuration is per-repo via `LocalConfig` (`.modastack/local.yaml`) and `RepoConfig` (`.modastack/config.yaml`)

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
