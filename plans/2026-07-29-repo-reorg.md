# Repo reorg: publish the product surface, consolidate the ops surface

> **Status:** Draft
> **Tracking issue:** none by decision (2026-07-29) · **Created:** 2026-07-29 · **Last amended:** —
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

One reorganization, three movements, ending with two repos instead of three:

1. **Publish the product surface.** The Cloudflare event-server Worker, the admin sidecar, and the container image recipe move from private `bobi-deploy` into public `bobi-agent`, reaching consumers through the PyPI wheel and a reference image instead of a private repo grant. The admin protocol becomes a documented, versioned contract — that, not the code, is what external consumers bind to.
2. **Retire the repo-split bridges.** The npm transport for `@moda-labs/bobi-events-core`, the `worker-integration.yml` cross-repo CI, and the exact-pin lockstep guards exist only to carry code across the private/public boundary. With the Worker home, they dissolve.
3. **Consolidate the ops surface.** What remains of `bobi-deploy` — the Fly deploy engine, the hosted console, the fleet workflows, the ci canaries — has exactly one consumer: moda's own fleet, whose teams and deployment configs live in `moda-agents`, which today carries a pinned checkout of the engine. Merge the remainder into `moda-agents` and archive `bobi-deploy`.

End state:

| Repo | Holds |
|---|---|
| `bobi-agent` (public) | framework wheel (now incl. `bobi/supervisor`), all four event-server variants (local, Slack socket, Discord gateway, **Worker**), the **reference image** (Dockerfile + `docker/` + GHCR release pipeline), admin protocol spec, deploy runbooks |
| `moda-agents` (private) | agent teams + deployment configs + the deploy engine (`bobi_deploy`) + hosted console + fleet/release workflows + ci canaries |
| `bobi-deploy` | archived, read-only |

The public/private boundary itself is unchanged and stays enforced (`tests/test_import_boundaries.py`): public product versus moda operational IP. This plan moves things that sat on the wrong side of that line, and removes the second private repo that no longer earns its keep once they're gone.

Nothing is redesigned in flight. Components move as they are; the only content edits are the ones publication forces.

## Problem

