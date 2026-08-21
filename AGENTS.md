# Bobi Agent Instructions

Read `~/AGENTS.md` first for general coding rules. This file only contains
repo-specific guidance for Bobi.

Bobi is an event-driven AI agent framework.

## Reference Docs

- `README.md`: product overview, installation path, architecture summary, and
  user-facing setup docs.
- `skills/bobi.md`: CLI command reference.
- `skills/create-agent.md`: agent team authoring guidance.
- `skills/checklist-execution.md`: the generic worker protocol for running a
  long job from a committed markdown checklist (read once, next item, verify,
  commit) - no engine, no framework code.
- `skills/slack-setup.md`: Slack integration setup.
- `skills/whatsapp-setup.md`: WhatsApp (Meta Cloud API) integration setup.
- `skills/discord-setup.md`: Discord bot integration setup (Gateway, local server).
- `skills/linear-setup.md`: Linear integration setup.
- `docs/EVENT_SERVER.md`: event-server architecture, topics, and security model.
- `docs/SELF_HOSTED_EVENT_SERVER.md`: running your own webhook ingress - tunnel,
  standalone Node event server, or the durable Cloudflare Worker.
- `docs/ADMIN_PROTOCOL.md`: the supervisor sidecar's wire contract (admin
  topics, the nine commands, heartbeat/lifecycle schemas, compatibility
  promise). Versioned by `SUPERVISOR_VERSION`; additive-only pre-1.0.
- `docs/REFERENCE_IMAGE.md`: the published container image
  (`ghcr.io/moda-labs/bobi`) - what it contains, the `--init` requirement, the
  runtime env contract, the `TEAM_DEPS` bake hook, and how it is published.
- `docs/AGENT_STATE.md`: the agent page's state tri-state (`running` /
  `stopped` / `not_responding`), the manager health probe behind it, and the
  status strip's best-effort telemetry segments.
- `docs/AGENT_OVERVIEW.md`: the agent page's read-only composition view
  (`GET .../overview`) and the `script_cache` savings block in the spend
  payload - how automations are counted and how savings are priced.
- `docs/RUNS_VIEW.md`: the unified runs read model behind the agent page's one
  table (`GET .../runs`) - the status vocabulary, the stalled threshold, and
  the rule that one piece of work produces one row.
- `docs/RUN_DRILLDOWNS.md`: opening a run - the debugging transcript view
  (timestamps + tool calls, distinct from `/messages`) and the Details
  payload for runs that have no transcript.
- `docs/MONITORS.md`: monitor scheduler and the `script_cache` token-saving runner.
- `docs/WORKFLOW_ENGINE.md`: workflow state machine, step types, suspend/resume.
- `docs/RUN_RESUME.md`: resuming a stalled workflow run from the agent page -
  why it spawns a process, and where the single-winner claim lives.
- `docs/TOOL_LIBRARY.md`: unified dependency model - declaring tools/skills/MCP
  deps (pinned `install:` vs guide-only), the catalog, and how they bake + verify.
- `docs/OTEL.md`: agent-authored OTLP telemetry (`bobi agent <name> otel`) -
  operator setup, the resource-attribute table, collector bring-up, and the
  write-only per-instance token requirement.
- `docs/SECURITY.md`: overall security model (trust, credentials, prompt-injection).
- `docs/TICKETING_POLICY.md`: Linear/GitHub ticketing conventions.
- `docs/RELEASE_RUNBOOK.md`: release process and checklist.
- `docs/FRONTEND_QA.md`: local frontend QA guidance for Bobi's vanilla web UIs.
- `docs/design-system/`: the Bobi design system - source of truth for
  anything visual on any Bobi surface (palette, type, icon set, components).
- `DESIGN.md`: source of truth for `bobi setup` UX and its offline constraints;
  its visual tokens were superseded by the design system on 2026-07-31.

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

General coding, bug-fix, testing, writing, and commit standards are the
house standards; `~/AGENTS.md` points to them. This file carries only
Bobi-specific deltas on top:

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

