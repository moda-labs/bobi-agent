# Changelog

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
