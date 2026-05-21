---
name: modastack project state
description: Current state and decisions for the modastack project — cron-based dispatch loop with Linear as primary interaction channel
type: project
---

Building a cron-based agent dispatch system at ~/dev/modastack.

**Why:** User wants to understand agentic development from first principles by building their own orchestration layer, taking design decisions from OpenClaw and Hermes.

**Key decisions made:**
- Linear + Linear comments as the sole interaction channel (Slack deferred to later)
- Cron every 1 minute (cheap cycle, just HTTP calls)
- Python + pip/venv
- Per-repo .modastack.yaml for portability
- Named credentials in ~/.modastack/credentials.yaml for multi-workspace
- Skills discovery layer (auto-detects gstack or any skill pack)
- BLOCKED state: agent posts question as Linear comment, polls for reply each cycle
- GitHub repo: underminedsk/modastack (public)
- GitHub account for this project: underminedsk

**How to apply:** When working on this project, use the underminedsk GitHub account. Linear is the primary channel — no Slack integration yet. Keep the architecture simple (cron, JSON state file, no daemon).
