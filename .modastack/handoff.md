---
issue_id: MDS-25
title: Self-updating — versioning, changelog, and Slack-driven updates
worktree: /Users/zkozick/dev/modastack/worktrees/mds-25
branch: agent/mds-25
phase: spec_complete
spec_path: specs/mds-25-self-updating.md
complexity: medium
---

## Status
Spec written: specs/mds-25-self-updating.md

Adds version-check poller (hourly, compares local VERSION vs origin/main),
Slack notification with changelog summary, user-approved self-update via
`modastack self-update` CLI command, and rollback support.

Key files to change: pollers.py (new poller), cli.py (new commands),
prompt.md (update event handling), consumer.py (startup check),
plus new CHANGELOG.md and __version__.py.