- **Durable state goes through `bobi/fsutil.py`.** Any file bobi must still be
  able to read after an abrupt death - monitor state, the spend window,
  workflow runs, `config.toml`, the setup checkpoint, pid/port files - is
  written with `atomic_write_text` / `atomic_write_json`, never a bare
  `write_text`. Loaders in this repo treat unparseable state as *empty* and
  reset, so a torn write does not lose one field, it silently drops the whole
  document. Read-modify-write state (a load, a mutate, a save) additionally
  takes `fsutil.file_lock`; atomicity alone keeps the file parseable but does
  not stop a concurrent updater's change from being overwritten. Do not
  hand-roll a seventh tmp+rename - that duplication is what stopped durability
  fixes from propagating (D092). **One exception, and it is a real one:** the
  write lands as a new inode renamed over the target, so the target's mode,
  ownership, and symlink-ness do not survive. A secret whose confidentiality
  depends on its mode is created at that mode instead
  (`events.state.save_bubble_state` opens `bubble.json` with `0o600` and stays off
  the helper on purpose).

## Development Lifecycle

Engineering work in this repo moves through four staged contracts: plan,
build, review, land. The stage contracts live in an installed skill
pack, not in this repo; agents with the pack resolve the stages from the
skills themselves. This section carries only the repo-anchored
conventions that hold regardless of how the stages are tooled:

- **Plans**: initiative-sized work (multiple coherent deliverables) gets
  a plan artifact `plans/YYYY-MM-DD-<slug>.md`, dated on creation — the
  stage pack validates this shape, and existing undated paths stay valid.
  It is merged and amended via PR, with a
  lightweight GitHub tracking issue (the issue holds discussion; the plan
  file is the source of truth). That issue is the feature request itself —
  no `plan` label, no `[plan]` prefix, a title that outlines the task to be
  accomplished, and a body someone can act on. Bug titles take the opposite
  shape: the shortcoming caused, in as few words as possible. See
  `docs/TICKETING_POLICY.md` §1a and §2a. Builders
  flip the plan's status markers (`[ ]` / `[wip]` / `[x]` / `[f]`)
  inside their PRs; post-approval changes are dated amendments, never
  silent rewrites. Single-unit work skips the plan and writes its design
  into the GitHub issue directly (see `docs/TICKETING_POLICY.md`).
  Legacy: epics already in flight with design docs in their issue bodies
  stay that way until they finish - do not migrate them.
- **Verification**: exercising the real flow end-to-end (isolated
  `BOBI_HOME`, real agent sessions), not just the test suite. Update the
  affected docs in the same PR as the change, never as a follow-up.
- **Landing**: a PR is authorized to land once a human maintainer has
  approved it — the approval is PR-bound (it survives amendments and
  mechanical rebases; decision 2026-07-23) — and its CURRENT head
  carries a LANDABLE house verdict with green required checks. This
  authorization form applies to human and bot landers alike. Merge only
  when checks are green, watch the merge commit's post-merge CI, then
  clean up the branch, worktree, and ticket. Landing never touches
  versions or changelogs - release work follows Release Rules below.
- **Continuity**: at a session boundary with unfinished work, write a
  handoff file capturing verified state so a fresh session can resume;
  handoff files stay local and uncommitted.

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

### Node, and the two versions that cannot share a shell

Day-to-day work needs no Node at all: `pip install -e .` is an *editable*
install, and `hatch_build.py` returns early for those, so the embedded event
server is never built.

Two tasks do need it, at **two incompatible versions**:

| Task | Node | Why |
|---|---|---|
| Any non-editable wheel build, and therefore `tests/integration/test_container_image.py` and `tests/integration/test_packaged_event_server.py` | **20 exactly** | `hatch_build.py::_require_build_node` rejects any other major outright, 22 and 24 included |
| The Worker/wrangler suites (`event-server/`) | **22+** | wrangler refuses anything below 22 |

There is deliberately no `.nvmrc`: a single pin would be wrong for one of the
two. Select per task, e.g. `nvm use 20` before a container run. This is also
why no CI job runs both — see the pinning comments in `container.yml` and
`ci.yml`.

