# Spec: Registry-based agent-team install + deploy — versioned per-team packages (`name@version`)

- **Issue:** [#440](https://github.com/moda-labs/bobi-agent/issues/440)
- **Status:** Approved by reviewer (`@underminedsk`, 2026-06-23, comment on PR #441
  — "all the recommended options"); §4 design decisions D-1–D-6 now **locked**.
  Implementation is staged/gated separately (§5.2) — Phase 1 first; merge of this
  spec PR is a separate gate, not implied by the comment approval.
- **Author:** eng-team (bot)
- **Type:** feature (medium→large, operationally sensitive — reshapes how every
  agent team, *including our own director/lead fleet*, is packaged, versioned,
  installed, and deployed)

> This spec is a **superset** of issue #440. The issue body (Summary, Why,
> Design Phases 1–3, Acceptance criteria, Out-of-scope, Key file anchors) is
> authoritative for intent and is reproduced/expanded here. Sections **§6
> (Backward-compat & rollout)** and **§7 (Coordination with #436/#438/#439)**
> are net-new and are the load-bearing additions the spec gate exists to review.

---

## 1. Problem & Solution

### Problem

Agent-team distribution today has **two weak, divergent paths**, and neither
honors a version:

1. **Install clones the whole repo at `main`, ignoring versions.**
   `registry.fetch()` (`bobi/registry.py:160`) downloads
   `https://api.github.com/repos/{repo}/tarball/main` — the *entire* repo at
   whatever `main` is right now — and extracts one `agents/<name>/` subdir. There
   is no `version` parameter. `registry.yaml` carries a `version:` per team that
   the download never honors. `bobi agents install eng-team` gives you "whatever is
   on main this second" — unpinnable and irreproducible.

2. **Deploy can't consume a registry team as a first-class team.**
   `DeployConfig.delivery` (`bobi/deploy.py:108`) has exactly two modes:
   - local `team:` → on-disk package → **team-flavored** image (bakes `build:`
     deps), `ssh-push` delivery, secret **prune** (the package's `${VAR}` refs
     are the prune authority).
   - `team_url:` → generic image, team fetched at boot → **no baked deps, no
     prune** (`_secret_sets` returns `declared=None` at `deploy.py:399`).

   A team with real build deps (eng-team bakes codex/bun/playwright via its
   `build:` block) **cannot** use `team_url:` without losing its tooling. So our
   own fleet can only deploy from a checked-in local copy or a CI repo-clone hack.

3. **The good artifact already exists but nothing consumes it.**
   `.github/workflows/team-packages.yml` + `scripts/build-team-tarballs.sh`
   already build clean **per-team** `<team>.tar.gz` and publish them — but only
   to a **rolling, mutable** `teams-latest` release (`--clobber`), and
   `registry.fetch` never uses them.

### Solution

Make a **versioned, immutable, per-team package** the unit of distribution. A
team version is published **once** as `…/teams-latest/<team>-<version>.tar.gz`;
both `install` and `deploy` resolve `name@version` to that asset. Deploy fetches
the pinned asset **to disk** and then reuses the **existing** local-team build
path unchanged — team-flavored image + ssh-push + prune all keep working,
because after fetch the package looks exactly like a local one.

The elegant property we exploit: **deploy already funnels every team through one
resolver, `local_package_dir()` (`deploy.py:341`)** — called by the secret-prune
scan, the deps-render, the deps-hash, and the build. If we make that one seam
understand `name@version` (fetch-to-cache when not local), all four consumers
inherit registry deploy for free, with no behavioral change to the local path.

---

## 2. Scope

### In scope

- **Phase 1 — versioned, immutable publishing.** `team-packages.yml` +
  `build-team-tarballs.sh` also publish `<team>-<version>.tar.gz` (version read
  from each team's `agent.yaml`), uploaded **only if not already present**
  (immutability). Rolling `<team>.tar.gz` stays (floating consumers like
  smoke-team). CI check: `agent.yaml` version == `registry.yaml` version.
- **Phase 2 — versioned fetch + install.** `registry.fetch(..., version=…)`
  downloads only the per-team versioned asset (not the repo tarball); cache keyed
  by `name@version`; meta pins the version + asset source; graceful fallback to
  the whole-repo path only when the asset is absent. CLI accepts `name[@version]`
  for `install` and `agents update`.
- **Phase 3 — deploy a registry team first-class.** `team:` accepts an optional
  `@version`; resolution prefers a pinned fetch, else a local dir, else latest
  fetch; downstream build/prune/deps path unchanged. `team_url:` unchanged.
- Tests-first for every codepath (per CLAUDE.md), including the regression guards
  that protect the local and `team_url:` paths.

### Out of scope (explicit — from the issue)

- **`FROM <team>` inheritance / "extend" tier** — a derived team checking in only
  its deltas atop a base. Separate future feature.
- **Re-bundling teams into the wheel** — intentionally NOT doing. Offline
  `bobi setup` templates are deprioritized; **PR #438 de-bundles all teams
  and that stays** (see §7).
- **Changing the rolling-release mechanism** for floating consumers — the rolling
  `<team>.tar.gz` remains exactly as today.
- **A public starter-registry** for `pip install` users behind a private
  `moda-labs/bobi-agent` — flagged by #438 as a possible follow-up; not this
  ticket (noted in §7 as a known gap).
- **Version-bump / changelog edits** — none in this PR (release-time only, per
  CLAUDE.md Contributing).

---

## 3. Technical Approach

### 3.1 Phase 1 — versioned, immutable publishing

**`scripts/build-team-tarballs.sh`**
- Keep producing the rolling `<team>.tar.gz` (unchanged shape: extracts to a
  single `<team>/` holding `agent.yaml`).
- **Also** produce `<team>-<version>.tar.gz`. The byte content is identical to the
  rolling tarball — only the filename differs (build `<team>.tar.gz` once, then
  `cp` to the versioned name, so the immutable asset matches what the rolling one
  served at publish time; the script already builds reproducibly with
  `--sort=name --owner=0 --group=0`).
- **Where the version is read (review fix):** parsing `agent.yaml` `version:` in
  bash is brittle. Do the version read in the **workflow step / a small Python
  helper**, not in `build-team-tarballs.sh` — pass the resolved `<version>` into
  the script (or rename after), keeping the bash script dumb. (`agent.yaml` is the
  source of the version; `registry.yaml` is the "latest published" pointer that
  must agree — see the CI check below.)
- A team with no `version:` in `agent.yaml` → log a clear warning and publish
  **only** the rolling tarball (no versioned asset). Not a hard failure (keeps
  smoke-team / version-less fixtures working).

**`.github/workflows/team-packages.yml`** (publish job, push-to-main only)
- Upload rolling `<team>.tar.gz` with `--clobber` (as today).
- Upload `<team>-<version>.tar.gz` **without `--clobber`**.
  - **Immutability must be fail-closed, not check-then-act (review fix).** Do
    **not** gate on a prior `gh release view` of the asset list — two concurrent
    main pushes (or a re-run) can both observe "absent" and race. Instead, attempt
    the upload **without `--clobber`** and treat the resulting "asset already
    exists" error (HTTP 422) as the **no-op success** path (log the skip and
    continue); any other error is a real failure. This makes the immutability
    invariant a property of the upload itself, not of a TOCTOU window.
  - Immutability invariant: **a published `<team>-<version>.tar.gz` is never
    overwritten.** Re-running the workflow for an unchanged version is a no-op.
  - Surface a clear, greppable log line per skip:
    `skip: eng-team-<version>.tar.gz already published (immutable)`.
- URL convention unchanged in shape:
  `https://github.com/<owner>/<repo>/releases/download/teams-latest/<team>-<version>.tar.gz`.

**CI version-agreement check** (PR + push): for each team, assert
`agents/<team>/agent.yaml` `version` == `agents/registry.yaml` `agents.<team>.version`.
A mismatch fails CI with a message naming the team and the two values. This makes
`registry.yaml` the authoritative "latest published" pointer and forces authors
to bump both together. (Implemented as a small step in `team-packages.yml`, or a
pytest in `tests/test_packaging.py` — see §7 for the #438 collision note; we lean
to the **workflow step** to avoid touching `test_packaging.py` while #438 is open.)

> **⚠️ Pre-req migration (review caught this — do FIRST in Phase 1).** The two
> sources **already disagree on `main`**: `agents/registry.yaml` has
> `eng-team: 1.0.0` but `agents/eng-team/agent.yaml` has `version: "1.1.0"`. The
> agreement check would fail on day one. **Step 0 of Phase 1** is to reconcile
> them (pick the correct published version — almost certainly bump
> `registry.yaml` eng-team → `1.1.0`) in the same PR that adds the check, and
> audit the other teams (`dogfood-content-review`, `market-research`,
> `support-manager`) the same way. All `@version` examples in this spec use
> `eng-team@1.1.0` on the assumption that reconciliation lands `1.1.0`.

### 3.2 Phase 2 — versioned fetch + install

**`bobi/registry.py` — `fetch(project_path, name, version=None, repo=None)`**

New signature (additive, keyword-only `version` defaulting to `None` → existing
callers unaffected):

- **`version` given** → resolve the per-team asset URL
  `https://github.com/{repo}/releases/download/teams-latest/{name}-{version}.tar.gz`
  and download **only that tarball**. Extract via the existing safe
  `_install_team_tar` machinery (the tarball already extracts to a single
  `<name>/` holding `agent.yaml` — same contract as `fetch_from_url`). This means
  the pinned path reuses the hardened extraction (`_safe_members`, `data` filter)
  we already trust for URL installs.
  - **Download must be token-authed (review fix).** Do **not** route the asset
    download through `fetch_from_url`, whose `pooled.get` is **un-authenticated** —
    that 404s on a private `moda-labs/bobi-agent`. Download the asset bytes via the
    token-aware `_urlopen` (used everywhere else in `registry.py`), then hand the
    bytes to the shared `_install_team_tar` core. (Refactor: split
    `fetch_from_url` so the extraction core is callable with pre-fetched bytes.)
- **`version` None** → read the latest version from the registry
  (`_read_remote_version` / `registry.yaml`) and fetch **that versioned asset**
  (not the repo tarball). So even "latest" becomes a clean per-team download.
  - **This is an intentional, non-silent behavior change** for unpinned
    `install`/`agents update` (review flagged it): today they clone `tarball/main`;
    after Phase 2 they pull the per-team latest asset. It is guarded by the
    fallback below and surfaced in a log line. It is **not** "free" — floating
    consumers now depend on the team-packages CI having published an asset for the
    current `registry.yaml` version (§5.2 step 2 calls this out honestly).
- **Fallback** (logged `warning`): only if the release asset 404s (older repos /
  a team published before Phase 1, or a `registry.yaml` version with no asset yet),
  fall back to the current whole-repo `tarball/main` path. The versioned asset is
  the **primary** path; the repo clone is the safety net. **Exception:** an
  **explicit `@version`** that 404s is a **hard error**, never a fallback — a
  caller that pinned must not silently get something else (see §3.3 precedence).
- **Repo resolution** unchanged: search `_all_registries` for the one that knows
  `name` when `repo` is not given.

**Cache & meta**
- Cache key becomes **`name@version`**. **Per the locked D-1=(a) (§4):** cache
  pinned versions at `_cache_dir / name` **with `.meta.json` recording the pinned
  version** (the installed-pack layout stays `<cache>/<name>/` so resolver/install
  code is unchanged); re-fetching a pinned version overwrites `<cache>/<name>` and
  re-pins meta. **No `.versions` sidecar** — the immutable upstream asset already
  guarantees re-fetch determinism, so the sidecar's marginal offline / multi-version
  benefit doesn't justify the extra copy logic. The rolling "latest" re-validates
  against `registry.yaml` on each `agents update`.
- `_write_meta` records `version` (the resolved concrete version, never
  `"unknown"` when pinned) and `source` (the asset URL).

**CLI** (`bobi/cli.py`)
- `bobi agents install <name>[@version]` (`cli.py:568`): parse a trailing
  `@version` off the registry-name branch (the URL / local-archive / local-dir
  branches are unaffected — an `@` only has meaning on the bare-name branch).
  Pass `version` through to `fetch`.
- `bobi agents update <name>[@version]`: same parse; `@version` pins,
  bare name takes latest. `agents update` with no version keeps today's
  "update to latest remote" behavior.
- Helpful errors: an unknown `name@version` (asset 404 and no fallback) reports
  the team, the version, and the URL it tried.

### 3.3 Phase 3 — deploy a registry team first-class (team-flavored)

**`bobi/deploy.py`** — the change is deliberately concentrated in **one
resolver** so every consumer inherits it.

- `DeployConfig.team` may carry `@version` (e.g. `team: eng-team@1.1.0`). Add a
  parsed helper, e.g. `team_name` / `team_version` properties that split on the
  last `@` (a bare `team:` keeps `team_version=None`).
- **Add a new wrapper `resolve_team_dir(project_path, team)`** that does the
  `name@version` split + fetch, and **switch every current `local_package_dir`
  caller to it** (locked: D-2=(b), see §4). Keep
  `local_package_dir` as the pure local-only primitive. Resolution order for
  `team: <name>[@<version>]`:
  1. `<version>` present → `registry.fetch(project_path, name, version=version)`
     into the deploy cache; use that dir. **An explicit `@version` NEVER falls
     back to a local dir** (review fix) — a pin means the pin; a 404 is a hard,
     clear error (team + version + URL). This prevents a stale local
     `agents/<name>` checkout from silently shadowing a requested pin.
  2. (bare name, no `@version`) a local `agents/<name>` / `<name>` dir exists →
     use it (**today's behavior, unchanged** — local dev keeps working).
  3. else (bare name, no local dir) → `registry.fetch(project_path, name)`
     (latest) into the cache; use that dir.
- **⚠️ Wire ALL call sites, not four (review caught a missed one).**
  `local_package_dir` / `resolve_team_dir` is called from **five** places; the
  spec's "four consumers" undercounted. The fifth is the one that actually ships
  the team:
  - `_secret_sets` (`deploy.py:401`) — prune authority.
  - `_render_team_deps_into_context` (`deploy.py:801`) — team-flavored deps bake.
  - `_local_team_deps_hash` (`deploy.py:828`) — #379 rebuild-on-drift guard.
  - **`deploy()` body (`deploy.py:996`, `pkg = local_package_dir(...)`)** — the
    **ssh-push** source that tarballs + pushes the package to the instance. Miss
    this and a pinned team would build/prune correctly but **ship the wrong (or
    absent) directory.** This is exactly why D-2b (one wrapper, switch all callers)
    is safer than editing `local_package_dir` in place and hoping every site is
    covered.
- **Why this is the whole job:** after resolution the package is **on disk**, so
  the existing path runs unchanged and *correctly*:
  - `_render_team_deps_into_context` (`deploy.py:790`) reads the fetched
    `agent.yaml` `build:` → **team-flavored** image, deps baked.
  - `delivery` stays `"ssh-push"` (it keys off `bool(self.team)`, still truthy).
  - `_secret_sets` (`deploy.py:383`) scans the fetched `agent.yaml` →
    `declared` is **non-None** → **secret prune works**.
  - `_local_team_deps_hash` (`deploy.py:819`) hashes the fetched `build:` → the
    #379 rebuild-on-deps-drift guard works for registry teams too.
  - `deploy()` (`deploy.py:996`) tarballs the resolved dir for ssh-push → the
    pinned package is what actually lands on the instance.
- **`team_url:` stays exactly as-is** — the dependency-free boot-fetch path
  (smoke-team). `validate()` still enforces exactly one of `team:` / `team_url:`.
- **Reproducibility:** a pinned `name@version` is an immutable tarball → stable
  `render-team-deps.py` deps-hash → no spurious image rebuild on redeploy of the
  same pin. A **latest** (unpinned) deploy can legitimately rebuild if the team
  republished — desired, and already covered by the #379 deps-hash guard.

### 3.4 Caching location for deploy fetches

Deploy resolution fetches into the **project cache** (`paths.agents_dir`), the
same place `install` populates, so a machine that has run `bobi agents install
eng-team@1.1.0` and then `bobi deploy` reuses the cached package with no
second download. (D-3 in §4: an isolated deploy-only cache vs. the shared install
cache. Recommend shared — fewer moving parts, and the immutable pin makes
sharing safe.)

---

## 4. Design decisions (RESOLVED)

> **Resolved 2026-06-23** — approved by **@underminedsk** on PR #441:
> *"approved with all the recommended options."* Every decision below is locked
> to its **recommended option**; implementation builds these, no further
> reviewer input needed.

Each had a recommended default; the reviewer accepted all of them.

- **D-1 — multi-version cache layout.** → **RESOLVED: (a)** Overwrite
  `<cache>/<name>` + pin meta (simplest) — the immutable upstream already
  guarantees re-fetch determinism, so the sidecar's marginal offline benefit did
  not justify the copy logic. (Option (b), the immutable
  `<cache>/.versions/<name>@<version>` sidecar, is dropped; the original spec had
  leaned (b) but the reviewer picked the review-recommended (a).)
- **D-2 — resolver shape.** → **RESOLVED: (b)** Add `resolve_team_dir()` that does
  name@version + fetch, and have **all five** callers (incl. the `deploy()`-body
  ssh-push site at `:996`) use it; keep `local_package_dir` as the pure-local
  primitive. (Option (a), extending `local_package_dir` in place, is dropped — it
  risked missing a call site and shipping the wrong dir; (b) makes coverage
  explicit and testable.)
- **D-3 — deploy cache.** → **RESOLVED: (a)** Shared with the install cache.
- **D-4 — version-agreement check home.** → **RESOLVED: (a)** A step in
  `team-packages.yml` (avoids editing `tests/test_packaging.py` while #438 is
  open — see §7). Optionally migrate to a pytest in `test_packaging.py` after
  #438 lands.
- **D-5 — version-less teams.** → **RESOLVED: yes.** A team with no `agent.yaml`
  `version:` publishes only the rolling tarball and is fetchable only as "latest"
  (no pinned asset) — keeps smoke-team / fixtures working.
- **D-6 — `@version` parse rule.** → **RESOLVED: as stated.** Split on the
  **last** `@`; `@` is meaningful only on the registry-name branch of `install`
  (not for URLs / paths / local archives) and on `deploy`'s `team:`.

---

## 5. Backward-compatibility & rollout (load-bearing — must not break running fleets)

This reshapes how **our own director/lead fleet** is packaged and deployed, so
the rollout is staged so that **no currently-running instance changes behavior
until we deliberately re-pin it.**

### 5.1 Backward-compat guarantees

- **`registry.fetch` is signature-additive.** `version` is keyword-only,
  defaulting to `None`. Every existing caller keeps compiling and keeps working;
  the only behavioral change at `version=None` is "download the per-team latest
  asset instead of the whole repo tarball," with the **whole-repo path retained
  as a logged fallback** when the asset is missing. So a repo that hasn't run
  Phase 1 publishing yet still installs.
- **Local `team:` deploy is byte-for-byte unchanged.** Resolution order tries the
  local dir before any latest-fetch; a checked-in `agents/<name>` still wins.
  Existing `deployments/*.yaml` with bare `team: eng-team` behave exactly as
  today. (Regression test asserts this.)
- **`team_url:` is untouched.** smoke-team's generic boot-fetch path is preserved
  and guarded by a regression test.
- **Rolling assets stay.** `<team>.tar.gz` continues to be clobbered on each
  main push, so any floating consumer keeps resolving.
- **No config migration required.** `@version` is purely opt-in. Existing
  `deployments/*.yaml`, `registry.yaml`, and `agent.yaml` files are valid
  unchanged.

### 5.2 Rollout order (so a running fleet never silently shifts)

1. **Land Phase 1 only** (publishing). Effect: versioned assets start appearing
   on `teams-latest`. **No consumer reads them yet** → zero runtime change to any
   running instance. Verify a couple of `<team>-<version>.tar.gz` assets exist and
   are immutable (re-run is a no-op).
2. **Land Phase 2** (fetch/install). Effect: `bobi agents install` and `agents
   update` *can* pin, and unpinned install now pulls the per-team latest asset.
   Running deployed instances are **unaffected** (they don't re-install
   themselves). A developer's `install eng-team` now resolves the published latest
   asset rather than raw `main`. **Honest caveat (review):** this is a real
   behavior change, not free — an unpinned install now depends on team-packages CI
   having published an asset for the current `registry.yaml` version; if it hasn't
   (e.g. a just-merged version bump before the publish job runs), the repo-tarball
   fallback covers it with a logged warning. Acceptable, but not "silent and
   strictly better."
3. **Land Phase 3** (deploy). Effect: `team: <name>@<version>` becomes
   deployable. **Critically, this is inert until someone edits a
   `deployments/*.yaml` to add `@version` or removes the local copy.** A reconcile
   of the existing fleet (still bare `team: eng-team` with a checked-in copy)
   takes the unchanged local path.
4. **Migrate our own fleet deliberately, one instance at a time.** Once Phase 3
   is proven on a canary/smoke deploy, switch the eng-team fleet
   (`moda-agents`) to a pinned `team: eng-team@<version>` in a separate,
   reviewed change — **not part of this PR.** Because the pin is immutable, the
   first pinned deploy rebuilds the team-flavored image once (deps-hash may
   differ from the checked-in copy's), then stabilizes. Roll canary → prod,
   watch the canary `CANARY-OK` gate (the existing `release.yml` gate), and keep
   the option to revert the YAML to the local copy.

### 5.3 Failure modes & guards

- **Asset missing / 404 on fetch** → logged fallback to repo tarball (install);
  for deploy, a missing pinned asset is a **hard, clear error** (we must not
  silently deploy "latest" when a pin was requested).
- **`registry.yaml` vs `agent.yaml` version drift** → caught by the Phase 1 CI
  check before publish, so the "latest" pointer can't lie.
- **Accidental version reuse** → immutable upload refuses to overwrite; the
  workflow logs a skip rather than mutating a published artifact. (A genuine
  re-cut requires bumping the version — by design.)
- **Private-registry auth** → unchanged from today (`_github_token`); see §7 for
  the #438-raised public-starter-registry gap (out of scope, flagged).
- **Our-fleet self-deploy safety** → because §5.2 step 4 is a separate change,
  the very framework session writing/deploying this can't brick itself by
  merging this PR.

---

## 6. Verification plan (tests-first, per CLAUDE.md)

Write each test **failing first**, then implement.

### Phase 1 (publish)
- **Unit (`scripts` / packaging test):** building a team with a `version:`
  produces both `<team>.tar.gz` and `<team>-<version>.tar.gz`, byte-identical
  payload; a version-less team produces only the rolling tarball + a warning.
- **Workflow logic** (extracted into a testable shell/py helper where practical):
  given an existing-assets list, the publish step uploads a missing versioned
  asset and **skips** an already-present one (no `--clobber`), emitting the skip
  log line.
- **CI agreement check:** a fixture where `agent.yaml` and `registry.yaml`
  versions disagree fails; agreement passes.

### Phase 2 (fetch/install)
- **Fetch (pinned):** `registry.fetch(name, version="1.1.0")` (mocked asset)
  downloads **only** `…/teams-latest/<name>-1.1.0.tar.gz` — assert it does **not**
  hit `…/tarball/main`; extracts to cache; meta pinned to `1.1.0`.
- **Fetch (latest):** `registry.fetch(name)` resolves `registry.yaml` version →
  fetches that per-team asset (assert URL), not the repo tarball.
- **Fetch (fallback):** asset 404 → falls back to repo tarball with a logged
  warning; still installs.
- **Install CLI:** `bobi agents install eng-team@1.1.0` populates cache + pins
  meta; `install eng-team` (latest) works; `@version` ignored/irrelevant on URL
  and local-path branches.
- **`agents update name@version`** pins; bare name updates to latest.

### Phase 3 (deploy)
- **Deploy (local unchanged):** `team: eng-team` with a local dir builds
  team-flavored + ssh-push; all existing deploy tests stay green (regression).
- **Deploy (registry pinned):** `team: eng-team@1.1.0` with **no** local dir →
  resolves via mocked `registry.fetch` → image is **team-flavored** (build deps
  present in the rendered deps hook), `delivery == "ssh-push"`, and
  `_secret_sets(...)` returns a **non-None** `declared` (prune enabled).
- **Deploy (ssh-push ships the resolved dir — review fix):** assert the
  `deploy()`-body ssh-push tarball is built from the **fetched** package dir, not
  a stale/absent local one — guards the 5th call site at `:996`.
- **Deploy (explicit pin never falls to local):** `team: eng-team@9.9.9` with a
  local `agents/eng-team` present and a 404 asset → **hard error**, does NOT
  silently use the local dir.
- **Fetch is token-authed (review fix):** pinned asset download sends the GitHub
  token header (so it works against a private repo) — assert it does not route
  through the un-authed `fetch_from_url`/`pooled.get`.
- **Deploy (registry latest):** `team: eng-team` with no local dir and a registry
  hit resolves latest and behaves as team-flavored.
- **Deploy (pinned asset missing):** hard error naming team+version+URL (no
  silent latest).
- **Regression guard:** `team_url:` still yields the generic boot-fetch image;
  `validate()` still rejects both/neither.
- **Reproducibility:** two resolutions of the same pin yield the same deps-hash
  (no spurious rebuild).

### Full-suite gate
- `pytest tests/ --ignore=tests/integration/` green before PR; note any
  pre-existing infra-gap skips (e2e/Playwright, ML-dep KB tests) that fail
  identically on `main`.

---

## 7. Coordination with #436 / #438 / #439 (the deploy/packaging theme)

This ticket is one of a cluster reshaping how teams ship. Explicit interactions:

### #438 — `chore/debundle-agent-teams` (OPEN, human PR by Zach)
- **Touches:** `bobi/setup/open_mode.py`, `pyproject.toml`,
  `tests/test_packaging.py`.
- **This spec touches:** `bobi/registry.py`, `bobi/deploy.py`,
  `bobi/cli.py`, `.github/workflows/team-packages.yml`,
  `scripts/build-team-tarballs.sh`, and **adds** test files.
- **File collision: none.** The only at-risk file is `tests/test_packaging.py`
  (#438 adds `test_no_agent_teams_bundled_in_binary`). **Mitigation:** we put the
  Phase 1 version-agreement check in `team-packages.yml` (D-4a), **not** in
  `test_packaging.py`, while #438 is open. If a packaging test is later wanted, it
  lands in a follow-up after #438 merges.
- **Thematic dependency (important):** #438 makes a published wheel ship **zero**
  teams → `bobi setup` falls back to the **registry**. That makes Phase 2's
  registry-fetch the **primary** acquisition path for `pip install` users — so the
  per-team-asset fetch + fallback robustness in this spec directly de-risks #438.
- **Known gap #438 raised (out of scope, flagged):** if `moda-labs/bobi-agent` is
  private, `pip install` users get no offline teams *and* an auth-walled registry.
  A public starter-registry is a possible follow-up; not this ticket.
- **Sequencing:** independent; either can merge first. If #438 merges first, no
  change here. If this merges first, #438 still applies cleanly (disjoint files).

### #439 — `bobi deploy-init` scaffold (MERGED)
- **Touched:** `docs/DEPLOYMENT.md`, `bobi/cli.py`, `bobi/scaffold.py`,
  `tests/test_scaffold.py`. Already on `main`; this branch is cut from current
  `main` (commit `8226a6c`), so we build on top.
- **Interaction:** `deploy-init` scaffolds a bring-your-own-repo CI that does
  `bobi deploy`. Phase 3 makes the scaffolded deploy able to pin
  `team: <name>@<version>` — i.e. this spec is the natural payoff for the BYOR
  flow #439 set up. The scaffold templates may later want a `@version` example;
  out of scope here (doc/scaffold follow-up).
- **cli.py overlap:** both edit `cli.py`, but different commands (`deploy-init`
  scaffold vs. `install`/`agents update` parsing). Already merged → no live
  conflict.

### #436 — `ci-fleet-rename` / drop eng-team deployment (MERGED)
- Moved the self-gate fleet to `ci` and dropped the in-repo eng-team prod
  deployment; eng-team prod now lives in the standalone **`moda-agents`**
  repo. This spec is what lets `moda-agents` deploy eng-team **by pin** with
  no checked-in copy and no CI repo-clone hack — the motivating case in the
  issue. The §5.2-step-4 fleet migration is the concrete follow-through, done as a
  separate change in `moda-agents`, not here.

### Net: our own fleet
- #436 (where our fleet lives) + #438 (wheel ships no teams) + #439 (BYOR deploy
  scaffold) + **#440 (this — versioned pin)** together move us to: *eng-team
  lives once in `bobi/agents/eng-team`, is published as immutable versioned
  packages, and is deployed from `moda-agents` by `team: eng-team@<version>`.*
  Per §5.2 the migration of the live fleet is **deliberate and out of this PR**,
  so merging #440 cannot by itself redeploy or disturb running director/lead
  sessions.

---

## 8. Implementation plan (suggested ordering)

Phase 1 → 2 → 3 (publish, then fetch/install, then deploy consumes). Each phase
is independently testable; Phase 3 depends on 2. Reasonable as **three stacked
PRs** (preferred — smaller reviews, staged rollout matches §5.2) or one PR with
phased commits. Lead's call at implementation time; this spec covers all three.

1. **Phase 1 PR** — `build-team-tarballs.sh` versioned output;
   `team-packages.yml` immutable upload + version-agreement check; tests.
2. **Phase 2 PR** — `registry.fetch(version=…)` + cache/meta; `cli.py`
   `install`/`agents update` `@version`; tests.
3. **Phase 3 PR** — `deploy.py` `resolve_team_dir()` (name@version + fetch);
   wire the four `local_package_dir` callers; tests.
4. **(separate, not in scope)** Migrate `moda-agents` fleet to a pinned
   `team: eng-team@<version>` (§5.2 step 4).

---

## 9. Key file anchors

- `bobi/registry.py:146` `fetch()`, `:107` `_read_remote_version`,
  `:98` `_write_meta`, `:30` `DEFAULT_REPO`, `:300` `_install_team_tar`
  (reuse for pinned extraction)
- `bobi/deploy.py:76` `DeployConfig` (`team`/`team_url`/`delivery`),
  `:123` `validate()`, `:341` `local_package_dir`, `:383` `_secret_sets`
  (the `declared=None` team-url branch), `:790` `_render_team_deps_into_context`,
  `:819` `_local_team_deps_hash`
- `.github/workflows/team-packages.yml`, `scripts/build-team-tarballs.sh`,
  `scripts/render-team-deps.py`
- `bobi/cli.py:568` `install`, deploy + `agents update` command group
- `agents/registry.yaml` (per-team `version:` = latest-published pointer)

---

## 10. Review log

- **Eng review (code-verified, adversarial):** ran an independent reviewer that
  read the actual `registry.py` / `deploy.py` / `cli.py` / build script / workflow
  and attacked the design. All code anchors verified accurate. Findings folded in:
  the day-one `registry.yaml`(1.0.0) vs `agent.yaml`(1.1.0) version drift (now §3.1
  Step-0 pre-req); the **missed 5th `local_package_dir` call site** at
  `deploy.py:996` ssh-push (now §3.3 + a dedicated test); token-authed asset
  download vs un-authed `fetch_from_url` for private repos (§3.2); fail-closed
  immutable upload instead of check-then-act TOCTOU (§3.1); explicit-pin never
  falls back to local (§3.3); honest framing of the unpinned-install behavior
  change (§5.2); D-1 simpler-default note.
- **Codex adversarial review:** attempted, **could not run** — the `codex` CLI
  returned `401 Unauthorized` (gateway not authed for this instance). Surfaced, not
  worked around. Re-run when codex auth is configured.
- **Design review:** **not applicable** — this is a CLI/packaging/deploy feature
  with **no web frontend or UX surface** (`has_frontend: false`). No design
  dimensions to score.
- **CEO/scope review:** scope matches the issue exactly (three phases, explicit
  out-of-scope list incl. `FROM` inheritance and wheel re-bundling). The one scope
  addition beyond the issue — §5 staged rollout + §7 coordination — is mandated by
  the director (operationally sensitive: reshapes our own fleet) and is
  documentation/sequencing, not extra build surface. Not too wide, not too narrow.
- **Reviewer approval (2026-06-23):** **@underminedsk** approved the spec on
  PR #441 — *"approved with all the recommended options."* §4 D-1…D-6 are now
  locked to their recommended options (see §4). No spec changes were requested.
