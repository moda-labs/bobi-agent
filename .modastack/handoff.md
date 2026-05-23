---
issue_id: MDS-21
title: Add stall detection and auto-routing for engineer sessions
worktree: /Users/zkozick/dev/modastack/worktrees/mds-21
branch: agent/mds-21
phase: spec_complete
spec_path: specs/mds-21-stall-detection.md
complexity: medium
---

## Status
Spec written: specs/mds-21-stall-detection.md

Heartbeat-based stall detection via output hashing in _poll_workers,
permission prompt detection in detect_state, process liveness checks,
and manager auto-routing instructions. Four new event types:
worker.stalled, worker.stuck, worker.permission_blocked, worker.process_dead.

Files to change: pollers.py, session.py, prompt.md, new test file.