A **fresh worktree also needs the event-server's npm deps**, separately from
the Python install:

```bash
cd event-server && npm ci
```

Several integration tests spawn a Node stub that resolves `ws` through
`NODE_PATH=event-server/node_modules`. Without it the stub cannot start and
the test fails 15s later as `gateway stub did not come up`, which reads like a
timeout rather than a missing dependency.

Between them, those two prerequisites are the entire reason
`test_container_image.py` and `test_packaged_event_server.py` were long
believed unable to run locally. Both run fine with Node 20 selected and
`npm ci` done; there is no deeper obstacle.

The container recipe (`Dockerfile`, `docker/`) and the Cloudflare Worker event
tier (`event-server/worker/`) live in THIS repo and are public, alongside the
three local event-server variants. So does the console, whole: the UI and BOTH
`TeamRuntime` implementations behind it - `LocalRuntime` for `bobi app`, and
`EventBusRuntime` (`bobi/webapp/event_bus.py`) for a deployed fleet driven over
the operator-authed `/fleet` API. Moda's own deployment surface - the Fly
deploy engine, the fleet workflows, and the hosted app that BINDS
`EventBusRuntime` to moda's fleet URL and operator token - is private, in
`moda-labs/moda-agents` under `bobi-deploy/`, and consumes this repo as a
RELEASED PyPI version (`pip install bobi==<pin>`), never a checkout. The former
`moda-labs/bobi-deploy` repo is archived; nothing builds or releases from it.

Two rules survive that move, both enforced by
`tests/test_import_boundaries.py`: private imports/pins public, never the
reverse; and the public/private line itself, encoded as literal allowlists
(`WORKER_ADAPTER_MODULES` vs `PUBLIC_LOCAL_MODULES`, plus a container-token
scan over `bobi/`) so a module that lands on the wrong side fails CI rather
than drifting silently. A fleet repo must never gate this repo's tip-of-main:
`bobi-agent` owns the machinery that keeps its releases from breaking
consumers.

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

## Live CI Lanes

Two workflows spend real money to prove the things the rest of CI cannot.
Both are **off by default**: they run nightly, on manual dispatch, or on a PR
carrying the **`ci:live`** label. Neither runs on a fork PR at all.

| Lane | Workflow | Proves | Cost per run |
|---|---|---|---|
| Brains | `container.yml` (`container-image` job) | The reference image completes one real ask round-trip on a **Claude** brain and one on a **Codex** brain, against an ephemeral event server started from the real Worker sources | Two model calls (a few cents) |
| Worker deploy | `worker-deploy-smoke.yml` | `wrangler deploy` produces a **working** Worker — the KV binding resolves, the `v1` `new_sqlite_classes` Durable Object migration applies, and a publish→subscribe round-trip completes against the deployed URL | One Cloudflare deploy |

Trigger either on a PR with `gh pr edit <n> --add-label ci:live`, or standalone
with `gh workflow run container.yml` / `gh workflow run worker-deploy-smoke.yml`.

**They must prove they RAN.** Every test in both lanes carries a `skipif`, so a
renamed secret or an unavailable harness would skip to green — which is exactly
how this repo shipped four green-but-vacuous lanes before #909. Each lane
therefore has two guards: a fail-fast step when a credential is empty, and
`scripts/assert_junit_ran.py`, which reads the junit report and rejects any
skip, any wrong count, and any missing named test. `tests/test_ci_live_wiring.py`
asserts the wiring itself is still in place, and fails if a live step is deleted.

**They must smoke the right deployment.** `wrangler deploy` and the
`wrangler secret bulk` that follows it publish two Worker versions reporting the
same release sha, and Cloudflare's rollover between them is not atomic. The
first one inherits the previous run's secrets, so smoking it yields a 401 on
`/mcp` and a 500 on the round-trip against credentials minted seconds earlier
(run 30895709421). `scripts/await_worker_ready.py` is the gate: it clears only
once the release sha matches, an operator-authenticated route answers 200 to
THIS run's minted token, and the serving `worker.version_id` holds still, across
five consecutive probes. Its behaviour is exercised in
`tests/test_ci_guard_scripts.py` against a Worker that fakes the rollover -
inline shell had carried this logic through two fixes with no test that could
reach it. The lane also takes a `concurrency` group: one shared Worker means
one run at a time, or two runs overwrite each other's minted secrets.

