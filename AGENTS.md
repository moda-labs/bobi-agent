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
- `skills/whatsapp-setup.md`: WhatsApp (Meta Cloud API) integration setup.
- `skills/linear-setup.md`: Linear integration setup.
- `docs/EVENT_SERVER.md`: event-server architecture, topics, and security model.
- `docs/MONITORS.md`: monitor scheduler and the `script_cache` token-saving runner.
- `docs/WORKFLOW_ENGINE.md`: workflow state machine, step types, suspend/resume.
- `docs/TOOL_LIBRARY.md`: unified dependency model - declaring tools/skills/MCP
  deps (pinned `install:` vs guide-only), the catalog, and how they bake + verify.
- `docs/SECURITY.md`: overall security model (trust, credentials, prompt-injection).
- `docs/TICKETING_POLICY.md`: Linear/GitHub ticketing conventions.
- `docs/RELEASE_RUNBOOK.md`: release process and checklist.
- `docs/FRONTEND_QA.md`: local frontend QA guidance for Bobi's vanilla web UIs.
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

## Development Lifecycle

Skills own the SDLC stages. Default path for any ticketed change:

1. **Scope and design**: write the design into the GitHub issue (see
   `docs/TICKETING_POLICY.md`). Design docs live in issues, not in `docs/`.
2. **Build**: `/build <issue#>` runs the full cycle for one ticket: scope from
   the issue, worktree from fresh `main`, implement with tests, verify, review,
   PR. Prefer it over ad-hoc implementation for anything ticketed.
3. **Debug**: for bugs and CI failures, `/investigate` to root-cause before
   writing a fix. Reproduce with a failing test first (see Bug fixes above).
4. **Verify**: `/verify` after any nontrivial runtime change. Exercise the real
   flow end-to-end (isolated `BOBI_HOME`, real agent sessions), not just the
   test suite. `/build` runs this as its verification stage.
5. **Review**: `/code-review high` on the working diff before opening a PR;
   apply confirmed findings. `/build` runs this as its review stage. Use
   `/simplify` for a quality-only pass when a diff has grown organically.
6. **Ship**: open the PR from `/build`, or manually per Release Rules below.
   No version or changelog edits in feature PRs.
7. **Continuity**: `/handoff` at the end of a session with unfinished work so
   a fresh session can resume; handoff files stay local and uncommitted.

Standalone stages (debugging an existing bug, reviewing someone else's diff)
use the individual skills directly; `/build` is the umbrella for new work.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,kb]" -e ./bobi_deploy
```

Use this install for broad non-integration test runs. It matches the CI `Unit
tests` job and includes the knowledge-base dependencies imported during test
collection (`bobi_deploy` is the deploy-plugin package; its tests live in
`bobi_deploy/tests/`). Use `.[dev]` only for focused e2e work that does not
collect the KB test surface.

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
pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q  # unit tests
pytest tests/                              # full suite, includes integration tests
```

Integration tests drive real Claude Code sessions. Run them before pushing when
the change touches runtime behavior, session orchestration, workflows, monitors,
or event delivery.

## Frontend QA

For any frontend change, read `docs/FRONTEND_QA.md` before deciding how to test it.

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
