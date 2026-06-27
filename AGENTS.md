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
- `docs/ticketing_policy.md`: Linear/GitHub ticketing conventions.
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

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Worktree Policy

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

CI failure or production bug means there is an integration test gap. Add a
failing integration test that reproduces the problem before fixing the behavior.

## Documentation Work

For the MOD-212 documentation effort, start with the public onboarding path:

- Improve `README.md` first.
- Then clean up `docs/` so user-facing docs are discoverable and design/spec
  material is separated from setup documentation.
- Then make `CLAUDE.md` point to this file once `AGENTS.md` is the canonical
  agent instruction file.

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
