---
issue_id: MDS-24
title: Web dashboard — task status and event log viewer
worktree: /Users/zkozick/dev/modastack/worktrees/mds-24
branch: agent/mds-24
phase: spec_complete
spec_path: specs/mds-24-web-dashboard.md
complexity: medium
---

## Status
Spec written: specs/mds-24-web-dashboard.md

FastAPI dashboard with event log viewer, session status, and decisions panel.
New module `dashboard/` with 4 files. Three new deps: fastapi, uvicorn, jinja2.
New CLI command: `modastack dashboard`.
