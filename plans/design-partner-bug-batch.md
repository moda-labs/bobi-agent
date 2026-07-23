# Design-partner bug batch: template scanner + auto_dispatch role

> **Status:** Approved
> **Tracking issue:** moda-labs/bobi-agent#828 · **Created:** 2026-07-23 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Fix the two open bugs reported by design partner @3amon from dogfooding a
containerized agent team (#797, #796). Both block real `auto_dispatch` usage:
one corrupts workflow templates at install time, the other makes multi-role
workflows impossible to auto-dispatch.

Secondary purpose, stated honestly: this plan is deliberately small — two
disjoint single-file lanes — because it is the first real run of the hosted
`plan-execute` workflow (moda-agents `plan-execute.yaml`, the headless host of
the house execute skill). It verifies that machinery end-to-end (gate →
parallel lane dispatch → landing queue → suspend → resume → converge) at low
stakes before the large `plans/review-remediation.md` run is dispatched
through it.

## Problem

Both verified against main @ `4770e3c` (2026-07-23):

1. **#797 — env scanner eats workflow templates.** `bobi/config.py:18` defines
   `_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")`. Against the workflow-engine
   template syntax `${{input.title}}` (legitimate in `agent.yaml`
   `auto_dispatch` `task:` templates) it matches `${{input.title}` and
   captures `{input.title`. Two call sites misbehave:
   - `bobi/config.py:128` (`_scan_env_refs`, reached via `find_env_var_refs`
     — the entry point `bobi agents install` actually calls at
     `bobi/cli.py:720` — and `find_required_env_vars`) reports bogus
     "required secrets" like `{input.title`, breaking
     `bobi agents install --non-interactive`.
   - `bobi/config.py:167` (`_ENV_VAR_RE.sub(_resolve, value)`) rewrites the
     template before the workflow engine ever sees it. This bites at runtime
     too: `Config._parse` interpolates the whole `agent.yaml`
     (`bobi/config.py:461`) and `auto_dispatch` is read from the
     **interpolated** dict (`config.py:514` — it is not in the
     verbatim-preserved set monitors/requires/host/build), so a
     `${{input.title}}` task template collapses to a stray `}` before the
     reactor's own template resolver (`bobi/events/reactor.py:282-287`) runs.
     This is where the two lanes meet: an agent.yaml exercising #796's
     `role:` with a `${{input.*}}` task template needs BOTH fixes — see the
     convergence gate.

2. **#796 — auto_dispatch forces `role="engineer"`, disabling per-step agent
   switching.** `bobi/events/reactor.py:264` — `_dispatch` passes
   `role="engineer"` unconditionally for every `AutoDispatchRule`.
   `bobi/workflow/orchestrator.py:778-779` —
   `next_agent = current_agent if role else (step.agent or current_agent)`:
   any truthy forced role short-circuits step `agent:` resolution for the
   whole run. Net effect: every step of an auto-dispatched workflow runs as
   `engineer` regardless of the workflow's per-step `agent:` declarations,
   while the same workflow launched via `subagents launch -w …` (no forced
   role) switches correctly *when the dials change*.

   **Scope caveat (verified, stated so #796's closure is honest):** per-step
   agent switching lives entirely inside the model/effort-change branch —
   `orchestrator.py:764` (`if step_model != current_model or step_effort !=
   current_effort:`) guards lines 778-779, and the in-code comment at 768-777
   admits "an agent change with identical dials never enters the branch - a
   pre-existing gap". This plan delivers **parity with `subagents launch`**
   (removing the reactor-side hardcode), NOT full per-step switching: a
   multi-role workflow whose roles resolve to identical model+effort still
   won't switch agents, under either launch path. The #796 closure comment
   must scope the fix accordingly; closing the dial-gap itself is out of
   scope (orchestrator design work, not a reactor bug).

Both issues carry partner-tested proposed patches ("running in our prototype
since 2026-07-15"); the issue bodies are the reference, the fix is re-derived
and tested here.

## Solution

Two independent, minimal fixes — no new modules, no seam changes:

1. **#797**: negative lookahead in the env-reference regex —
   `\$\{(?!\{)([^}]+)\}` — so `${{` is never treated as an env reference.
   One shared regex fixes both call sites (scan and interpolation) at once.
   Alternative rejected: escaping/pre-masking templates before scanning —
   more code, two passes, same result.

2. **#796**: optional per-rule `role:` field on `AutoDispatchRule`, default
   `"engineer"` (today's behavior, zero back-compat risk). A falsy value
   (`role: ""` or YAML `role:` → null) defers to each workflow step's
   `agent:` resolution — exactly the semantics the orchestrator's existing
   truthiness check already implements; the reactor just stops hardcoding.
   The parser normalizes: `entry.get("role", "engineer") or ""` — absent →
   `"engineer"`, and both falsy spellings collapse to `""` so `None` never
   leaks into the `str`-typed field, the spawn JSON, `SessionEntry.role`, or
   the spend rollup key (`costs.py` folds `data.get("role", ...)` — an
   unnormalized YAML null would surface as literal `None`/`null` in
   `bobi spend` by-role output).
   Alternative rejected: flipping the default to defer — silently changes
   behavior for every existing rule.

## Relevant files

### Existing (verified 2026-07-23)

- `bobi/config.py` — `_ENV_VAR_RE` (line 18) and its two call sites (128, 167).
- `bobi/events/reactor.py` — `AutoDispatchRule` dataclass (line 27), rule
  parsing (~line 156-165), `_dispatch` role hardcode (line 264).
- `bobi/workflow/orchestrator.py` — read-only reference: the role truthiness
  short-circuit (lines 778-779) that gives falsy `role` its meaning, and the
  model/effort-change guard at line 764 that bounds it (the dial-gap caveat).
  Not modified by this plan.
- `tests/test_orchestrator.py` — read-only reference: lines 856 and 961
  already pin the truthy-blocks / falsy-switches orchestrator semantics.
- `tests/test_config.py` — home of `find_required_env_vars` tests (line 440+).
- `tests/test_reactor.py` — home of auto_dispatch rule tests.

### New

None. Both fixes land in existing files and existing test modules.

## Questionables

- **Q1:** How should a rule spell "defer to step `agent:` resolution" in
  #796? Options: (a) any falsy `role:` value — empty string per the issue's
  tested patch, YAML `role:` (null) equally accepted, both normalized to
  `""` at parse (`entry.get("role", "engineer") or ""`), documented as
  `role: ""` / (b) a distinct sentinel like `role: steps` or a new boolean
  `respect_step_agents: true`. Recommendation: (a) — it matches the
  orchestrator's existing truthiness semantics with zero new concepts, and
  matches the patch the partner has run live since 2026-07-15; (b) adds a
  concept to teach for no added expressive power.
  **Decision (2026-07-23, Zach):** (a) — falsy defers, both spellings
  normalized to `""` at parse; tests assert the stored value.

- **Q2:** The lookahead uncovers `${VAR}` refs *nested inside* `${{…}}`
  regions that the old bogus match swallowed whole: `${{ ${SECRET} }}` now
  resolves `${SECRET}` (empirically verified against both regexes). Options:
  (a) pin this as intended — env interpolation applies everywhere, template
  regions are not exempt; one test documents it / (b) exclude refs inside
  `${{…}}` spans — more code (region tracking) for a pathological authoring
  pattern. Recommendation: (a) — the value is pack-author-controlled (an
  event author cannot inject agent.yaml text), the semantics are simpler to
  state, and (b) buys safety nobody has asked for at real complexity cost.
  **Decision (2026-07-23, Zach):** (a) — pin as intended; env interpolation
  applies everywhere, one test documents the nested-ref behavior.

## Phases

Phases 1 and 2 are fully parallel lanes at the file level: disjoint source
files, disjoint test modules, no landing-order constraint in either direction.
They are NOT semantically disjoint — the partner's actual use case (a
`${{input.*}}` task template on a role-deferring rule) needs both fixes,
because `Config._parse` interpolates `auto_dispatch` before the reactor reads
it. Neither lane alone resolves that combined scenario; the convergence gate's
combined-lane check is what proves it, and neither issue-closure comment
should imply the combined case works until both lanes have landed.

### Phase 1 — #797 env scanner vs `${{…}}` templates (Lane A)

- [x] Failing tests first in `tests/test_config.py`: (1) an `agent.yaml`
  containing `auto_dispatch` `task: "… ${{input.title}} … ${{input.severity}} …"`
  yields NO env refs for the template fields from the scan entry points
  (`find_env_var_refs` — the one `bobi agents install` calls — and
  `find_required_env_vars`), while a real `${VAR}` in the same file is still
  found (fixture pattern: copy `test_find_required_env_vars` at
  `tests/test_config.py:440` — `tmp_path / "package" / "agent.yaml"`, raw
  dedent'd text; the scan is a raw-text regex, no valid full pack needed);
  (2) interpolation leaves `${{input.title}}` byte-identical while resolving
  an adjacent `${VAR}` — assert via `Config.load` that
  `cfg.auto_dispatch[0]["task"]` survives untouched (this pins the runtime
  path, not just the scan); (3) per the Q2 decision: one test pinning the
  nested-ref behavior (`${{ ${VAR} }}`).
- [x] Fix: `_ENV_VAR_RE = re.compile(r"\$\{(?!\{)([^}]+)\}")` in
  `bobi/config.py` with a comment stating why the lookahead exists (workflow
  template syntax must survive the scan and interpolation untouched).
- [x] Close #797 via the PR ("Fixes #797").

**Validation gate**

- [x] New tests fail before the fix, pass after (both states shown in the PR).
- [x] `pytest tests/test_config.py -q`
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`

### Phase 2 — #796 auto_dispatch per-rule `role:` (Lane B)

- [ ] Failing tests first in `tests/test_reactor.py`: (1) a rule with no
  `role:` key dispatches with `role="engineer"` (today's behavior, pinned);
  (2) a rule with `role: ""` AND one with YAML null both dispatch with role
  exactly `== ""` (assert the stored/passed value, not just falsiness — this
  pins the normalization); (3) an explicit `role: director` dispatches with
  that role. Test pattern: copy `test_dispatches_on_matching_event`
  (`tests/test_reactor.py:291-304`) — `@patch("bobi.subagent.launch_agent")`
  plus the module-level `_wait_calls` helper (dispatch runs on a daemon
  thread), then assert `mock_launch.call_args[1]["role"]`. Parsing tests
  copy `TestFromConfig` patterns (~line 640). Do NOT add orchestrator tests:
  the truthy-blocks / falsy-switches semantics are already pinned by
  `test_model_change_preserves_explicit_role` (tests/test_orchestrator.py:856)
  and `test_agent_change_at_model_switch_starts_fresh` (:961).
- [ ] Fix per the Q1 decision: `role: str = "engineer"` field on
  `AutoDispatchRule` with a comment on the falsy-defers semantics AND the
  dial-gap caveat; rule parsing reads `entry.get("role", "engineer") or ""`;
  `_dispatch` passes `role=rule.role` instead of the hardcode (reactor.py:264
  is the only `role="engineer"` site in `bobi/` — no other sites to touch).
- [ ] Close #796 via the PR ("Fixes #796"), with the closure comment scoping
  the fix per the Problem section's caveat: parity with `subagents launch`;
  identical-dial agent switching is a pre-existing orchestrator gap this fix
  does not close.

**Validation gate**

- [ ] New tests fail before the fix, pass after (both states shown in the PR).
- [ ] `pytest tests/test_reactor.py -q`
- [ ] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`

### Convergence gate (deferred — after both lanes merge)

- [ ] Full unit run green on merged main:
  `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`
- [ ] #797 repro re-run against merged main: call
  `bobi.config.find_env_var_refs(project_path)` (the exact entry point
  `bobi agents install` uses, `bobi/cli.py:720`) on a dir containing
  `package/agent.yaml` with the issue's repro yaml; assert no `{input.*`
  names appear.
- [ ] Combined-lane check (the seam where the two fixes meet — this is the
  partner's actual use case): `Config.load` on an agent.yaml whose
  `auto_dispatch` rule carries BOTH `role: ""` and a
  `task: "… ${{input.title}} …"` template; assert the parsed rule's task
  text is byte-identical and its role is `""`. May be a script or a test
  added on main — either way, run on the merged tree.

## Proof of work

Both are bugs: failing-test-first is mandatory, and each PR shows the
red→green transition — concretely: run the new tests at the pre-fix commit
and paste the red output in the PR body (the repo's convention per merged
bug-fix PRs is a narrative claim; this plan asks for the paste).
Suites that must stay green: the unit run
(`pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`).
Real-Claude e2e judgment call (per CLAUDE.md): **no claude leg** — both fixes
are brain-agnostic (install-time config scanning; reactor dispatch plumbing
whose role semantics are proven at the orchestrator unit seam); the stub/unit
surface is where the risk lives. Neither PR touches `VERSION`, `pyproject.toml`
version, or `CHANGELOG.md`.

## Ticket map

The lane tickets are the partner-filed issues themselves — they stay open as
the dispatch issues (Ready-gated before dispatch) so the reporter keeps
visibility; no new lane issues are filed. This is a recorded deviation from
`docs/TICKETING_POLICY.md` §1a (plan-born dispatch issues normally carry the
`[<slug>]` bracket prefix and a thin plan-pointer body): the partner-filed
titles and bodies stay as-is, and the retrofit at Ready-gate time is a
pointer comment on each issue naming its lane and
`plans/design-partner-bug-batch.md`, so the builder and the board can route
without retitling the reporter's issues.

| Phase | Ticket | One-line scope | Status |
|---|---|---|---|
| 1 | #797 | env-scanner negative lookahead + failing-first tests | open |
| 2 | #796 | per-rule `role:` on AutoDispatchRule + failing-first tests | open |

**Lanes:** Lane A: #797. Lane B: #796. Fully parallel — disjoint files,
disjoint test modules, no landing-order constraint.

## Amendments

*(append-only)*

## Notes

- Issue bodies: https://github.com/moda-labs/bobi-agent/issues/797 and
  https://github.com/moda-labs/bobi-agent/issues/796 — both include repros and
  partner-tested patches.
- **Docs**: no doc surface documents `auto_dispatch` rule fields today (only
  prose mentions at `docs/OVERVIEW.md:102` / `docs/EVENT_SERVER.md:91`;
  `skills/create-agent.md` never covers it) — so per "update the affected
  docs in the same PR" there is nothing affected to update; builders should
  not hunt for or invent a doc surface. Documenting `auto_dispatch` keys
  (candidate home: `skills/create-agent.md`) is deferred follow-up work, not
  a lane deliverable.
- **bobi-deploy blast radius (#797)**: `scan_required_vars`/`scan_declared_vars`
  (`bobi/config.py:135/146`) share `_ENV_VAR_RE` and have zero callers in
  this repo — their consumers are bobi-deploy's `deploy.py` and
  `scaffold.py`, where `scan_declared_vars` is the secret prune/env-file
  filter authority. Verified safe direction (the fix only removes
  always-garbage `{input.*`-style names, never a real `${VAR}` ref), but
  deploy-side env filtering behavior changes on the next pin bump — one
  deploy-path smoke run is warranted at that time.
- Adjacent pre-existing oddity, out of scope: `defaults.role` is parsed
  (`config.py:510`) but has zero consumers — a role-deferring pack author
  might expect it to backfill; it doesn't.
- bobi-deploy#32 (same reporter, container entrypoint auth gate) is
  out of scope: different repo, single-unit work per house convention —
  handled as a normal issue dispatch, not a plan lane.
- The `plan-execute` verification context: moda-agents
  `agents/moda-eng-team/workflows/plan-execute.yaml`; moda-skills
  `plans/execute-bot-orchestration.md` (convergence gate = first real
  multi-lane bot-native execute run).
