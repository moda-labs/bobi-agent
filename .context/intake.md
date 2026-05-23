# Intake — 2026-05-22 — agent/mds-22
Classification: update
Problem: Modastack can't bootstrap repos on a fresh remote box without SSH — no clone-from-URL, no worktree cleanup, no Slack-driven setup.
User & moment: Operator deploying modastack to a remote machine (EC2, Mac mini) who wants to manage everything via Slack after initial setup.
In scope: Config format supporting remotes, `register` accepting org/repo, auto-clone on startup, main branch sync before spawn, worktree cleanup on Done, git identity config, manager prompt for Slack-driven repo setup, deploy script simplification.
Out of scope:
  - GitHub App auth (stays with `gh auth login`)
  - Multi-machine coordination
  - S3/artifact storage
  - Custom deploy pipelines
Size: large (single PR — sub-features are tightly coupled)
UX decision: none (CLI + Slack interface, no UI)
Scope-guard answers:
  Billing primitive: N/A
  User journey: N/A
  Schema change: additive — bare path strings still work, new {remote, path} objects are optional. Rollback: revert to bare paths in config.yaml.
Next: /spec (mandatory — self-modification guardrail, touches manager/ and engineer/)
