# Design-partner bug batch: template scanner + auto_dispatch role

> **Status:** Draft
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
   - `bobi/config.py:128` (`find_env_refs` / the install-time required-env
     scan) reports bogus "required secrets" like `{input.title`, breaking
     `bobi agents install --non-interactive`.
   - `bobi/config.py:167` (`_ENV_VAR_RE.sub(_resolve, value)`) can rewrite the
     template before the workflow engine ever sees it.

2. **#796 — auto_dispatch forces `role="engineer"`, disabling per-step agent
   switching.** `bobi/events/reactor.py:264` — `_dispatch` passes
   `role="engineer"` unconditionally for every `AutoDispatchRule`.
   `bobi/workflow/orchestrator.py:778-779` —
   `next_agent = current_agent if role else (step.agent or current_agent)`:
   any truthy forced role short-circuits step `agent:` resolution for the
   whole run. Net effect: every step of an auto-dispatched workflow runs as
   `engineer` regardless of the workflow's per-step `agent:` declarations,
   while the same workflow launched via `subagents launch -w …` (no forced
   role) switches correctly.

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
   Alternative rejected: flipping the default to defer — silently changes
   behavior for every existing rule.

## Relevant files

### Existing (verified 2026-07-23)

- `bobi/config.py` — `_ENV_VAR_RE` (line 18) and its two call sites (128, 167).
- `bobi/events/reactor.py` — `AutoDispatchRule` dataclass (line 27), rule
  parsing (~line 156-165), `_dispatch` role hardcode (line 264).
- `bobi/workflow/orchestrator.py` — read-only reference: the role truthiness
  short-circuit (lines 778-779) that gives falsy `role` its meaning. Not
  modified by this plan.
- `tests/test_config.py` — home of `find_required_env_vars` tests (line 440+).
- `tests/test_reactor.py` — home of auto_dispatch rule tests.

### New

None. Both fixes land in existing files and existing test modules.

## Questionables

- **Q1:** How should a rule spell "defer to step `agent:` resolution" in
  #796? Options: (a) any falsy `role:` value — empty string per the issue's
  tested patch, with YAML `role:` (null) equally accepted, documented as
  `role: ""` / (b) a distinct sentinel like `role: steps` or a new boolean
  `respect_step_agents: true`. Recommendation: (a) — it matches the
  orchestrator's existing truthiness semantics with zero new concepts, and
  matches the patch the partner has run live since 2026-07-15; (b) adds a
  concept to teach for no added expressive power.

## Phases

Phases 1 and 2 are fully parallel lanes: disjoint source files, disjoint test
modules, no landing-order constraint in either direction.

### Phase 1 — #797 env scanner vs `${{…}}` templates (Lane A)

- [ ] Failing tests first in `tests/test_config.py`: (1) an `agent.yaml`
  containing `auto_dispatch` `task: "… ${{input.title}} … ${{input.severity}} …"`
  yields NO env refs for the template fields from the scan entry point
  (`find_env_refs` / `find_required_env_vars`), while a real `${VAR}` in the
  same file is still found; (2) interpolation leaves `${{input.title}}`
  byte-identical while resolving an adjacent `${VAR}`.
- [ ] Fix: `_ENV_VAR_RE = re.compile(r"\$\{(?!\{)([^}]+)\}")` in
  `bobi/config.py` with a comment stating why the lookahead exists (workflow
  template syntax must survive the scan and interpolation untouched).
- [ ] Close #797 via the PR ("Fixes #797").

**Validation gate**

- [ ] New tests fail before the fix, pass after (both states shown in the PR).
- [ ] `pytest tests/test_config.py -q`
- [ ] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`

### Phase 2 — #796 auto_dispatch per-rule `role:` (Lane B)

- [ ] Failing tests first in `tests/test_reactor.py`: (1) a rule with no
  `role:` key dispatches with `role="engineer"` (today's behavior, pinned);
  (2) a rule with `role: ""` (and one with YAML null) dispatches with a falsy
  role; (3) an explicit `role: director` dispatches with that role.
- [ ] Fix per the Q1 decision: `role: str = "engineer"` field on
  `AutoDispatchRule` with a comment on the falsy-defers semantics; rule
  parsing reads `entry.get("role", "engineer")`; `_dispatch` passes
  `role=rule.role` instead of the hardcode.
- [ ] Close #796 via the PR ("Fixes #796").

**Validation gate**

- [ ] New tests fail before the fix, pass after (both states shown in the PR).
- [ ] `pytest tests/test_reactor.py -q`
- [ ] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`

### Convergence gate (deferred — after both lanes merge)

- [ ] Full unit run green on merged main:
  `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`
- [ ] #797 repro from the issue re-run against merged main: an agent.yaml
  with `${{input.*}}` task templates scans clean (no bogus required env
  vars) via the same entry point `bobi agents install` uses.

## Proof of work

Both are bugs: failing-test-first is mandatory, and each PR shows the
red→green transition. Suites that must stay green: the unit run
(`pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`).
Real-Claude e2e judgment call (per CLAUDE.md): **no claude leg** — both fixes
are brain-agnostic (install-time config scanning; reactor dispatch plumbing
whose role semantics are proven at the orchestrator unit seam); the stub/unit
surface is where the risk lives. Neither PR touches `VERSION`, `pyproject.toml`
version, or `CHANGELOG.md`.

## Ticket map

The lane tickets are the partner-filed issues themselves — they stay open as
the dispatch issues (Ready-gated before dispatch) so the reporter keeps
visibility; no new lane issues are filed.

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
- bobi-deploy#32 (same reporter, container entrypoint auth gate) is
  out of scope: different repo, single-unit work per house convention —
  handled as a normal issue dispatch, not a plan lane.
- The `plan-execute` verification context: moda-agents
  `agents/moda-eng-team/workflows/plan-execute.yaml`; moda-skills
  `plans/execute-bot-orchestration.md` (convergence gate = first real
  multi-lane bot-native execute run).
