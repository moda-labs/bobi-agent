# Bobi Agent Instructions

Read `~/AGENTS.md` first for general coding rules. This file only contains
repo-specific guidance for Bobi.

Bobi is an event-driven AI agent framework.

## Reference Docs

- `README.md`: product overview, installation path, architecture summary, and
  user-facing setup docs.
- `skills/bobi.md`: CLI command reference.
- `skills/create-agent.md`: agent team authoring guidance.
- `skills/slack-setup.md`: Slack integration setup.
- `skills/linear-setup.md`: Linear integration setup.
- `docs/EVENT_SERVER.md`: event-server architecture, topics, and security model.
- `docs/MONITORS.md`: monitor scheduler and the `script_cache` token-saving runner.
- `docs/WORKFLOW_ENGINE.md`: workflow state machine, step types, suspend/resume.
- `docs/SECURITY.md`: overall security model (trust, credentials, prompt-injection).
- `docs/TICKETING_POLICY.md`: Linear/GitHub ticketing conventions.
- `docs/RELEASE_RUNBOOK.md`: release process and checklist.
- `DESIGN.md`: source of truth for `bobi setup` web UI visual and UX decisions.

## First Principles

- Keep the framework generic. Do not bake Moda-specific workflow assumptions
  into `bobi/`.
- Treat agent teams as the distribution unit for domain behavior: prompts,
  roles, workflows, monitors, tools, and context.
- Runtime behavior should read from the installed package image under
  `$BOBI_HOME/agents/<name>/run/package/`, not directly from source packages.
- Credentials belong in runtime `.env` files or environment variables. Never
  commit secrets.

## Coding Standards

### Code

- Prefer quality, simplicity, robustness, scalability, and long-term maintainability.
- Keep a single code path for doing any one thing.
- Review code for simplicity.
- If something looks off, fix it along the way, even if it is unrelated to the current task.

### Bug fixes

- A CI failure or production bug means there is an integration test gap.
- Reproduce the bug first, as closely aligned to real usage as possible.
- Write a failing test that reproduces the problem, then write the fix.

### Testing

- When developing a new feature, unit tests are important, but end-to-end integration tests better prove the feature functions correctly.
- Write integration tests that mimic the actual user experience as much as possible.
- When working on tests, review the current set of tests and ensure coverage is complete but non-redundant.

### Markdown and writing

- Prefer concise wording over long descriptions.
- Never use the em dash. Use a regular dash instead.

### Commits

- Never auto-add your agent name as co-author in commit messages.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Worktree Policy

- Before creating a new worktree, fetch the latest `main` (or the intended base
  branch) and branch the worktree from it, never from a stale local base.
- Use `worktrees/<branch-or-task-name>/` under the repo root for task-specific
  worktrees.
- Keep only active worktrees in `worktrees/`; remove stale directories after
  their branch or PR is no longer active.
- Do not create worktrees outside this repo unless the user explicitly asks.
- Keep each worktree focused on one issue, branch, or task.

## Tests

```bash
pytest tests/ --ignore=tests/integration/  # unit tests
pytest tests/                              # full suite, includes integration tests
```

Integration tests drive real Claude Code sessions. Run them before pushing when
the change touches runtime behavior, session orchestration, workflows, monitors,
or event delivery.

## Design System

Before any visual or UX decision on the `bobi setup` web UI, read `DESIGN.md`.
It supersedes older setup UI design assumptions elsewhere in the repo.

## Release Rules

Feature PRs must not bump the version or edit `CHANGELOG.md`. Leave `VERSION`, the
`version` field in `pyproject.toml`, and `CHANGELOG.md` untouched unless the
task is explicitly a release.

Write PR descriptions with enough detail to support a later release changelog:
what changed, why, and the ticket id.

Release work happens only at release time: bump versions, write the
`CHANGELOG.md` entry, and publish the GitHub Release that triggers the release
workflow. Follow `docs/RELEASE_RUNBOOK.md` for the full process.