**The Worker lane is isolated by construction.** It deploys to a dedicated
`bobi-events-ci-smoke` Worker with its own KV namespace, in a **separate
Cloudflare account** from the one the fleet runs production on — sharing
production's KV would let smoke traffic write live event state. The separate
account is what makes the isolation real: Cloudflare grants Workers and KV
permissions at ACCOUNT scope only, with no per-script or per-namespace
restriction, so a token confined to a CI-only account is the strongest
boundary available. `scripts/render_worker_ci_config.py` derives the
CI config from the shipped `wrangler.jsonc` (so the migration and compatibility
date stay identical to what production deploys) and refuses to render a config
that names `bobi-events`, that carries the `REPLACE_WITH_YOUR_KV_NAMESPACE_ID`
placeholder, or whose KV id did not come from CI configuration.

Required repo secrets: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (brains);
`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CI_SMOKE_KV_NAMESPACE_ID`
(Worker deploy). `pull_request_target` is never used in this repo — it is the
one trigger that would hand a fork's code these secrets, and a test enforces
its absence.

**Known blind spot:** no canary exercises `auth: subscription`, the mode most
fleet teams run, because a fresh subscription volume triggers a device login
that blocks on a human. Tracked separately; see
`plans/2026-08-01-ci-coverage.md` Q3.

## Frontend QA

For any frontend change, read `docs/FRONTEND_QA.md` before deciding how to test it.

## Design System

`docs/design-system/` is the **Bobi design system** and the source of
truth for anything **visual** on any Bobi surface. Read its `README.md` before
making a visual decision; `SKILL.md` lists the non-negotiables. It is also
invocable as `/bobi-design`.

The four rules that matter most:

1. **Violet is state, not decoration** — live, enforced, gated, focused.
   Anything decorative is clay. Moda's red never appears on a Bobi surface.
2. **Gates are sacred** — a human approval step always renders with the violet
   rail + rotated-square glyph and names the workflow and step. Never a toast.
3. **Config is the interface** — show real filenames, real YAML, real shell.
4. **The lockup always travels with the "BY MODA LABS ↗" byline.**

Also load-bearing: mono is for **data** (paths, ids, crons, code), sans for
chrome; product chrome is **lowercase**, with uppercase only on document plate
labels and corner marks; Bobi's own hand-drawn icon set only — never Lucide,
Heroicons, or emoji.

`DESIGN.md` remains the source of truth for the **UX** of the `bobi setup`
wizard (its flow, pacing, and the digestion-prompt philosophy) and for its
local hard constraints: fully offline, vanilla HTML/CSS/JS, no build step,
inline SVG only. As of the 2026-07-31 amendment it is **no longer** the origin
of the visual tokens — both local web UIs were reskinned onto the design
system.

The ported tokens live in `bobi/webui_common/static/tokens.css`, shared by
`bobi setup` and `bobi app`. It is hand-maintained, so
`tests/test_webui_tokens.py` pins it to the design system and fails on drift.
The brand faces (Geist, Geist Mono, Inter) are vendored as woff2 under
`bobi/webui_common/static/fonts/` — never a CDN, so the UIs stay offline.
Refresh them with `python3 scripts/fetch_brand_fonts.py
bobi/webui_common/static/fonts`.

## Release Rules

Feature PRs must not bump the version or edit `CHANGELOG.md`. Leave `VERSION`, the
`version` field in `pyproject.toml`, and `CHANGELOG.md` untouched unless the
task is explicitly a release.

Write PR descriptions with enough detail to support a later release changelog:
what changed, why, and the ticket id.

Release work happens only at release time: bump versions, write the
`CHANGELOG.md` entry, and publish the GitHub Release that triggers the release
workflow. Follow `docs/RELEASE_RUNBOOK.md` for the full process.
