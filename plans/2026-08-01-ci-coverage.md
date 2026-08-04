# CI proves the product: both brains, a real Worker deploy, and one fleet canary that gates version rolls

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#909 · **Created:** 2026-08-01 · **Last amended:** 2026-08-01 (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Public `bobi-agent` CI should prove the three things the product is: an agent that runs on a **Claude** brain, an agent that runs on a **Codex** brain, and a **Cloudflare Worker that deploys** into a working event bus. Today it proves none of them. Private `moda-agents` should prove exactly one further thing — that a given `bobi` version **deploys to Fly** — and should refuse to roll the fleet onto a version it has not proven.

The 10x version is cheap here and worth naming: most of this already exists as written-but-disabled tests. The expensive-looking half (real brains, end-to-end through a real Worker) is a `-m` marker change plus two secrets, not a new harness.

## Problem

**Every claim below was verified against the working tree on 2026-08-01.**

### bobi-agent runs no agent, on any brain

Four independent exclusions stack up so that no real brain executes in CI:

- `.github/workflows/ci.yml:322` — `integration-fast` runs `pytest tests/integration/ -m "not claude and not docker"`, deselecting every `requires_claude` test.
- `.github/workflows/container.yml:130` — the only pytest invocation is `-m "docker and not live"`, excluding the live round-trips.
- `.github/workflows/release.yml:93` — the subscription-login smoke sets `ANTHROPIC_API_KEY: ""` and fakes `claude auth login`. Correct for what it tests (the bootstrap path), but it is not a brain proof.
- `.github/workflows/ci.yml:424-429` states it outright: *"the real-Claude integration suite … ran on a self-hosted EC2 runner. That box was retired when eng-team moved to Fly (C24, #368) … a GitHub-hosted replacement … is a follow-up if we want them gating CI again."* The follow-up never happened.

Codex is worse: the token `codex` appears in bobi-agent CI only in `.github/workflows/team-packages.yml` (lines 5, 23, 57), and only to **build a fixture tarball**. Nothing executes a Codex brain.

**The tests already exist and are simply switched off.** `tests/integration/test_container_image.py` defines `test_image_ask_roundtrip` (line ~437, Claude) and `test_image_codex_ask_roundtrip` (line ~544, Codex). Both run the real image against an **ephemeral event server started from the real Worker sources via `wrangler dev`** (`_start_wrangler_server`, imported from `tests/integration/test_event_server.py`), install the dependency-free `claude-smoke` / `codex-smoke` fixture teams, and complete one named ask round-trip against the real API. Both are `@pytest.mark.live` and `skipif` on a missing key.

The scaffolding to run them is also already present but unfinished: `container.yml:43-45` sets `types: [opened, synchronize, reopened, labeled, unlabeled]` with the comment *"adding the `ci:live` label triggers a run that includes the live-gated steps"* — **the trigger exists; the step it exists for was never added.** `container.yml` also already has a nightly cron (line 48) and `workflow_dispatch` (line 49).

### The Worker is protocol-proven but deploy-unproven

`ci.yml`'s `wrangler` job (line 333) runs `pytest tests/integration/test_event_server.py -k wrangler` against `wrangler dev`, and the `event-server` job runs the Worker vitest suite plus two typechecks. That is real coverage of the Worker's *code*. Nothing proves `wrangler deploy` produces a working Worker: the KV binding, the `v1` `new_sqlite_classes` Durable Object migration, and the account-side provisioning are exercised nowhere. `event-server/worker/wrangler.jsonc` ships `"id": "REPLACE_WITH_YOUR_KV_NAMESPACE_ID"` deliberately, so a deploy without id materialization silently auto-provisions a new namespace.

The only real `wrangler deploy` anywhere is `moda-agents/.github/workflows/release-fleet.yml`'s `deploy-event-server` job — which deploys **straight to production** `bobi-events` before anything validates it.

### moda-agents' canary is broken, unproven, and no longer representative

`release-fleet.yml`'s `build-canary` implements a good idea — both brains as hard gates at parity (#428), `ci-canary` (Claude) and `ci-codex-smoke` (Codex), each ending in a functional `scripts/canary-smoke.sh` ask that must answer `CANARY-OK`. Four problems:

