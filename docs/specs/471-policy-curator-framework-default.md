# Spec — #471: policy-curator as a framework default monitor (opt-out)

- **Issue:** [moda-labs/modastack#471](https://github.com/moda-labs/modastack/issues/471)
- **Type:** update (framework / compose + monitors)
- **Status:** SPEC — held for Zach's approval. This is a unified spec+impl PR; the spec is posted on the issue for sign-off before merge.
- **Author:** engineer (spec phase)
- **Related:** #460 (curator content that silently missed prod — the motivating failure), #470 (the eng-team@1.1.0 content cutover that finally landed it), #456 (the curator mechanism), #454 (the rotation-wedge failure mode), #446/#451 (`from:` compose + merge), release v0.32.0 (#469)

> This spec is a strict superset of the issue and the locked design comment ([issuecomment-4791052666](https://github.com/moda-labs/modastack/issues/471#issuecomment-4791052666)). Nothing in either is dropped.

---

## 1. Problem

`policy-curator` is **framework infrastructure** — it protects the engine's own context-rotation path from the unbounded-log wedge (#454/#456), a failure mode that threatens *any* long-running team, not a domain behavior. Yet today it is **activated by a team-declared monitor record** (the `policy-curator` entry with `curator: true` in `agents/eng-team/monitors/defaults.yaml`). That makes the wedge protection **opt-in per team**.

This is exactly how #460 silently missed prod (2026-06-24): #460 added the `curator: true` record to `eng-team` content **without bumping the package version**, so the immutably-published `eng-team@1.0.0` never re-published, the live `moda-eng-team` kept composing the stale base, and the curator never ran. Landing it took a full team-content cutover (#470: bump 1.0.0→1.1.0 → publish → re-pin → `rebuild=true`).

If the curator were a **framework default**, none of that pinning would gate it — it would be present the moment the engine shipped, the same way the curator *machinery* (`prompts/curator.md`, the `curator: true` scheduler path, the `## Team Policy` injection) is already framework-provided and automatic.

### Why this isn't a "framework topology opinion"

The "no framework topology opinions" principle is about *domain* behavior (who manages what, which tracker, how work flows). The curator is none of that — it is context hygiene for the engine's own rotation path, the same category as the native check runners (`pr_conflicts`, `disk_free`) the framework already ships. Auto-installing it asserts "the framework keeps its own context healthy," not "your team should be shaped this way."

---

## 2. Locked design decisions

From the issue's design-decisions comment (treated as binding, not re-litigated here):

1. **Opt-out, not opt-in.** `policy-curator` is auto-installed on **every** team as a framework default. A team removes/overrides it via the existing `prune:` mechanism.
2. **Default interval: 6h** (matches what eng-team runs today).
3. **All teams, including single-shot / non-persistent.** No special-casing of team shape in the framework.
4. **Single source of truth.** Once it's a framework default, the explicit `policy-curator` entry is **removed** from `agents/eng-team/monitors/defaults.yaml`. Net effect on the live eng-team must be **neutral**.
5. **`prune:` precedence honored.** A leaf/overlay `prune: { monitors: [policy-curator] }` anywhere in a `from:` chain must still remove the framework default. Covered by a test.

---

## 3. Scope

### In scope

- Seed a framework-default `policy-curator` monitor record into **every** composed team image, at compose time.
- Make the framework default **prunable** (opt-out) and **overridable** (a team's own `policy-curator` record wins on field conflicts, e.g. a custom `interval`).
- Remove the `policy-curator` record from `agents/eng-team/monitors/defaults.yaml`.
- Tests for: new-team-gets-it, prune removes it, `prune:` precedence in a `from:` chain, and the byte-identical neutrality of removing the eng-team entry.

### Out of scope

- The other monitor records in `eng-team/monitors/defaults.yaml` (`pr-conflict-check`, `stale-pr-check`, `disk-free-check`, `team-status-roundup`) stay team-declared. Only `policy-curator` moves to the framework. (Their *check runners* are already framework code; their *records* are team SDLC defaults — a separate decision, not this issue.)
- The curator's runtime behavior (windowing, cap, cursor, `## Team Policy` injection) — unchanged.
- The runtime `MonitorRegistry` read path — unchanged (it already reads the composed `.modastack/monitors/defaults.yaml`, which is where the seed lands).
- No version bump / CHANGELOG edit (feature-PR policy).

---

## 4. Technical approach

### 4.1 Where the seed goes: compose, as a synthetic base layer

`compose()` is the single chokepoint for **every** install and deploy — a team with no `from:` still "composes to a single-layer image" (`cli.py`), and `deploy.py` composes too. The runtime `MonitorRegistry` reads only the composed `.modastack/monitors/defaults.yaml` (`registry.py`, no framework fallback). So seeding at compose time:

- reaches **all** teams (decision 3),
- lands in the one place runtime already reads (no registry change), and
- is naturally subject to `prune:`, which runs **after** the merge against the frozen file (decision 5).

The framework default is modeled as **the most-base layer of the chain** — processed **first**, before any real team layer. This ordering is load-bearing:

- A team's own `policy-curator` record is processed *after* the seed, so it **overlays** the framework default (the deep-merge-by-name path already in `_accumulate_monitors`). A team that sets `interval: 12h` wins — correct override semantics.
- Because the seed is at index 0 of the monitor order, the composed list is `[policy-curator, …team monitors…]` whether or not a team also declares the record. This is what makes removing the eng-team entry a **byte-identical no-op** (decision 4 / acceptance).

### 4.2 The framework default record — single source of truth

A new framework data file, `modastack/monitors/framework_defaults.yaml`, holds the canonical record. Its `policy-curator` entry is a **verbatim copy** of what `eng-team` declares today, so the cutover is byte-neutral:

```yaml
monitors:
  - name: policy-curator
    description: >
      Distill new agent transcripts since the last run into the team's
      policy.md (#456). ...
    interval: 6h
    event: system/policy.updated
    curator: true
```

It ships as package data the same way `modastack/prompts/curator.md` already does (`packages = ["modastack"]` includes non-`.py` files). A path constant is exported from `modastack/monitors/__init__.py` (mirroring `prompts/__init__.py`'s `CURATOR_PATH`).

### 4.3 Compose change

In `_compose_structured_dir`, for the `monitors` surface only, seed the framework records into the accumulator (`monitor_records` / `monitor_order` / `monitor_src`) **before** iterating the chain, labelled `framework`, and force the defaults file to be written even when no team declares any monitors (so a brand-new team still gets `monitors/defaults.yaml` with the curator). Everything downstream — deep-merge-by-name, prune, determinism — is unchanged.

### 4.4 eng-team edit

Delete the `policy-curator` entry (and its now-redundant comment lines) from `agents/eng-team/monitors/defaults.yaml`. The framework seed supplies an identical record, so the live composed set is unchanged.

### 4.5 What is deliberately *not* touched

- `MonitorRegistry`, `scheduler.py` curator path, `curator.py`, `prompts/curator.md` — untouched.
- `prune` logic — untouched; the seed rides the existing path.
- `pyproject.toml` packaging — `packages = ["modastack"]` already ships the new YAML; no change needed (confirmed against how `curator.md` ships).

---

## 5. Verification plan

New tests in `tests/test_compose.py`:

1. **New team gets the curator at 6h.** A team with **no** `monitors/` dir composes to a `monitors/defaults.yaml` containing `policy-curator` with `interval: 6h` and `curator: true`.
2. **Prune opts out.** A team with `prune: { monitors: [policy-curator] }` composes **without** `policy-curator`.
3. **Prune precedence in a `from:` chain.** A leaf that `from:`s a base and prunes `policy-curator` composes without it (the framework default is inherited-most-base, so a leaf prune reaches it).
4. **Team override wins.** A team declaring `policy-curator` with `interval: 12h` composes with `interval: 12h` (the framework default is overlaid, not clobbering).
5. **Byte-identical neutrality (acceptance).** Compose an eng-team-shaped team **with** the `policy-curator` record and **without** it; assert the two `monitors/defaults.yaml` outputs are **byte-identical**. This is the regression guard for decision 4.

Updated existing test:

- `test_prune_drops_inherited` asserts an exact monitor list; update it to reflect the now-always-present framework `policy-curator` (the test's intent — prune drops the inherited `drop-me` — is preserved).

Full gate: `pytest tests/ --ignore=tests/integration/` green; `/review` clean.

### Manual QA

```bash
# New team, no monitors → curator seeded at 6h
python - <<'PY'
from pathlib import Path; import tempfile, yaml
from modastack import compose
# (construct a minimal no-from team, compose, print monitors/defaults.yaml)
PY
```
…plus confirm `agents/eng-team` composed monitors are unchanged vs `main` (the byte-identical test encodes this).

---

## 6. Implementation plan

1. Add `modastack/monitors/framework_defaults.yaml` (verbatim `policy-curator` record).
2. Export `FRAMEWORK_DEFAULTS_PATH` from `modastack/monitors/__init__.py`.
3. Seed it in `compose._compose_structured_dir` (monitors surface, first/base, force-write).
4. Remove the `policy-curator` record from `agents/eng-team/monitors/defaults.yaml`.
5. Add the five tests above; update `test_prune_drops_inherited`.
6. Run the test gate + `/review`; open the unified spec+impl PR against `main`.

---

## 7. Risks & mitigations

- **Every composed team now always has a `monitors/defaults.yaml`.** Intended (every team gets the curator). Harmless for teams that previously had none — it contains exactly the one framework record.
- **Existing tests that assert exact monitor lists.** Only `test_prune_drops_inherited` does; updated. Membership-style assertions (`test_monitors_deep_merge_by_name`) and determinism (`test_compose_is_deterministic`) are unaffected.
- **Drift between the framework record and the removed eng-team record.** The byte-identical neutrality test (5.5) is the guard — if the two ever diverge, it fails loudly.
- **Team override direction.** Seeding the framework record as base (not as a trailing layer) is required so team customizations win; the override test (5.4) guards this.
