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
- `skills/discord-setup.md`: Discord bot integration setup (Gateway, local server).
- `skills/linear-setup.md`: Linear integration setup.
- `docs/EVENT_SERVER.md`: event-server architecture, topics, and security model.
- `docs/SELF_HOSTED_EVENT_SERVER.md`: running your own webhook ingress - tunnel
  or standalone Node event server.
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

General coding, bug-fix, testing, writing, and commit standards live in
`~/AGENTS.md`. Bobi-specific additions:

- **Real-Claude e2e as acceptance criteria (judgement call).** Bobi's runtime
  runs through a real Claude brain. For a feature whose correctness depends on
  that brain path (session orchestration, turn handling, tool use, resume,
  event delivery through a live session), the acceptance bar includes an
  end-to-end integration test that drives a REAL Claude session, not only the
  deterministic `stub` brain. Follow the "one mechanism, two brains" pattern:
  parametrize the e2e `[stub]+[claude]`, gate the claude leg on the CLI so it
  runs when available and skips otherwise. This is a judgement call per feature,
  usually the implementor's: a brain-agnostic change (process lifecycle, event
  routing, read-model folds, the admin/control plane) is proven by the stub e2e
  and does not need a claude leg - add one only when the real brain is where the
  risk actually lives.

## Development Lifecycle

Work moves through four named stages — **plan**, **build**, **review**,
**land**. Each stage has a defined contract; the tooling that implements
them lives outside this repo, so this section names the stages generically.

1. **Plan**: initiative-sized work (multiple coherent deliverables, phased
   delivery) gets a plan artifact: `plans/<slug>.md` in this repo, merged
   and amended via PR, with a lightweight GitHub tracking issue labeled
   `plan` (the issue holds discussion and labels; the plan file is the
   source of truth). Builders flip the plan's status markers (`[ ]` /
   `[wip]` / `[x]` / `[f]`) inside their PRs, and post-approval changes are
   dated amendments, never silent rewrites. Single-ticket work skips the
   plan and writes its design into the GitHub issue directly (see
   `docs/TICKETING_POLICY.md`). Legacy: epics already in flight with design
   docs in their issue bodies stay that way until they finish — do not
   migrate them.
2. **Build**: the full cycle for one ticket: scope from the issue, worktree
   from fresh `main`, implement with tests, verify, review, PR. For bugs
   and CI failures, root-cause before writing a fix and reproduce with a
   failing test first (per the bug-fix standards in `~/AGENTS.md`).
   Verification means exercising
   the real flow end-to-end (isolated `BOBI_HOME`, real agent sessions),
   not just the test suite.
3. **Review**: every nontrivial diff gets an adversarial review before it
   merges — independent findings, each verified against the code, with an
   explicit landable / needs-fixes verdict. Apply confirmed findings; the
   build stage runs this before opening the PR, and it also stands alone
   for reviewing someone else's diff.
4. **Land**: merging is a deliberate step, distinct from opening the PR:
   merge only when checks are green, watch the merge commit's post-merge
   CI, then clean up the branch, worktree, and ticket. Landing never
   touches versions or changelogs — release work follows Release Rules
   below.

Continuity: at a session boundary with unfinished work, write a handoff
file capturing verified state so a fresh session can resume; handoff files
stay local and uncommitted.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,kb]"
```

Use this install for broad non-integration test runs. It covers the `ci.yml`
unit suites and includes the knowledge-base dependencies imported during test
collection. Use `.[dev]` only for focused e2e work that does not collect the
KB test surface.

Deployment (containers, Fly fleet, the Cloudflare Worker event tier) lives in
the private `moda-labs/bobi-deploy` repo, which installs this repo from a
side-by-side checkout. Nothing in `bobi/` may import from it: private
imports/pins public, never the reverse (`tests/test_import_boundaries.py`
enforces this).

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