1. **It cannot run.** The job sets `defaults.run.working-directory: bobi-deploy` and then calls `fly deploy "$PWD" --dockerfile "$PWD/Dockerfile"`. Its own comment says *"Dockerfile, deployments/ and scripts/ come from THIS repo"* — but Lane 2 of the repo reorg moved the Dockerfile to public `bobi-agent`. `moda-agents/bobi-deploy/Dockerfile` **does not exist**. The build context is wrong too: the public `Dockerfile` does `COPY docker/docker-entrypoint.sh` and `COPY docker/healthcheck.sh` (lines 277-278), while `bobi-deploy/docker/` contains only `team.Dockerfile` and `webapp.Dockerfile`.
2. **Nothing caught that**, because `release.yml`, `release-fleet.yml` and `deploy-webapp.yml` have **0 runs each** since the reorg relocated them (`gh run list --workflow=…`).
3. **It no longer matches how the fleet builds.** Post-D7 the fleet builds `FROM ghcr.io/moda-labs/bobi:<version>` plus a one-layer team overlay (`bobi-deploy/docker/team.Dockerfile`). The canary builds the *whole* image from the wheel with `--build-arg BOBI_BUILD=wheel`. Before D7 those were the same path; now they diverge, so the canary no longer proves what the fleet does.
4. **Nothing gates a fleet roll.** `deploy-agent-teams.yml` fires on a `deploy-*` tag or manual dispatch and has no dependency on the canary. Bumping `BOBI_VERSION` (line 79) and rolling is unguarded — the 2026-08-01 roll put all five production teams on a version whose first fleet validation *was* that roll.

### A blind spot neither canary covers

Neither `bobi-deploy/deployments/canary.yaml` nor `codex-smoke.yaml` sets `auth:`, so both default to `api_key` (`deploy-agent-teams.yml`: `auth="${auth:-api_key}"`). **Four of five fleet teams run `auth: subscription`.** No canary exercises the auth mode the fleet actually uses — which is the mode that produced the 5-day baohua credential wedge.

## Solution

Split the proof by what each repo owns.

**bobi-agent owns brain and Worker correctness.** Turn on the two live round-trips that already exist, driven by the **real container image** rather than a raw runner: the image is the artifact consumers execute, `container.yml` already builds it, and one step covers entrypoint, auth-file materialization, first-boot team install from an empty volume, the durable volume, supervisor and PID-1 *in addition to* the brain. A raw `ubuntu-latest` runner would prove a `pip install` path no deployed instance uses. Add a separate lane that does a real `wrangler deploy` to a **dedicated `ci-smoke` Worker with its own KV namespace and Durable Object** — never moda-agents' production environment, because sharing prod's KV would let smoke traffic write live event state.

**moda-agents owns the Fly deployment path, and only that.** Once bobi-agent proves both brains against the real image, the Fly canary's remaining job is brain-agnostic: `ci-canary` and `ci-codex-smoke` are byte-identical configs apart from `brain: codex`, so the second buys a duplicate deploy and costs an always-on Fly app, a secret, and the `bootstrap`-policy branch. Keep one canary, repair its wiring, build it **the way the fleet builds** (published base + overlay), and make it gate a `BOBI_VERSION` bump.

**Alternatives considered.** *Raw ubuntu runner for the brain lane* — rejected: tests a path nothing deploys, and would need a new harness while the image path needs a marker change. *Ephemeral per-run Worker created and deleted each time* — see Q2. *Keeping `ci-codex-smoke`* — rejected: the delta it covers moves into bobi-agent, where it runs on every nightly instead of only at release. *Self-hosted runner (restore the retired EC2 box)* — rejected: reintroduces the fragile infrastructure whose retirement caused this gap.

## Relevant files

### Existing (verified 2026-08-01)

**bobi-agent**
- `.github/workflows/container.yml` — builds `bobi:ci`, runs contract tests at line 130 with `-m "docker and not live"`; already carries the `ci:live` label trigger (43-45), nightly cron (48) and dispatch (49). The live step attaches here.
- `tests/integration/test_container_image.py` — holds `test_image_ask_roundtrip` and `test_image_codex_ask_roundtrip`, both `@pytest.mark.live`, both Linux-only (`--network host`).
- `tests/integration/test_event_server.py` — `_start_wrangler_server` / `_has_wrangler`, the ephemeral-Worker harness the live tests import.
- `tests/fixtures/claude-smoke/`, `tests/fixtures/codex-smoke/` — dependency-free fixture teams; already published as tarballs by `team-packages.yml`.
- `event-server/worker/wrangler.jsonc` — `name: bobi-events`, KV `EVENTS` with a deliberate placeholder id, DO `DEPLOYMENT_SESSION`, migration tag `v1` (`new_sqlite_classes`).
- `.github/workflows/ci.yml` — `wrangler` job (333) and `event-server` job (157); the `changes` gate pattern and the matrix-gate idiom to imitate.
- `pyproject.toml:109-114` — marker definitions (`claude`, `docker`, `live`, `local_only`).

**moda-agents**
- `.github/workflows/release-fleet.yml` — `deploy-event-server` (68) deploys prod; `build-canary` (234) holds the broken `--dockerfile "$PWD/Dockerfile"` and the two-canary loop.
- `.github/workflows/deploy-agent-teams.yml` — `BOBI_VERSION` pin at line 79; the roll that currently has no gate.
- `bobi-deploy/deployments/canary.yaml`, `codex-smoke.yaml`, `defaults.yaml` (`fleet: ci` → apps `ci-canary`, `ci-codex-smoke`).
- `bobi-deploy/scripts/canary-smoke.sh` — the functional ask that asserts `CANARY-OK`.
- `bobi-deploy/docker/team.Dockerfile` — the D7 overlay the fleet actually builds.