**The access problem.** A design partner stands up their own bobi deployment on Kubernetes and needs the Worker and the sidecar. Everything else they need is already public — they build their own image from `pip install bobi==<version>`. Under the current layout the only delivery options are a `bobi-deploy` read grant (GitHub has no path-level scoping, so that hands over the Fly engine and fleet config too) or a maintained copy (permanent drift; the supervisor is actively developed — bobi-deploy#45 is open). The de facto status quo is worst of all: a committed doc in the partner's repo instructs `git clone git@github.com:moda-labs/bobi-deploy.git`.

**The lockstep problem.** The split forces bridge machinery whose only job is keeping two repos in sync:

- The Worker consumes `@moda-labs/bobi-events-core` as an exact npm registry pin; `ci.yml` runs a publish-smoke step and `worker-integration.yml` runs a pin-match check purely to police that seam.
- `moda-agents/deploy-agent-teams.yml` checks out `bobi-deploy` at a pinned commit into `.bobi-deploy`; the pin comment is a running changelog of nine issue references, each requiring a coordinated bump.
- Releases touch three repos in sequence, every time (`docs/RELEASE_RUNBOOK.md`).

**No component left behind has a reason to be where it is.** Verified 2026-07-29/30:

- The supervisor imports **stdlib + public `bobi` only**; its `__init__` forbids importing its parent package; it already has a `platform == "k8s"` branch (downward-API `POD_NAME`/`NODE_NAME`); its only moda-specific string is a comment. The public `supervise` CLI slot is empty — the sidecar replaced it, so this is a restoration.
- The Worker's only non-public dependency is the already-published npm core; its full CI (`wrangler dev`, miniflare, the protocol suite) runs with **no Cloudflare credentials**.
- The base image is already built for outsiders — `release-image.yml`: *"a consumer on their own orchestrator can `docker run` it or `FROM` it without vendoring our Dockerfile"* — and publishes public multi-arch GHCR tags from public inputs (pypi-mode wheel, noop team-deps, no secrets baked). Only the *recipe* is private, for no remaining reason once the supervisor ships in the wheel.
- The deploy engine's only consumer is moda's fleet: the partner explicitly rejected it ("NOT bobi-deploy's Fly engine"), and the `bobi-deploy` PyPI name is a guard stub that errors on install. It is ops tooling, not product — and `moda-agents` (the fleet's teams and configs) already depends on it by pinned checkout.
- The client half of the admin path is already public: `bobi/events/client.py:153` (`EventServerClient`), `bobi/events/server.py:684` (`ensure_bubble`).

## Solution

### Movement 1 — publish the product surface

**Worker → public `event-server/`.** The four TS sources (`index`, `deployment-session`, `fleet`, `internal-auth`), `wrangler.jsonc`, `worker-configuration.d.ts`, and the test tree join the three local variants. Publication edits: parameterize the moda KV namespace id (`e84e897a…`) with a placeholder + `wrangler kv namespace create EVENTS` setup step (`name: bobi-events` becomes a documented default); rename the `x-moda-test-secret` header (`src/index.ts:765`) to a vendor-neutral name. The `DEPLOYMENT_SESSION` DO binding and `v1` `new_sqlite_classes` migration carry over verbatim. Worker build/test config merges into the existing workspace `package.json`/`tsconfig.json`/`vitest.config.mts` without breaking the local variants. A deploy runbook covers: Workers Paid plan (SQLite-backed DOs), KV create, the `wrangler secret put` list, `wrangler deploy`, `/health` verification.

**Sidecar → `bobi/supervisor/`.** The 11 modules move as-is (dropping the now-moot "must not import its parent" note, keeping the discipline); a `bobi supervise` CLI entry point is added (Q1); `pyproject.toml` packaging covers the new package. The private hosted console keeps importing it (private→public, the allowed direction).

**Reference image → public `bobi-agent`.** The Dockerfile, `docker/` (entrypoint, healthcheck, noop-deps), `release-image.yml`, and the `container.yml` CI gate move to the public repo. Definition of the reference image: **everything needed to stand up bobi in a VM — the wheel, kb deps, the pinned brain CLIs (`claude`, `codex`, `aichat`), runtime tools, the sidecar — and nothing else.** Deltas the move forces:

- The supervisor arrives **via the wheel**: delete `ARG BOBI_SUPERVISOR_SRC`, the supervisor `COPY`, and the `bobi-supervisor` PYTHONPATH shim; the entrypoint supervises through the public CLI. This gates the lane on a release carrying the sidecar.
- **PID-1 is a documented flag, not a baked init.** The image stays tini-free (tini-on-Fly is a documented boot-failure trigger, and moda's fleet still consumes this image); the runbook states `docker run --init` for VM/compose and an init/`shareProcessNamespace` for K8 pod specs.
- The **generic container contract docs** (one image/every tenant, identity via volume + env, the `/data` layout) migrate public with the recipe; Fly fleet mechanics stay behind for Movement 3.
- The `TEAM_DEPS` hook stays in the Dockerfile (Q6): default-noop, byte-identical image when unused, renderer (`bobi/build_render.py`) already public — and removing it would force moda to keep a private Dockerfile variant, recreating the fork this plan deletes.
- GHCR publishing continues uninterrupted: the workflow moves with its `IMAGE: ghcr.io/moda-labs/bobi` env unchanged; consumers never see the publishing repo. Prove the new home with a dispatch dry-run publish (the same verification #609 used).

**Admin protocol contract.** Publish a spec covering: admin topic naming and bubble binding (commands are bubble-namespaced and fail-closed — only the target deployment can receive its own); the command/`command_result` schema for all nine commands (`restart`/`stop`/`start` ack immediately as `{"accepted": true}` with the effect surfacing on the heartbeat; `chat` is the only async command); the heartbeat/snapshot schema for dashboard consumers. `snapshot.py` already carries `SUPERVISOR_VERSION`; the spec states its compatibility promise (Q3) rather than inventing a scheme. Security of the write path rests on bubble namespacing, not obscurity — say so explicitly.

**Positioning sweep.** `docs/SELF_HOSTED_EVENT_SERVER.md` currently says durable replay "is the managed deployment tier, not this one" — publishing the Worker makes that false (Q5); rewrite it so the durable Worker is a documented self-host option, add the Worker as the fourth variant wherever the three are enumerated, sweep `README.md` and `docs/EVENT_SERVER.md`.

### Movement 2 — retire the repo-split bridges

**The npm transport.** Inside the public repo the core is consumed through npm workspaces (`"@moda-labs/bobi-events-core": "*"`), never the registry; the published package exists solely for the private Worker (`ci.yml` says so verbatim). Once the Worker rejoins the workspace: repoint its dependency from the `0.1.0` pin to `"*"`; delete `pack:publish` + `core/scripts/pack.mjs`, `smoke` + `core/scripts/smoke.mjs`, the vendored `core/moda-labs-bobi-events-core-0.1.0.tgz`, and the "Events-core publish smoke" CI step; drop the npm leg from the release process. Do **not** unpublish `0.1.0` — it stays on the registry (Q4 covers whether the publish path ever returns).

**`worker-integration.yml`.** Dissolve, don't move. The split scaffolding (sibling checkout, pin check, `BOBI_TEST_ES_DIR` lockstep grep, dual toolchain setup) is meaningless in one repo. Two steps survive: Worker typecheck + miniflare units fold into `ci.yml`'s `event-server` job (which already runs workspace-wide `tsc --noEmit` + `vitest run`); the wrangler-dev protocol suite (`pytest tests/integration/test_event_server.py -k wrangler`, `BOBI_TEST_WRANGLER=1`) becomes a public CI job, replacing the placeholder NOTE at `ci.yml:277`. Cadence is the executor's call — the private nightly existed largely to catch published-core drift, which this movement eliminates.

### Movement 3 — consolidate the ops surface

Merge what remains of `bobi-deploy` into `moda-agents`, preserving history (`git subtree add` — 120 commits, 4.2 MB), then archive `bobi-deploy` read-only (archive, not delete: inbound links from issues, PRs, and past release notes keep resolving).

Contents that migrate: the `bobi_deploy` package (engine: `build.py`, `deploy.py`, `scaffold.py`, `cli.py`; `webapp/` hosted console), `deployments/` (ci-fleet canaries), the fleet/ops workflows (`release.yml`, `release-fleet.yml`, `team-images.yml`, `deploy-webapp.yml`, `deploy-package.yml`, `pypi.yml`), `fly.webapp.toml`, the Fly-specific docs, the engine tests, `scripts/`, and the `pypi-stub` name guard (already published; keep the source with the engine it guards).

Integration edits in `moda-agents`:

- `deploy-agent-teams.yml` drops the `.bobi-deploy` pinned checkout and installs the engine from the repo itself — the nine-issue pin comment block dies. Engine and fleet config now version atomically.
- The README's identity paragraph is rewritten honestly: `agents/` + `deployments/` remain the reference shape for an outside consumer (published wheel, no framework checkout); the repo *also* carries moda's private deploy engine and ops. The dogfooding property lives in the teams' consumption path, which is unchanged.
- Open `bobi-deploy` issues migrate; the eng-team bot's dispatch watch repoints from `bobi-deploy` to `moda-agents`.
- `docs/RELEASE_RUNBOOK.md` is rewritten for the two-repo train: `bobi-agent` releases wheel + image; `moda-agents` bumps pins and rolls the fleet.

### Ordering

Movement 1's Worker/sidecar lanes and Movement 2 land first in `bobi-agent`. A public release (**gate R**) then ships the wheel carrying `bobi/supervisor` and the Worker sources. The reference-image lane needs R (the image installs the sidecar from the released wheel). Movement 3 needs the image lane complete (so the merge carries no Dockerfile) and R (so the fleet pin bump and the engine merge land together). The fleet must roll green from the merged repo **before** `bobi-deploy` is archived.

### Non-goals

- No restructuring of any component's internals; no change to the public/private boundary itself.
- No new public UI — the hosted console stays private; external consumers build their own (MCP client, dashboards).
- The MCP admin server is enabled by this work, not part of it.
- No changes to how teams are authored, packaged, or installed.

## Relevant files

### Existing (verified 2026-07-29/30)

**Public `bobi-agent`:** `event-server/src/{local,slack-socket-local,discord-gateway-local,socket-driver-common}.ts`; `event-server/package.json` (workspace + `"*"` dep), `core/package.json` + `core/scripts/{pack,smoke}.mjs` + vendored tgz (publish path being retired); `tsconfig.json`, `vitest.config.mts` (merge targets); `bobi/cli.py` (empty `supervise` slot); `bobi/events/client.py:153`, `bobi/events/server.py:684` (public admin-path primitives); `bobi/build_render.py` (the already-public team-deps renderer); `pyproject.toml` (packaging for `bobi/supervisor/` + Worker sources); `tests/integration/test_event_server.py` (wrangler leg → public job); `.github/workflows/ci.yml` (`event-server` job, publish-smoke step, `:277` placeholder NOTE); `tests/test_import_boundaries.py`; `docs/SELF_HOSTED_EVENT_SERVER.md`, `docs/EVENT_SERVER.md`, `docs/RELEASE_RUNBOOK.md`, `README.md`.

**Private `bobi-deploy`:** `event-server/` (Worker sources, config, tests); `bobi_deploy/src/bobi_deploy/supervisor/` (11 modules); `bobi_deploy/src/bobi_deploy/{build,deploy,scaffold,cli}.py` + `webapp/`; `Dockerfile` + `docker/`; `.github/workflows/{release-image,container,release,release-fleet,team-images,deploy-webapp,deploy-package,pypi,worker-integration}.yml`; `tests/test_worker_integration_workflow.py`; `deployments/` (ci canaries); `pypi-stub/`; `fly.webapp.toml`; containerized-deployment docs (split public/private).

**Private `moda-agents`:** `.github/workflows/deploy-agent-teams.yml` (the `.bobi-deploy` pin + checkout); `README.md` (identity paragraph); `agents/`, `deployments/` (unchanged by this plan).

### New

- `bobi/supervisor/` — the relocated package.
- `docs/ADMIN_PROTOCOL.md` (home TBD, Q3) — the versioned contract.
- Worker deploy runbook; reference-image runbook (incl. the PID-1 `--init` requirement).

## Questionables

### Q1 — CLI shape for the supervisor?

`bobi supervise` vs `bobi agent <name> supervise`. The sidecar is agent-scoped in practice (it supervises one manager), which argues for the latter at the cost of a longer pod-spec invocation.

### Q2 — Does `wrangler.jsonc` ship live or as an example?

Ship it with a placeholder KV id and a loud comment (matching how the repo handles required secrets), accepting that a fresh clone fails until the operator creates a namespace; `wrangler.example.jsonc` is the alternative if that failure mode proves confusing.

### Q3 — Where does the protocol spec live, and how strong is the promise?

New `docs/ADMIN_PROTOCOL.md` vs a section of `docs/EVENT_SERVER.md` (which already owns topics + security model). The harder half: once external consumers ship dashboards and an MCP client against it, the schema cannot change casually — decide whether `SUPERVISOR_VERSION` gates compatibility and what the deprecation window is.

### Q4 — Should `@moda-labs/bobi-events-core` ever be published again?

Its only consumer stops needing it. Keeping a maintained publish path is worth it only if third-party adapters for other runtimes are a goal — the core is the protocol, so that is the surface they would build against. `0.1.0` stays up regardless.

### Q5 — Does publishing the Worker change the durable tier's commercial positioning?

The plan's stance: the moat is **operating** the durable tier (SLA, upgrades, multi-tenancy), not possessing the source — the design partner runs it themselves either way. If that stance is wrong, this plan is wrong, and the fallback is a private distribution repo with automated subtree sync. Decide before Movement 1 lands, not after.

### Q6 — Does the `TEAM_DEPS` hook belong in the reference image?

Recommendation: yes. It is generic (default noop, byte-identical image when unused), its renderer is already public, and removing it forces a private Dockerfile fork — the exact thing this plan deletes. The counterargument ("nothing else" purism) buys no smaller image and costs a maintained fork.

## Phases

### Phase 1 — Worker to public (Lane A) `[ ]`

Move sources/config/tests; merge workspace config; parameterize KV id; rename the test header; write the Worker deploy runbook.

### Phase 2 — Sidecar to public (Lane B) `[ ]`

Move the 11 modules to `bobi/supervisor/`; add the CLI entry point (Q1); packaging; verify the hosted console still imports.

### Phase 3 — Admin protocol contract (Lane C) `[ ]`

Depends on B's placement. Topics, all nine command schemas, heartbeat/snapshot schema, the `SUPERVISOR_VERSION` promise (Q3), the bubble-security note.

### Phase 4 — Positioning and docs sweep (Lane D) `[ ]`

Depends on A. Rewrite the "managed deployment tier" line (Q5); enumerate the Worker as the fourth variant; sweep README + EVENT_SERVER.

### Phase 5 — Retire the repo-split bridges (Lane E) `[ ]`

Depends on A. Repoint the Worker to the workspace; delete the npm publish path and vendored tgz; fold Worker typecheck/units into the `event-server` job; add the wrangler-dev protocol job, deleting the `ci.yml:277` NOTE; leave `0.1.0` published.

**→ Gate R: cut a public release carrying Phases 1–2** (the wheel now ships `bobi/supervisor`; the sdist/wheel carry the Worker sources). Release work follows the runbook as always — this plan just marks the dependency.

### Phase 6 — Reference image to public (Lane F) `[ ]`

Gated on R. Move Dockerfile + `docker/` + `release-image.yml` + `container.yml`; delete the `BOBI_SUPERVISOR_SRC` machinery (sidecar via wheel; entrypoint on the public CLI); keep the `TEAM_DEPS` hook (Q6); migrate the generic container-contract docs; write the reference-image runbook (PID-1 `--init` documented); prove GHCR publishing from the new home with a dispatch dry-run.

### Phase 7 — Consolidate into moda-agents (Lane G) `[ ]`

Gated on F and R. Subtree-merge the `bobi-deploy` remainder into `moda-agents` (history preserved); drop the `.bobi-deploy` pin + checkout from `deploy-agent-teams.yml`; migrate open issues and repoint the eng-team bot; rewrite the README identity paragraph and `docs/RELEASE_RUNBOOK.md` (two-repo train); roll the fleet from the merged repo; **only after a green fleet roll**, archive `bobi-deploy` read-only.

## Proof of work

- **Worker**: spec suite + wrangler-dev protocol suite green in public CI (no Cloudflare credentials); a real `wrangler deploy` to a scratch account applies the `v1` DO migration and `/health` responds, from a clean clone following only the runbook.
- **Sidecar**: `bobi supervise` runs from a pip-installed wheel (no checkout, no shim), joins its bubble, publishes a heartbeat, executes a `restart` end to end — including with a deliberately wedged manager, since surviving a wedged manager is the sidecar's reason to exist.
- **Reference image**: from the runbook alone, `docker run --init` on a plain VM stands up an agent under sidecar supervision; the GHCR dispatch dry-run publishes multi-arch tags from the new home.
- **Fleet non-regression**: moda's Fly fleet runs on the pip-installed supervisor before the bundled copy is deleted; the fleet rolls green from the merged `moda-agents` before `bobi-deploy` is archived.
- **Release**: one full two-repo release executes cleanly under the rewritten runbook.
- **Import boundaries**: `tests/test_import_boundaries.py` green throughout.
- **Consumer proof**: a Kubernetes pod running the sidecar against a Terraform-deployed Worker, remote monitoring and control observed from outside the cluster, touching no private repo. This is the acceptance test the whole plan exists for.

## Lane map

| Lane | Scope | Depends on | Parallel with |
|---|---|---|---|
| A | Worker → public `event-server/` | — | B |
| B | Sidecar → `bobi/supervisor/` + CLI | — | A |
| C | Admin protocol spec | B (placement) | A, D, E |
| D | Positioning + docs sweep | A | B, C, E |
| E | Retire npm transport + `worker-integration.yml` | A | B, C, D |
| — | **Gate R**: public release carrying A+B | A, B | — |
| F | Reference image → public | R | — |
| G | Consolidate `bobi-deploy` → `moda-agents`, archive | F, R | — |

## Amendments

_None yet._

## Notes

### Why the wheel and the image are the delivery mechanisms

The target consumer already does `pip install bobi==<version>` and already extracts the bundled event server from `site-packages`; anything in the wheel reaches them with no grant, no fork, no drift. The sidecar adds zero third-party dependencies. The reference image extends the same posture (public PyPI, public npm core, public GHCR tags) to the container path.

### What publication does not give away

The Worker is an adapter of an already-public protocol whose three siblings are already public. The sidecar is a client of the control plane, not the control plane — the admin read model and hosted console stay private. The reference image contains only public inputs and always has. Publishing the producers does not publish the product; the durable tier's moat is operating it (Q5).

### Why consolidation is a correction, not a precedent

The public/private boundary proves something and stays. The `bobi-deploy`/`moda-agents` boundary, after the extractions, protects nothing and costs a pinned checkout, a nine-issue pin changelog, and a third leg on every release. The test for any future repo boundary: does it prove something, or only cost something?
