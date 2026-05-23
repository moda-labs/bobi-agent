---
issue_id: MDS-25
title: Self-updating — versioning, changelog, and Slack-driven updates
worktree: /Users/zkozick/dev/modastack/worktrees/mds-25
branch: agent/mds-25
phase: implement_complete
spec_path: specs/mds-25-self-updating.md
complexity: medium
---

## Status
Implementation complete. All tests passing (15/15 new, 34/34 existing).

### What was built
- `modastack/__version__.py` — reads VERSION file as runtime source of truth
- `CHANGELOG.md` — initial changelog
- `_poll_version` + `_check_version` in pollers.py — hourly version check poller
- `modastack self-update` — fetch, stash, pull --ff-only, pip install, pop stash
- `modastack rollback` — reset to pre-update HEAD from saved state
- Manager prompt update — handles `system.update_available` events via Slack DM
- Startup one-shot version check in consumer.py
- `tests/test_self_update.py` — 13 unit tests