### New

**bobi-agent**
- `.github/workflows/worker-deploy-smoke.yml` — the real-deploy lane against the `ci-smoke` Worker. Separate from `ci.yml` because it needs Cloudflare credentials and must never run on fork PRs; keeping it in its own file makes that boundary reviewable rather than buried in a job `if:`.
- `event-server/worker/wrangler.ci.jsonc` (or a materialization step) — the CI Worker's own name/KV/DO. See Q2.
- `tests/test_ci_live_wiring.py` — a guard that the live lane cannot be silently disabled (see Proof of work).

**moda-agents**
- `.github/workflows/version-gate.yml` — canary a candidate `BOBI_VERSION` and, only on green, allow the pin bump. See Q4.

## Questionables

- **Q1:** What should the canary build — the wheel, or the published base image plus the team overlay? Options: (a) wheel-built pre-publish, preserving today's canary-gates-GHCR ordering but testing a path the fleet no longer uses; (b) base+overlay post-publish, representative of the fleet but unable to gate the GHCR push that produces its own input; (c) both, split by repo — bobi-agent proves wheel→image (it already builds `bobi:ci` from the wheel and runs contract + live tests on it), moda-agents' canary proves published-base+overlay→Fly. Recommendation: **(c)**, because the two gates answer different questions and the split falls out of the repo boundary already established by the reorg. It also means moda-agents' canary runs *after* a release publishes, which is exactly where a fleet-roll gate belongs.
  **Decision (2026-08-01, Zach):** (c) both, split by repo. bobi-agent owns wheel→image; moda-agents owns published-base+overlay→Fly.

- **Q2:** Should the CI Worker be persistent or created-and-deleted per run? Options: (a) a persistent `ci-smoke` Worker with its own KV namespace and DO, redeployed (overwritten) each run; (b) a uniquely-named Worker per run, deleted at the end. Recommendation: **(a)** — Workers deploys are idempotent and cheap, a stable name makes failures debuggable after the fact, and (b) multiplies KV namespaces and leaves orphans whenever a run is cancelled. The isolation requirement is satisfied by separate bindings, not by ephemerality. Either way the deploy must materialize the KV id explicitly: omitting it auto-provisions a fresh empty namespace, which is the documented trap that would cut a fleet over to an empty bus.
  **Decision (2026-08-01, Zach):** (a) a persistent dedicated `ci-smoke` Worker — stated directly as the requirement: *"we will need to make a ci-smoke worker, it should not use the environment that moda-agents uses for prod."*

- **Q3:** Do we accept that no canary covers `auth: subscription`, the mode four of five fleet teams run? Options: (a) accept and document it as a known blind spot; (b) add a subscription-auth canary. Recommendation: **(a)** for this initiative — subscription auth on a fresh volume triggers device-login, which blocks on a human pasting a code, so it is not automatable without new machinery. Worth its own issue rather than silently widening this plan.
  **Decision (2026-08-01, Zach):** (a) accept and file an issue. Documented as a known blind spot; NOT silently omitted.

- **Q4:** Where does the fleet-roll version gate live? Options: (a) a job inside `deploy-agent-teams.yml` that canaries before deploying; (b) a separate `version-gate.yml` that canaries a candidate version and only then bumps the `BOBI_VERSION` pin, so every roll is already on a proven version. Recommendation: **(b)** — it separates "prove this version" from "roll the fleet", avoids re-canarying on every unrelated team-config roll, and puts the gate where the version actually changes (a PR editing `deploy-agent-teams.yml:79`).
  **Decision (2026-08-01, Zach):** (b) a separate `version-gate.yml`.

- **Q5:** How are the new secrets scoped in a **public** repo? bobi-agent today holds one secret (`HOMEBREW_TAP_TOKEN`) and two environments (`pypi`, `release`); this adds `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`. Options: (a) repo secrets, relying on GitHub never passing secrets to fork-PR workflows; (b) a dedicated `ci-live` environment with required reviewers, so even a same-repo PR needs an approval click to spend credentials. Recommendation: **(b)** for the Cloudflare pair (a deploy credential is the higher-blast-radius one) and **(a)** for the two model keys, gated on the `ci:live` label so cost stays deliberate. Non-negotiable either way: `pull_request_target` is never used, and the live lane must be provably inert on fork PRs.
  **Decision (2026-08-01, Zach):** (a) all four as repo secrets — keeping the nightly lanes fully unattended (a gated environment would stall a cron run waiting for a reviewer, losing the rot-detection the nightly exists for). The compensating controls therefore carry more weight and are REQUIRED, not optional: no `pull_request_target` anywhere, a proven-inert-on-fork-PRs check in Phase 1's gate, the `ci:live` label for cost, and a Cloudflare token scoped to the `ci-smoke` Worker alone so it cannot touch production.

