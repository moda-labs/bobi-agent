# Changelog

## 0.3.0 — 2026-05-23

### Added
- Remote deployment: `modastack register org/repo` clones repos via `gh`, auto-detects settings
- Auto-clone missing repos on startup — restart/redeploy just works
- Main branch sync (fetch + reset) before engineer spawn keeps worktrees current
- Worktree + branch cleanup when tasks move to Done
- Git identity config during `modastack init` for headless remote boxes
- Manager can set up repos via Slack DM — no SSH needed after initial auth
- Handoff docs moved to `~/.modastack/handoffs/` — target repos stay clean

### Changed
- All repo config consolidated into `~/.modastack/config.yaml` — eliminated per-repo `.modastack.yaml`
- `RepoConfig` replaced by `RepoEntry` in `GlobalConfig` — one config layer instead of two
- `modastack register` accepts both local paths and `org/repo` format
- Deploy script simplified — repos managed via Slack after one-time SSH setup

### Removed
- `.modastack.yaml` per-repo config files — no longer needed
- `RepoConfig` class and `generate_dispatch_yaml()` / `setup_repo()` functions
- `example.modastack.yaml`

## 0.2.1 — 2026-05-23

- Self-updating: version check poller, Slack notification, user-approved update
- Slack threading fix — conversations inline, only proactive updates threaded

## 0.2.0 — 2026-05-20

- Event-driven architecture with persistent manager session
- Linear + GitHub Issues task tracking
- Slack Socket Mode for real-time events
- Engineer lifecycle: pickup, spec, implement, prepare-pr, feedback
- Orphan session detection