- **Q5 follow-on (raised during Lane A's build, 2026-08-01):** the decision's
  fourth compensating control — *"a Cloudflare token scoped to the `ci-smoke`
  Worker alone so it cannot touch production"* — **is not implementable as
  written.** Cloudflare's `Workers Scripts` and `Workers KV Storage`
  permissions are granted at ACCOUNT scope only; there is no per-script or
  per-namespace resource restriction (verified against Cloudflare's permissions
  reference, and empirically: the existing token lists and can edit production
  `bobi-events` and its `EVENTS` namespace). Isolation therefore has to move up
  a level. Options: (a) a separate Cloudflare account holding only the
  `ci-smoke` Worker and its KV, with an account-scoped token that consequently
  reaches nothing production; (b) one account, accepting that a public repo
  holds a credential able to overwrite the production event bus.
  **Decision (2026-08-01, Zach):** (a) — a separate Cloudflare account. The
  control survives in substance; only its mechanism changed.

## Phases

### Phase 1 — bobi-agent: both brains run, against the real image `[x]`

- [x] Add `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` as repo secrets (Q5).
- [x] Add an `npm ci` step in `event-server/` to `container.yml`'s `container-image` job so `_start_wrangler_server` is available. **Needed a Node 22 swap too** — the job pins 20 because `hatch_build.py` requires exactly 20 for a non-editable wheel, and wrangler refuses anything below 22. The swap goes after the wheel build; nothing after it builds a wheel.
- [x] Add the live step to `container-image`, gated `schedule || workflow_dispatch || contains(labels, 'ci:live')`, running `pytest tests/integration/test_container_image.py -m "docker and live"` with `BOBI_TEST_IMAGE: bobi:ci`.
- [x] Make the step **fail if the round-trips did not run**. Both tests `skipif` on a missing key, so a misnamed secret skips both and exits 0. Two layers, because one is not enough: (i) fail the step up front when either key is empty — cheap, and catches the common case loudly; (ii) run with `--junitxml` and assert exactly 2 passed / 0 skipped, which also catches a skip from the Linux/`--network host` guard or an unavailable wrangler harness. Do not parse `-rA` prose. Layer (ii) is `scripts/assert_junit_ran.py`, whose every rejection path is unit-tested in `tests/test_ci_guard_scripts.py`.
- [x] Confirm the lane is inert on fork PRs and never uses `pull_request_target`. Inertness is by IDENTITY (`head.repo.full_name != github.repository`), not by relying on GitHub withholding secrets — failing a contributor's PR for a credential they cannot have is its own bug. `test_no_workflow_uses_pull_request_target` enforces the absence across every workflow.

**Validation gate**

- [x] `gh workflow run container.yml` → the live step runs and **both** `test_image_ask_roundtrip` and `test_image_codex_ask_roundtrip` PASS. Run [30718916924](https://github.com/moda-labs/bobi-agent/actions/runs/30718916924), 2026-08-01: `2 passed, 19 deselected in 40.50s`, and the ran-assertion printed `live lane ran: 2 passed, 0 skipped`. **First time a real brain of either kind has executed in this repo's CI.**
- [x] Non-vacuity: a run with `ANTHROPIC_API_KEY` deliberately unset **fails** rather than reporting green. Run [30719047118](https://github.com/moda-labs/bobi-agent/actions/runs/30719047118) with the secret deleted: RED at the fail-fast step, round-trips skipped, never reported green. Secret restored immediately after. Layer (ii)'s own rejection paths (skip / empty selection / renamed test / missing report) are proven in `tests/test_ci_guard_scripts.py` rather than by deliberately breaking CI a second time.
- [x] A PR without the `ci:live` label does not run the step, and its check set is unchanged — the `container.yml` run on this lane's own PR shows steps 9-14 skipped with the job name unchanged.

### Phase 2 — bobi-agent: a real Worker deploy, on its own infrastructure `[x]`

- [x] Provision the dedicated `ci-smoke` Worker (Q2) — `bobi-events-ci-smoke` in a SEPARATE Cloudflare account (`71403a72…`, not production's `6db47dd2…`), with its own `EVENTS-ci-smoke` KV namespace and its own DO: its own name, its own KV namespace, its own Durable Object. **It must not reference moda-agents' production `bobi-events` Worker or its `EVENTS` namespace.**
- [x] Add `.github/workflows/worker-deploy-smoke.yml`: materialize the CI KV id into the Worker config (fail loudly if absent — never let wrangler auto-provision), `wrangler deploy`, then assert `/health` and one publish→subscribe round-trip through the deployed Worker.
- [x] Add `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` / `CI_SMOKE_KV_NAMESPACE_ID` as repo secrets (Q5). **The stated scoping is not implementable** — see the 2026-08-01 Q5 amendment; isolation moves to the account level.
- [x] Add an isolation guard — `scripts/render_worker_ci_config.py`, which derives the CI config from the shipped `wrangler.jsonc` (so the migration, DO binding and compatibility date cannot drift from what production deploys) and refuses to render an unsafe one. **It cannot compare against the production KV id** — that id lives in private `moda-agents` and public `bobi-agent` must never learn it. Assert instead on properties bobi-agent owns: the CI Worker's `name` is not `bobi-events`, its KV id comes from CI configuration rather than a hardcoded literal, and the placeholder `REPLACE_WITH_YOUR_KV_NAMESPACE_ID` never reaches a deploy.

**Validation gate**

- [x] The workflow deploys and `/health` returns 200 with the expected version metadata. Run [30721331250](https://github.com/moda-labs/bobi-agent/actions/runs/30721331250), 2026-08-01: `deployment is serving 571d0a65…`, both tests PASS, ran-assertion `2 passed, 0 skipped`.
- [x] A publish→subscribe round-trip succeeds against the **deployed** Worker (not `wrangler dev`) — same run.
- [x] The `v1` `new_sqlite_classes` migration applies on a first deploy to a clean Worker. The account and the Worker did not exist before this lane; the first-ever deploy created both, and the DO-backed WebSocket round-trip proves the migration took effect.

**Isolation verified, not assumed** (2026-08-01): the CI token is rejected with `Authentication error` on production's account for BOTH `workers/scripts` and `storage/kv/namespaces`, while succeeding on the CI account — which held zero Workers and zero KV namespaces before this lane created them.
- [x] Guard proves non-vacuous: a config naming `bobi-events`, or carrying the placeholder KV id, fails the run — every rejection path exercised in `tests/test_ci_guard_scripts.py`, including the CLI writing no file at all when the config is unsafe.

### Phase 3 — bobi-agent: make the coverage un-disableable and documented `[x]`

- [x] `tests/test_ci_live_wiring.py`: assert `container.yml` still carries the `ci:live` trigger types, the live step, and the ran-assertion; assert `pyproject.toml` still defines the `live` marker. This is the same class of protection as `release-fleet.yml`'s `smoked` output — a lane that silently stops running is worse than one that never existed.
- [x] Document both lanes in `AGENTS.md` (how to trigger, what they cost, what they prove) — new "Live CI Lanes" section. `CLAUDE.md` is a symlink to it, so one edit covers both.

**Validation gate**

- [x] `pytest tests/test_ci_live_wiring.py` passes, and fails when the live step is deleted from `container.yml` — verified 2026-08-01 by deleting the step: `test_live_brain_roundtrips_still_run_on_both_brains` FAILED, 14 passed.
- [x] Full unit suite green: 3799 passed, 1 skipped (2026-08-01).

### Phase 4 — moda-agents: repair the canary so it can run at all `[x]`

- [x] Re-aim `build-canary` onto the fleet's real build path per **Q1(c)**: published base + `bobi-deploy/docker/team.Dockerfile` overlay, `BASE_IMAGE_REPO`/`BOBI_VERSION` build-args, `TEAM_DEPS` pointed at the rendered hook.
  **DONE differently, and better:** rather than re-spelling the build-args in YAML, `build-canary` now runs `bobi deploy canary --rebuild` — the same primitive `deploy-agent-teams.yml` uses. The engine picks `team.Dockerfile` and passes `BOBI_VERSION`/`TEAM_DEPS` itself, so the workflow holds no second copy of the build recipe to drift. Proven in the run: `--dockerfile /tmp/.../team.Dockerfile --build-arg BOBI_VERSION=0.51.1`.
  **NOTE — this supersedes the obvious repair.** The naive fix is to re-point `--dockerfile` at the checked-out public repo (`$PWD/bobi-agent/Dockerfile`) so `COPY docker/docker-entrypoint.sh` resolves. Do NOT do that: Q1(c) moves wheel→image proof to bobi-agent, so the canary must stop building the full recipe entirely. Both the `--dockerfile` path AND `--build-arg BOBI_BUILD=wheel` go away with it.
- [x] Re-evaluate whether `Checkout public bobi-agent (sibling layout)` and `Stage the public pyproject.toml (image deps-layer key)` are still needed. Under base+overlay the canary consumes a published image, so it may need neither — delete what is genuinely dead rather than leaving inert steps.
  Both deleted, along with the wheel staging/rebuild steps and the `claude-version` + `use-dist-artifact` inputs they served. `release.yml` updated to match, and a test asserts the canary checks out no other repo.
- [x] Verify `scripts/canary-smoke.sh` and `bobi-deploy/deployments/*.yaml` still resolve under `working-directory: bobi-deploy` (they do today: `fleet: ci` → `ci-canary`).
  Confirmed by the live run. Its message was also corrected — it claimed to have smoked "the wheel".
- [x] Record the release-ordering consequence of Q1(c) in `docs/RELEASE_RUNBOOK.md` in the same PR: the canary now consumes a PUBLISHED base, so it can no longer gate the GHCR publish.
  `docs/RELEASE_RUNBOOK.md` lives in THIS repo, so "the same PR" was cross-repo-impossible; it is recorded here instead, in the PR that flips these markers. The moda-agents side (`release.yml`'s and `CONTAINERIZED_DEPLOYMENT.md`'s statements of the old ordering) was corrected in that PR.

**Validation gate**

- [x] `gh workflow run release-fleet.yml` (dispatch) completes and `ci-canary` answers `CANARY-OK`.
  Run [30723965286](https://github.com/moda-labs/moda-agents/actions/runs/30723965286), the FIRST ever run of this workflow in its new home, green on the first attempt: `Canary builds FROM ghcr.io/moda-labs/bobi:0.51.1` → overlay build → `ci-canary answered CANARY-OK (attempt 1)`.
- [x] `smoked=true` is emitted — the gate genuinely ran, not merely succeeded.
  `Set output 'smoked'` in the run log. The same run also proved the skipped-`deploy-event-server` path does not skip the gate (`deploy-worker: false`).

### Phase 5 — moda-agents: one canary, and a version gate in front of the fleet `[x]`

Landed as moda-agents PR #80 (`cada0a49`); both merge-blocked gates discharged 2026-08-02.

- [x] Collapse the `canaries=(…)` loop to the single brain-agnostic `ci-canary`; delete `bobi-deploy/deployments/codex-smoke.yaml`, destroy the `ci-codex-smoke` Fly app, and remove `CODEX_SMOKE__OPENAI_API_KEY`.
  App destroyed 2026-08-01 (Zach's call to do it immediately rather than after merge). `CODEX_SMOKE__OPENAI_API_KEY` needed no removal from GitHub — it never existed there; the key lived only as a Fly secret on the app and went with it.
- [x] Remove the now-dead `bootstrap` policy branch and its comment.
- [x] Add `version-gate.yml` per Q4: canary a candidate `BOBI_VERSION`, and only on green allow/open the pin bump.
  It also moves every pin at once. The version is pinned in FOUR places (`deploy-agent-teams.yml`, `team-images.yml`, and twice in `lint.yml`), previously kept in sync by a comment; a test now discovers the pin set rather than hard-coding it, so a fifth pin fails CI instead of going stale.
- [x] Record the Q3 subscription-auth blind spot as an issue and reference it in `deployments/README.md`.
  moda-agents#81, referenced from `deployments/README.md`.

**Validation gate**

- [x] `version-gate.yml` dispatched against the current pin (0.51.1) goes green.
  Run [30756274258](https://github.com/moda-labs/moda-agents/actions/runs/30756274258): canary deployed FROM `ghcr.io/moda-labs/bobi:0.51.1`, answered `CANARY-OK` on attempt 1, verdict `0.51.1 is proven`. It also proved the SELF-HEALING credential path end-to-end — with `CANARY__ANTHROPIC_API_KEY` now set, the fallback warning is gone — and the bump correctly no-opped (`already pinned to 0.51.1`) without opening a PR.
- [x] Dispatched against a deliberately bad version, it **fails** and does not bump the pin.
  Run [30756439165](https://github.com/moda-labs/moda-agents/actions/runs/30756439165) against `0.99.99`: `prove` FAILED at the pip pin (`No matching distribution`), the verdict job reported `the fleet must not roll onto it`, and `bump` was **skipped**. All four pins still read `0.51.1` on `main` and no `version-gate/*` branch was created. The verdict job's `always()` is what makes a red gate report rather than silently skip.
- [x] `ci-codex-smoke` no longer exists in `fly apps list`, and `release-fleet.yml` contains no reference to it.
  Both confirmed: `fly apps list` shows `ci-canary` alone, and `test_release_smokes_exactly_one_brain_agnostic_canary` fails if any codex reference returns.

## Proof of work

- **Bugs get a failing test first.** Phase 4 repairs a break that has never executed; the proof is a dispatched run that reaches `CANARY-OK`, and the run recorded on the PR — not a reading of the YAML.
- **Every gate must prove it RAN, not merely that it passed.** This initiative exists because four separate lanes were green while proving nothing (`-m "not live"`, a retired runner, 0-run workflows, a `skipif` on an absent key). Each new lane therefore carries an explicit ran-assertion, and each non-vacuity gate above deliberately breaks the lane to confirm it goes red. `release-fleet.yml`'s existing `smoked` output is the in-repo precedent.
- **Real-brain e2e (bobi's judgement call, per `AGENTS.md`).** This initiative *is* the real-brain leg: correctness here depends entirely on the live brain path, so the stub is not sufficient and the acceptance bar is the live round-trips themselves.
- **Suites that must stay green:** `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/` (bobi-agent unit), `tests/integration -m "not claude and not docker"`, both TS suites and both typechecks, and moda-agents' `deploy-tests` / `package-smoke`.
- **New tests:** `tests/test_ci_live_wiring.py` (Phase 3) and the production-KV guard (Phase 2).

## Lane map

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| A | #909 | 1-3 | bobi-agent: both brains against the real image, real Worker deploy on dedicated infra, anti-rot guards | concurrent | **landed** (PR #911, 2026-08-01) |
| B | none — built in-session | 4-5 | moda-agents: repair the canary, collapse to one, gate fleet rolls on a proven version | concurrent | **landed** (moda-agents PR #80, 2026-08-02) |

**Lanes:** Two lanes because the work spans two repos, which forces separate PRs — no same-repo parallel cut is made or needed. Topology is **STACKED by landing, parallel by build**: Lane B may be built as soon as Lane A's shape is known, but it **lands after** Lane A, because dropping `ci-codex-smoke` is only safe once Codex coverage genuinely exists in bobi-agent. That is "lands after", not "depends on" — it does not block dispatch. Both lanes are `concurrent` marker mode (cross-repo): flip markers as status-only commits to bobi-agent's `main` referencing the code PR, never inside a feature branch.

**Interface lock to relay (A → B):** the exact published base-image tag shape and what the `team.Dockerfile` overlay may rely on already being present — Lane B's canary rebuild is written against that, and must not assume it.

- [x] Convergence gate — *deferred* (needs the real sequence, not a merged preview): one release cut under the rewritten runbook in which bobi-agent's live lanes ran green, moda-agents' repaired canary gated it, and the fleet rolled onto a version the version-gate had already proven.
  Discharged 2026-08-03 by the **bobi 0.52.0** release. All three conditions met, each independently verified rather than inferred from a green check:
  1. *Live lanes green* — the nightly on `main` ran both of Lane A's lanes unprompted: `container-image` (run 30799606075, both brains against the real image) and `worker-deploy-smoke` (run 30804504803), the latter confirming #921's 5-consecutive-match readiness fix holds against a real Cloudflare rollover.
  2. *The repaired canary gated it* — `release.yml` (moda-agents run 30824522825, its **first-ever** execution) deployed `ci-canary` from the just-published `ghcr.io/moda-labs/bobi:0.52.0` and logged `Canary smoke passed — ci-canary answered CANARY-OK on the deployed image (attempt 1)` before setting `smoked`. Checked against the vacuous-pass trap: `FLY_API_TOKEN` was present, so the `exit 0 without smoking` branch was not taken.
  3. *The fleet rolled onto a version-gate-proven version* — `version-gate.yml` (run 30824960663) re-proved 0.52.0 on the canary, and the `BOBI_VERSION` pin moved 0.51.1 → 0.52.0 (moda-agents #85) only after that. All five teams then rolled and were confirmed running 0.52.0 by direct query, not by CI status.

  **The gate earned its keep.** Sequencing the real thing found three defects that every prior green run had hidden, all in never-executed paths — the shape this plan was written to attack:
  - `deploy-agent-teams.yml`'s moda-skills `git ls-remote` had **never once succeeded in CI**: `actions/checkout` persists `http.extraheader`, which overrides URL-embedded credentials, so it authenticated as `GITHUB_TOKEN` and 404'd on the private repo. It read as an expired credential and cost a needless token rotation before the real cause was found. Fixed in moda-agents #86, with a mutation-proven guard.
  - `version-gate.yml` **cannot open its own pin-bump PR**: `GITHUB_TOKEN` has no `workflows` scope, and three of its four pins live under `.github/workflows/`. Invisible until now because the only green proof ran against the already-pinned version, where the rewrite short-circuits with `changed=false` and never reaches the push. Opened by hand this release; still open as a follow-up.
  - `HOMEBREW_TAP_TOKEN` had expired, so the tap dispatch failed with `Bad credentials`.
- [x] Convergence gate — *fuse-runnable*: bobi-agent full unit suite + both TS suites + both typechecks + `tests/integration -m "not claude and not docker"` green on a locally merged preview of both lanes.
  Run 2026-08-02 against `main` at `55402c4` — with both lanes landed, main IS the merged preview. Unit **3801 passed, 1 skipped**; integration (not claude, not docker) **305 passed, 9 skipped**; events-core **402 passed**; Worker **104 passed**; both typechecks clean. One caveat worth stating rather than burying: `test_packaged_event_server.py` errored 6 times on the first pass purely because the local shell had Node v24 — `hatch_build` requires exactly Node 20 to build the embedded event server. Re-run under Node 20: **9 passed**. Not a regression; CI pins 20 for this.

## Amendments

- **2026-08-01** (Lane B build): **Lane B was built without a dispatch issue**,
  by Zach's decision — filing one in `moda-agents` routes an event to the
  eng-team bot, and the work involved a never-before-run release workflow plus
  destroying a Fly app, which is not where a PR-latency debug loop belongs. The
  Lane map row records "none — built in-session" rather than carrying a stale
  `#TBD`.

- **2026-08-01** (Lane B build): three things the plan did not anticipate, all
  found by executing rather than reading.
  1. **Phase 4's prescribed mechanism was superseded by a simpler one.** The plan
     said to re-spell `BASE_IMAGE_REPO`/`BOBI_VERSION`/`TEAM_DEPS` as build-args
     in the workflow. But "the fleet's real build path" IS `bobi deploy` — the
     engine already selects `team.Dockerfile` and passes those args. Hand-copying
     them into YAML would have recreated the drift seam the reorg removed. The
     canary now runs the same primitive the fleet does.
  2. **A second, independent piece of reorg breakage.** The `canary` GitHub
     Environment (`CANARY__*`) never survived the move out of the archived
     bobi-deploy repo. `deployments/canary.yaml` documented an Environment that
     did not exist; the canary kept working only because a `team-url` deployment
     prunes nothing, so the live Fly secret survived every image swap. Recreated
     and wired, so the gate can now re-credential a recreated app.
  3. **`smoked` gates the ROLL, not the publish — and the old note was
     impossible.** `release.yml` carried a Lane 3 instruction to "run this fleet
     gate FIRST, publish the image only once `smoked` is true". Under Q1(c) the
     canary consumes the published base, so the image must exist before the gate
     can run at all. Deleted rather than left as an instruction nobody can follow.


- **2026-08-01** (Lane A landing): the Lane map's `concurrent` marker mode
  assumes status-only commits straight to `main`. That is not possible in this
  repo — `main` carries a `pull_request` rule, so every marker flip needs a PR.
  Lane A's phase markers rode PR #911; this row flip is a separate one. Lane B
  should fold its own status flip into a single PR at the end rather than
  opening one per marker.

- **2026-08-01** (session "repo-reorg"): plan created from the CI review in that session.
- **2026-08-01** (session "repo-reorg"): Q1-Q5 resolved with Zach and written back; no questionables remain open.
- **2026-08-01** (Lane A build, #909): three plan claims did not survive
  implementation, all recorded above rather than silently worked around.
  (1) Q5's per-Worker token scoping is impossible on Cloudflare — isolation
  moved to the account level. (2) Phase 2's health check was to lean on
  `CF_VERSION_METADATA` as the "really deployed" signal; `wrangler dev`
  populates `version_id` and `version_timestamp` too, so the check now
  discriminates on the release sha, which only the rendered CI config carries.
  (3) Phase 1's `npm ci` item needed a Node 22 swap alongside it — the job pins
  20 for the wheel build and wrangler refuses anything below 22.
- **2026-08-01** (Lane A build, #909): the first real deploy immediately earned
  its keep by exposing two failures invisible to `wrangler dev`, both now fixed
  and guarded. (1) Cloudflare's edge answers **403 to the default
  `Python-urllib` User-Agent**, so every smoke request failed against the
  deployed Worker while the same code passed against dev; the smoke now sends
  an explicit UA. Scope is the smoke module only — the shipped client posts via
  httpx, which is not blocked. (2) `wrangler secret put` publishes another
  Worker version, and requests during that rollover **500**; the lane now gates
  on the deployment serving the commit's own release sha before smoking, which
  is a readiness gate rather than a retry-until-green (a stale or broken deploy
  never becomes ready).
- **2026-08-01** (Lane A build, #909): Lane A's marker flips ride its own PR
  rather than status-only commits to `main`, a deliberate deviation from the
  `concurrent` marker mode in the Lane map. Lane A is the only lane touching
  this repo, so there is no concurrent writer to collide with, and the
  falsified claims above are things a reviewer needs to see in the same diff
  that acts on them.
- **2026-08-01** (session "repo-reorg"): self-review (red-team / staff-engineer / implementer lenses) folded back three findings — the prod-KV guard was unimplementable across the public/private boundary and is re-specified against properties bobi-agent owns; the ran-assertion got a concrete two-layer mechanism; and Phase 4's dockerfile repair was superseded by Q1(c), which retires the full-recipe build in moda-agents entirely.

## Notes

- **Prior art / origin.** This plan comes out of the repo-reorg session (`plans/2026-07-29-repo-reorg.md`), whose Convergence gate remains open. The reorg's deferred "public brain-smoke Worker" item — parked because *"sharing prod's KV lets smoke traffic write live event state"* — is Phase 2 here, and Zach's constraint on 2026-08-01 restated it: the CI Worker must not use the environment moda-agents uses for prod.
- **Structural point worth keeping.** Until Lane A lands, the public repo's brain coverage is entirely delegated to a private repo's release gate. Anyone forking `bobi-agent` gets CI that never runs an agent. Phase 1 is what makes the public repo's CI mean something standalone.
- **Cost.** The live lanes spend real model credits and a Cloudflare deploy per run, which is why they are nightly + label-gated rather than per-PR. If nightly proves too noisy or too expensive, reduce cadence before reducing coverage.
- **Deferred (do not silently absorb):** a subscription-auth canary (Q3); restoring the canary-gates-GHCR-publish invariant that `release.yml` lost in the split, which currently needs a cross-repo `repository_dispatch`; and a CI lane for the DIY `pip install bobi` path, which the image lane deliberately does not cover.
