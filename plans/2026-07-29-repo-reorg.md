# Repo reorg: publish the product surface, consolidate the ops surface

> **Status:** Approved
> **Tracking issue:** none by decision (2026-07-29) · **Created:** 2026-07-29 · **Reviewed:** 2026-07-30 · **Last amended:** 2026-07-30
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

One reorganization, three movements, ending with two repos instead of three:

1. **Publish the product surface.** The Cloudflare event-server Worker, the admin sidecar, and the container image recipe move from private `bobi-deploy` into public `bobi-agent`, reaching consumers through the PyPI wheel and a reference image instead of a private repo grant:

- the **Cloudflare event-server Worker** (`event-server/src/{index,deployment-session,fleet,internal-auth}.ts`), which joins the three local event-server variants already public;
- the **admin sidecar** (`bobi_deploy/src/bobi_deploy/supervisor/`), restored as a first-class public CLI surface, `bobi agent <name> supervise`.

The admin protocol becomes a documented, versioned contract — that, not the code move, is what external consumers bind to.

2. **Retire the repo-split bridges.** The npm transport for `@moda-labs/bobi-events-core`, the `worker-integration.yml` cross-repo CI, and the exact-pin lockstep guards exist only to carry code across the private/public boundary. With the Worker home, they dissolve.
3. **Consolidate the ops surface.** What remains of `bobi-deploy` — the Fly deploy engine, the hosted console, the fleet workflows, the ci canaries — has exactly one consumer: moda's own fleet, whose teams and deployment configs live in `moda-agents`, which today carries a pinned checkout of the engine. Merge the remainder into `moda-agents` and archive `bobi-deploy`.

End state:

| Repo | Holds |
|---|---|
| `bobi-agent` (public) | framework wheel (now incl. `bobi/supervisor`), all four event-server variants (local, Slack socket, Discord gateway, **Worker**), the **reference image** (Dockerfile + `docker/` + GHCR release pipeline), admin protocol spec, deploy runbooks |
| `moda-agents` (private) | agent teams + deployment configs + the deploy engine (`bobi_deploy`) + hosted console + fleet/release workflows + ci canaries |
| `bobi-deploy` | archived, read-only |

The public/private *principle* is unchanged — public product versus moda operational IP — but its enforcement code is not: `tests/test_import_boundaries.py` encodes the CURRENT split as literal allowlists (`WORKER_ADAPTER_MODULES` must stay absent from `src/`; `PUBLIC_LOCAL_MODULES` is an exact-equality set; container-build tokens are banned under `bobi/`). Every one of those guards asserts the arrangement this plan reverses, so each lane that moves code also rewrites the guard that forbade the move — re-aimed at the NEW boundary, never weakened. This plan moves things that sat on the wrong side of that line, and removes the second private repo that no longer earns its keep once they're gone.

Nothing is redesigned in flight, with one deliberate exception recorded as D7: how moda's fleet bakes team-flavored images changes, because the current mechanism (rebuild the whole recipe with a `TEAM_DEPS` build-arg) requires the private engine to hold the recipe the plan is publishing. Everything else moves as-is; the only other content edits are the ones publication forces.

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

## Decisions

All six open questions were resolved 2026-07-30 (Zach) and are folded into the Solution below; recorded here with their rationale.

- **D1 — CLI surface: `bobi agent <name> supervise`.** Agent-scoped like the rest of the per-agent CLI; the sidecar supervises exactly one manager. The longer invocation is irrelevant — pod specs and entrypoints call it programmatically.
- **D2 — `wrangler.jsonc` ships live**, with a placeholder KV id and a loud comment (matching how the repo handles required secrets). A fresh clone fails loudly until the operator creates the namespace — explicit beats an extra copy step.
- **D3 — Protocol spec: dedicated `docs/ADMIN_PROTOCOL.md`; promise = versioned, additive-only pre-1.0.** Additive changes are free; a breaking change bumps `SUPERVISOR_VERSION` and carries a migration note; no formal deprecation window while direct consumers number two (moda's console, and the Worker's MCP fleet-control route — `plans/2026-07-30-mcp-fleet-control.md` — through which the partner's agents administrate). One pressure to respect: the MCP route publishes **tool schemas** over this same contract, and a tool schema is a harder binding than a document — a consumer's agent re-reads it at every `tools/list`, so a shape change is felt immediately rather than at the next read of a doc. That is why breaking changes get the full `SUPERVISOR_VERSION` + migration-note discipline rather than casual drift. Honest about maturity without freezing a protocol that is still evolving (bobi-deploy#45 open against the supervisor today).
- **D4 — npm publish path retired.** `@moda-labs/bobi-events-core@0.1.0` stays frozen on the registry (unpublishing breaks unknown consumers); the machinery deletes. If a third-party adapter ecosystem materializes, restoring it is a day's work — don't pay for it speculatively.
- **D5 — The durable tier is open self-host.** The moat is **operating** the tier (SLA, upgrades, multi-tenancy), never source access — the partner runs it themselves either way. Any future paid offering is managed hosting. The positioning line in `SELF_HOSTED_EVENT_SERVER.md` rewrites accordingly.
- **D6 — The `TEAM_DEPS` hook stays in the reference image.** Default-noop, byte-identical image when unused, renderer (`bobi/build_render.py`) already public; stripping it would recreate the private-Dockerfile fork this plan exists to delete.

Three further decisions were resolved 2026-07-30 (Zach) during design review, after the review verified claims the original draft had asserted:

- **D7 — moda's fleet builds team images `FROM` the published base image; the private engine stops holding the recipe.** The draft assumed Phase 6 was a file move. It is not: `bobi_deploy/pyproject.toml:51-62` force-includes `../Dockerfile`, `../docker/*.sh`, and `src/bobi_deploy/supervisor` into the deploy wheel, and `build.py:206-215` copies them into a staged build context for every checkout-less `bobi deploy`. Publishing the recipe orphans all of that — the wheel `moda-agents` installs at `BOBI_DEPLOY_REF` would stop building. Rather than keep the engine coupled to the recipe's location, the team bake becomes a three-line Dockerfile in `moda-agents` (`FROM ghcr.io/moda-labs/bobi:<version>`, `COPY` the rendered hook, `RUN` it). The 300-line recipe stays single-sourced in public `bobi-agent`; the engine never needs it again.

  The cost is real and was measured, not estimated, against two eng-team deploys. Today the `TEAM_DEPS` layer sits BELOW the volatile venv COPY (`Dockerfile:236` vs `:248`) so a framework-only release leaves the team bake cached; `FROM`-the-base inverts that, so every bobi release re-runs each team's bake. eng-team's bake (`#18`) is **256s**; the other three teams run ~2 min each; the four deploys are parallel matrix jobs, so the fleet-roll wall-clock cost is **~4 minutes**, bounded by eng-team. Two facts make that acceptable: a fully-cached eng-team deploy still takes 5.7 min (most of a deploy job is machine roll + health wait, not build), and on the 0.50.0 roll the bake re-ran ANYWAY — `#17 COPY .../moda-eng-team.sh` was not cached, because the rendered hook changed — so on the most recent real release this change would have cost approximately zero. The layer ordering protects less than its comment claims.

- **D8 — `fleet.ts` publishes with the other three Worker sources; its "deliberately PRIVATE" header is superseded.** The file's own header (lines 3-8) declares it private control-plane, "never shipped in the public local dev server," on the reasoning that a fleet-of-instances view is meaningless in a single-team local product. True for the local dev server; false for the Worker, and false for a K8s design partner running several deployments — who is the consumer this plan exists for. Its 450 lines are the record schemas, the `adminTopic`/`COMMAND_RESULT_TOPIC` wire constants, the KV folds, `reachability()`, and the operator-token-gated query builders — no moda business logic. It cannot stay private: `docs/ADMIN_PROTOCOL.md` (Lane 1) documents exactly its schemas, and `admin.py:52` requires the Python and TS sides match byte-for-byte, so a public contract with a secret server half is not a contract. The Notes section's claim that "the admin read model stays private" was wrong and is corrected: what stays private is the hosted console UI. Lane A deletes the superseded header comment.

- **D9 — the `x-moda-*` header namespace stays; only the test header's *gating* is documented.** The draft called for renaming `x-moda-test-secret` "to a vendor-neutral name." Review found five sibling headers — `x-moda-bubble`, `x-moda-algo`, `x-moda-timestamp`, `x-moda-nonce`, `x-moda-signature` — that are already the PUBLIC wire protocol, read by public `event-server/core/src/core.ts:601-605` and emitted by the public Python client. Renaming one of six is incoherent; renaming all six is a breaking protocol change across the wheel, all four server variants, and every deployed bubble, for cosmetics. The namespace is simply this protocol's public prefix. No rename. The publication edit that IS required is documentation: `/__test/resource-grants` is fail-closed (the route does not exist unless `TEST_GRANTS_SECRET` is set) and additionally bubble-authed — say so in the runbook so a reader does not mistake it for a bypass.

## Solution

### Movement 1 — publish the product surface

**Worker → public `event-server/worker/`, a THIRD workspace package.** The four TS sources (`index`, `deployment-session`, `fleet`, `internal-auth`), `wrangler.jsonc`, `worker-configuration.d.ts`, and the test tree move as their own npm workspace package alongside `core/` — they do NOT join the local variants in `event-server/src/`, and their build/test config does NOT merge into the existing `tsconfig.json`/`vitest.config.mts`. The draft assumed a merge; review established it is not possible, for two independent reasons:

- **tsconfig.** The public compile unit is deliberately Node-only — `lib: ["es2024"]`, `@types/node`, and a comment at `event-server/tsconfig.json` saying so — while the Worker needs `worker-configuration.d.ts` and Cloudflare's globals, whose `fetch`/`Request`/`Response` declarations conflict with `@types/node`'s. One `include` cannot serve both.
- **vitest.** The public config is `defineConfig({ test: { environment: "node" } })`; the Worker's is `defineWorkersConfig` with the `@cloudflare/vitest-pool-workers` pool, `wrangler.configPath`, `isolatedStorage: false`, and six miniflare bindings. These are mutually exclusive in a single config.

So: add `worker` to `event-server/package.json`'s `workspaces` array (currently `["core"]`), keep the Worker's own `tsconfig.json` + `vitest.config.mts` + `package.json` (renaming the package from `bobi-events-worker` as convenient), and have CI run the two suites side by side. `event-server/test/tsconfig.json` and the root typecheck stay pointed at the Node unit. **Constraint carried from the guard**: `tests/test_import_boundaries.py::test_no_path_aliases` walks every `tsconfig*.json` under `event-server/` and fails on any `paths`/`baseUrl`, so the Worker package must resolve `@moda-labs/bobi-events-core` through the workspace, never an alias.

Publication edits: parameterize the moda KV namespace id (`e84e897a…`) with a placeholder + `wrangler kv namespace create EVENTS` setup step (`name: bobi-events` becomes a documented default); delete `fleet.ts`'s superseded "deliberately PRIVATE" header comment (D8); no header renames (D9). The `DEPLOYMENT_SESSION` DO binding and `v1` `new_sqlite_classes` migration carry over verbatim; `wrangler.jsonc` ships as the real file, not an `.example` (D2). A deploy runbook covers: Workers Paid plan (SQLite-backed DOs), KV create, the `wrangler secret put` list, `wrangler deploy`, `/health` verification, and the fail-closed status of `/__test/resource-grants` (D9).

**Re-aim the import-boundary guard (Phase 1, same PR).** `tests/test_import_boundaries.py` fails the instant the Worker lands — by design, it is the guard against exactly this. Three of its assertions must be rewritten to describe the new boundary: `WORKER_ADAPTER_MODULES` (currently "must stay ABSENT from `src/`"), `PUBLIC_LOCAL_MODULES` (an exact-equality set over `event-server/src/`), and `TestEventServerCoreNeverImportsWorkerAdapter` (which forbids public sources importing the adapter). What survives and must stay strict: the events-core package boundary in both directions (`test_no_relative_import_into_core`, `test_core_never_imports_outside_itself`, `test_core_bare_imports_are_declared_dependencies`) — the Worker consuming core by package name is still the property that keeps core publishable — plus `test_no_path_aliases`, and default-deny classification for any new module under either package.

**Sidecar → `bobi/supervisor/`.** The 11 modules move as-is (dropping the now-moot "must not import its parent" note, keeping the discipline); the CLI entry point is `bobi agent <name> supervise` (D1). Packaging needs nothing: `pyproject.toml`'s wheel target is `packages = ["bobi"]`, so `bobi/supervisor/` is picked up as a subpackage automatically — the draft listed a packaging edit that does not exist.

**Correction to the draft's premise:** no private Python code imports `bobi_deploy.supervisor`. Review grepped the whole private tree; the only match outside the package is a prose comment at `webapp/runtime.py:311`. The hosted console consumes the sidecar's *heartbeats off the event bus*, not its API. Lane B therefore has no private-side import to update — it is strictly easier than drafted. The real private-side coupling is packaging, not imports: `bobi_deploy/pyproject.toml:62` force-includes `src/bobi_deploy/supervisor` into the deploy wheel, and `build.py:213` stages it into the image build context. Both retire under D7 (the sidecar arrives via the public wheel, baked into the published base image).

**Re-aim the container-token guard (Phase 2, same PR).** `tests/test_import_boundaries.py::TestNoContainerBuildInPublicPackage` scans EVERY file under `bobi/` for `\b(docker|dockerfile|flyctl)\b` against a two-file allowlist. The supervisor trips it in at least four places, one of them a runtime value rather than prose: `identity.py:41` returns the literal `"docker"` as a platform name, plus `identity.py:11,37-38,50`, `__main__.py:3`, and `supervision.py:66`. The guard's stated premise — "the image engine is deploy IP; the public product builds no docker images" (#707) — is a premise THIS plan reverses, so the honest fix is to restate the guard, not to bolt on exceptions: it should ban code that *assembles a build context or invokes docker*, and permit code that merely *names a container runtime*. Keep `test_allowlist_entries_stay_justified`, which fails on stale entries in both directions.

**Reference image → public `bobi-agent`.** The Dockerfile, `docker/` (entrypoint, healthcheck, noop-deps), `release-image.yml`, and the `container.yml` CI gate move to the public repo. Definition of the reference image: **everything needed to stand up bobi in a VM — the wheel, kb deps, the pinned brain CLIs (`claude`, `codex`, `aichat`), runtime tools, the sidecar — and nothing else.** Deltas the move forces:

- The supervisor arrives **via the wheel**: delete `ARG BOBI_SUPERVISOR_SRC`, the supervisor `COPY`, and the `bobi-supervisor` PYTHONPATH shim (`Dockerfile:275-282`); the entrypoint supervises through the public CLI. This gates the lane on a release carrying the sidecar. Two entrypoint edits fall out, both in `docker/docker-entrypoint.sh`: the exec line (`:611`) currently ends `bobi-supervisor -- --foreground "$@"`, whose `--`-then-flags shape must be re-derived for `bobi agent "${AGENT_NAME}" supervise` (D1) — the agent name moves from an env var the shim read into a CLI positional; and the misbuilt-image guard (`:605`, `command -v bobi-supervisor`) must test the public CLI instead. Keep the guard: an image where supervision is missing must still fail loudly at boot rather than run unsupervised.
- **PID-1 is a documented flag, not a baked init.** The image stays tini-free (tini-on-Fly is a documented boot-failure trigger, and moda's fleet still consumes this image); the runbook states `docker run --init` for VM/compose and an init/`shareProcessNamespace` for K8 pod specs.
- The **generic container contract docs** (one image/every tenant, identity via volume + env, the `/data` layout) migrate public with the recipe; Fly fleet mechanics stay behind for Movement 3.
- The `TEAM_DEPS` hook stays in the Dockerfile (D6): default-noop, byte-identical image when unused — a consumer who ignores it gets the "nothing else" image bit-for-bit; a consumer who wants baked team tools gets the same mechanism moda uses. Note the hook is now *two* mechanisms serving one purpose: the in-recipe `ARG TEAM_DEPS` for anyone building the recipe directly, and the `FROM`-the-base overlay moda's fleet uses (D7). Both render from the same public `bobi/build_render.py`.
- **The private engine is decoupled from the recipe (D7), not re-pointed at it.** Moving the Dockerfile breaks the deploy wheel's build outright — `bobi_deploy/pyproject.toml:51-57` force-includes `../Dockerfile`, `../docker/docker-entrypoint.sh`, `../docker/healthcheck.sh`, `../docker/noop-deps.sh` (plus `../scripts/*.sh`) from the repo root above it. This lane therefore also: deletes those force-includes; retires `resolve_assets`' binary-mode context staging (`build.py:203-217`) and its `BOBI_SUPERVISOR_SRC` arg; and adds the three-line team Dockerfile in `moda-agents` that `stage_team_deps`' rendered hook now feeds. `resolve_assets`' source-checkout branch (`build.py:184-193`, which points at `repo/Dockerfile`) has no repo to point at post-merge and retires with it.
- GHCR publishing needs one manual prerequisite the draft assumed away: a GHCR package is linked to the repo that published it, and `release-image.yml` authenticates with that repo's `GITHUB_TOKEN` (`permissions: packages: write`). A token from `bobi-agent` has no write access to `ghcr.io/moda-labs/bobi` until the package's settings grant the new repo access. Grant it BEFORE the dispatch dry-run, or the dry-run 403s. The `IMAGE:` env itself is unchanged and consumers never see the publishing repo. Prove the new home with a dispatch dry-run publish (the same verification #609 used).

**Admin protocol contract.** A dedicated `docs/ADMIN_PROTOCOL.md` — a contract consumers link to, with `EVENT_SERVER.md` pointing at it (D3) — covering: admin topic naming and bubble binding (commands are bubble-namespaced and fail-closed — only the target deployment can receive its own); the command/`command_result` schema for all nine commands (`restart`/`stop`/`start` ack immediately as `{"accepted": true}` with the effect surfacing on the heartbeat; `chat` is the only async command); the heartbeat/snapshot schema for dashboard consumers. Compatibility promise per D3: versioned, additive-only pre-1.0, breaking changes bump `SUPERVISOR_VERSION` (already in `snapshot.py`) with a migration note. Security of the write path rests on bubble namespacing, not obscurity — say so explicitly.

**Positioning sweep.** `docs/SELF_HOSTED_EVENT_SERVER.md` currently says durable replay "is the managed deployment tier, not this one" — publishing the Worker makes that false. Per D5, rewrite it so the durable Worker is a documented self-host option, add the Worker as the fourth variant wherever the three are enumerated, sweep `README.md` and `docs/EVENT_SERVER.md`.

### Movement 2 — retire the repo-split bridges

**The npm transport.** Inside the public repo the core is consumed through npm workspaces (`"@moda-labs/bobi-events-core": "*"`), never the registry; the published package exists solely for the private Worker (`ci.yml:87` says so verbatim). Once the Worker rejoins the workspace: repoint its dependency from the `0.1.0` pin to `"*"`; delete `pack:publish` + `event-server/core/scripts/pack.mjs`, `smoke` + `event-server/core/scripts/smoke.mjs`, and the "Events-core publish smoke" CI step (`ci.yml:196-198`); drop the npm leg from the release process. Per D4 the publish path is retired outright, and `0.1.0` stays frozen on the registry.

*Claim correction:* the draft listed `core/moda-labs-bobi-events-core-0.1.0.tgz` as a vendored file to delete. It is a **local build artifact, not tracked** — `.gitignore:26` ignores `event-server/core/*.tgz`. Deleting it from the repo is a no-op; the real edit is removing that now-purposeless `.gitignore` line.

**`worker-integration.yml`.** Dissolve, don't move. The split scaffolding (sibling checkout, pin check, `BOBI_TEST_ES_DIR` lockstep grep, dual toolchain setup) is meaningless in one repo. Two steps survive: Worker typecheck + miniflare units become a second suite in `ci.yml`'s `event-server` job — note the job's existing `tsc --noEmit` + `vitest run` cover the *Node* workspace only, so the Worker package's typecheck and workers-pool vitest run as their own steps (see the Movement 1 tsconfig/vitest split), not by widening the existing ones; the wrangler-dev protocol suite (`pytest tests/integration/test_event_server.py -k wrangler`, `BOBI_TEST_WRANGLER=1`) becomes a public CI job, replacing the placeholder NOTE at **`ci.yml:319-321`** (the draft said `:277`, which is the packaged-event-server regression step). `ci.yml:327`'s note about the SHA channel the private repo consumes goes too. Cadence is the executor's call — the private nightly existed largely to catch published-core drift, which this movement eliminates.

**This lane spans two repos.** The deletions above are public-side, but `worker-integration.yml` itself lives in `bobi-deploy`, along with `tests/test_worker_integration_workflow.py` — a test *of that workflow*, whose `test_protocol_suite_runs_from_public_checkout_against_this_worker` encodes the split assumption directly. Both delete in `bobi-deploy`. The Lane map records the second repo explicitly; a builder handed only the public half leaves a workflow testing a Worker that is no longer there.

### Movement 3 — consolidate the ops surface

Merge what remains of `bobi-deploy` into `moda-agents`, preserving history (`git subtree add` — 120 commits, 4.2 MB), then archive `bobi-deploy` read-only (archive, not delete: inbound links from issues, PRs, and past release notes keep resolving).

Contents that migrate: the `bobi_deploy` package (engine: `build.py`, `deploy.py`, `scaffold.py`, `cli.py`; `webapp/` hosted console), `deployments/` (ci-fleet canaries), the fleet/ops workflows (`release.yml`, `release-fleet.yml`, `team-images.yml`, `deploy-webapp.yml`, `deploy-package.yml`, `pypi.yml`), `fly.webapp.toml`, the Fly-specific docs, the engine tests, `scripts/`, and the `pypi-stub` name guard (already published; keep the source with the engine it guards).

Integration edits in `moda-agents`:

- `deploy-agent-teams.yml` drops the `.bobi-deploy` pinned checkout and installs the engine from the repo itself — the `BOBI_DEPLOY_REF` pin and its nine-issue comment block (`:73-83`) die. Engine and fleet config now version atomically. `BOBI_VERSION` stays: it now pins the public wheel AND the `FROM ghcr.io/moda-labs/bobi:<version>` base tag (D7), which is one fewer pin than today, not one more.
- The three-line team Dockerfile lands here (D7), fed by `stage_team_deps`' rendered hook. Two properties to preserve when the bake moves above the framework layer: the BuildKit secret path (`--mount=type=secret,id=MODA_SKILLS_TOKEN`, never a build-arg — a build-arg persists in `docker history`), and the host-side refusal when a hook references a secret nobody supplied.
- The README's identity paragraph is rewritten honestly: `agents/` + `deployments/` remain the reference shape for an outside consumer (published wheel, no framework checkout); the repo *also* carries moda's private deploy engine and ops. The dogfooding property lives in the teams' consumption path, which is unchanged.
- Open `bobi-deploy` issues migrate; the eng-team bot's dispatch watch repoints from `bobi-deploy` to `moda-agents`.
- `docs/RELEASE_RUNBOOK.md` is rewritten for the two-repo train: `bobi-agent` releases wheel + image; `moda-agents` bumps pins and rolls the fleet.

### Ordering

Lane 1 (Phases 1-5: Movements 1 and 2 in `bobi-agent`) lands first. A public release (**gate R**) then ships the wheel carrying `bobi/supervisor`. Lane 2 (the reference image) needs R, because the image installs the sidecar from the released wheel rather than a bundled copy. Lane 3 (Movement 3) needs Lane 2 complete — so the merge carries no Dockerfile and the fleet's `FROM`-the-base bake has a published base to build on — and R, so the version pin and the engine merge land together. The fleet must roll green from the merged repo **before** `bobi-deploy` is archived.

### Non-goals

- No restructuring of any component's internals; no change to the public/private *principle* (its enforcement code is re-aimed — see Purpose). D7's change to the fleet's image-bake path is the one deliberate in-flight redesign, and it is confined to how the bake is invoked; the recipe's contents are untouched.
- No new public UI — the hosted console stays private; external consumers build their own dashboards.
- The MCP admin server is enabled by this work, not part of it. It is **not** a client an external consumer writes against the spec: it lands as a route on the Worker this plan publishes (`plans/2026-07-30-mcp-fleet-control.md`), so the agentic control surface arrives with `event-server/` rather than after it. That plan is separately approved and does not gate any lane here — the only coupling is D3.
- No changes to how teams are authored, packaged, or installed.

## Relevant files

### Existing (verified 2026-07-29/30)

Paths are repo-root-relative. The draft wrote several `event-server/`-internal paths as if they were at the root; corrected below.

**Public `bobi-agent`:** `event-server/src/{local,slack-socket-local,discord-gateway-local,socket-driver-common}.ts`; `event-server/package.json` (`workspaces: ["core"]` + `"*"` dep — gains `worker`), `event-server/core/package.json` + `event-server/core/scripts/{pack,smoke}.mjs` (publish path being retired; the `.tgz` is untracked build output, see Movement 2); `event-server/tsconfig.json`, `event-server/vitest.config.mts`, `event-server/test/tsconfig.json` (Node compile unit — NOT merge targets, see Movement 1); `bobi/cli.py` (empty `supervise` slot; `bobi/watchdog.py` already deleted); `bobi/events/client.py:153`, `bobi/events/server.py:684` (public admin-path primitives); `bobi/build_render.py` (the already-public team-deps renderer); `pyproject.toml` (`packages = ["bobi"]` — needs no edit for the supervisor; the `[tool.hatch.build.targets.wheel.force-include]` + `hatch_build.py` event-server declarations DO need the Worker's files if the wheel is to carry them); `tests/integration/test_event_server.py` (wrangler leg → public job); `.github/workflows/ci.yml` (`event-server` job at the `Typecheck`/`Run event-server tests` steps, publish-smoke at `:196-198`, placeholder NOTE at `:319-321`, private-CI note at `:87` and `:327`); `tests/test_import_boundaries.py` (three guard families to re-aim); `.gitignore:26`; `docs/SELF_HOSTED_EVENT_SERVER.md`, `docs/EVENT_SERVER.md`, `docs/RELEASE_RUNBOOK.md`, `README.md`.

**Private `bobi-deploy`:** `event-server/` (Worker sources, config, tests — its own `tsconfig.json`, `vitest.config.mts`, `worker-configuration.d.ts`, `wrangler.jsonc` with KV id `e84e897a…`); `bobi_deploy/src/bobi_deploy/supervisor/` (11 modules); `bobi_deploy/src/bobi_deploy/{build,deploy,scaffold,cli}.py` (`build.py:176-227` `resolve_assets`, `:273+` `stage_team_deps`) + `webapp/`; `bobi_deploy/pyproject.toml:50-62` (the force-includes that break on the recipe move); `Dockerfile` (`:236` `ARG TEAM_DEPS`, `:248` volatile layer, `:275-282` supervisor shim) + `docker/` (`docker-entrypoint.sh:605,611`); `.github/workflows/{release-image,container,release,release-fleet,team-images,deploy-webapp,deploy-package,pypi,worker-integration}.yml`; `tests/test_worker_integration_workflow.py` (deletes with the workflow it tests); `deployments/` (ci canaries); `pypi-stub/`; `fly.webapp.toml`; containerized-deployment docs (split public/private).

**Private `moda-agents`:** `.github/workflows/deploy-agent-teams.yml` (`BOBI_VERSION` at `:72`, `BOBI_DEPLOY_REF` + nine-issue pin comment at `:73-83`, the `.bobi-deploy` checkout at `:182+`); `README.md` (identity paragraph); `agents/`, `deployments/` (unchanged by this plan).

### New

- `bobi/supervisor/` — the relocated package.
- `event-server/worker/` — the Worker as a third workspace package, with its own `tsconfig.json` / `vitest.config.mts` / `package.json` (D-review, Movement 1).
- `docs/ADMIN_PROTOCOL.md` — the versioned admin-protocol contract (additive-only pre-1.0, D3).
- `moda-agents`: the three-line team Dockerfile (`FROM ghcr.io/moda-labs/bobi:<version>` + hook COPY + RUN), per D7.
- Worker deploy runbook; reference-image runbook (incl. the PID-1 `--init` requirement).

## Phases

### Phase 1 — Worker to public (Lane 1) `[ ]`

Move sources/config/tests into `event-server/worker/` as a third workspace package (NOT merged into the Node compile unit); add it to `workspaces`; parameterize the KV id; delete `fleet.ts`'s superseded private header (D8); no header renames (D9); re-aim the three import-boundary guard families so the suite is green on the new boundary; write the Worker deploy runbook.

**Gate:** `pytest tests/test_import_boundaries.py` green *and* non-vacuous — the re-aimed guards must still fail when a genuinely-private module is planted under either package (prove it with a deliberate mutant, per the house negative-claim idiom). `npx tsc --noEmit` green for BOTH compile units; both vitest suites green.

### Phase 2 — Sidecar to public (Lane 1) `[ ]`

Move the 11 modules to `bobi/supervisor/`; add the `bobi agent <name> supervise` entry point; restate the container-token guard so it bans build-context assembly rather than the word "docker". No packaging edit needed (`packages = ["bobi"]` covers subpackages) and no private-side import to update (the console reads heartbeats off the bus, not the API).

**Gate:** `pytest tests/test_import_boundaries.py` green with a planted-mutant check that the restated token guard still catches real build-context code; `bobi agent <name> supervise --help` resolves from a built wheel in a clean venv.

### Phase 3 — Admin protocol contract (Lane 1) `[ ]`

Depends on B's placement. `docs/ADMIN_PROTOCOL.md`: topics, all nine command schemas, heartbeat/snapshot schema, the additive-only `SUPERVISOR_VERSION` promise, the bubble-security note.

### Phase 4 — Positioning and docs sweep (Lane 1) `[ ]`

Depends on A. Rewrite the "managed deployment tier" line per D5; enumerate the Worker as the fourth variant; sweep README + EVENT_SERVER.

### Phase 5 — Retire the repo-split bridges (Lane 1) `[ ]`

Depends on A. **Two repos.** Public: repoint the Worker's core dependency to `"*"`; delete the npm publish path (`pack:publish`, `smoke`, both scripts, the `ci.yml:196-198` step) and the now-purposeless `.gitignore:26` tgz line; add the Worker's typecheck + workers-pool vitest as their own steps in the `event-server` job; add the wrangler-dev protocol job, deleting the `ci.yml:319-321` NOTE and the `:87`/`:327` private-CI notes; leave `0.1.0` published, frozen. Private `bobi-deploy`: delete `worker-integration.yml` and `tests/test_worker_integration_workflow.py`.

**Gate:** `npm run smoke` and `pack:publish` are gone and nothing references them (`grep`); the wrangler-dev job runs green on a PR with no Cloudflare credentials in the environment; `pytest tests/` green in `bobi-deploy` after the workflow-test deletion.

**→ Gate R: cut a public release carrying Phases 1–2** (the wheel now ships `bobi/supervisor`). Release work follows the runbook as always — this plan just marks the dependency. **Open question for the release lane:** whether the wheel/sdist should also carry the Worker sources. The draft asserted they would, but the wheel's event-server payload is an explicit force-include list (`pyproject.toml:70-73` + `hatch_build.py`) built to ship the *embedded local server*, and nothing in the product runs the Worker from `site-packages` — a self-hoster deploys it with `wrangler` from a clone. Default to NOT adding them (smaller wheel, no new build-hook surface); the runbook points at the repo. Decide explicitly at the release, either way.

### Phase 6 — Reference image to public (Lane 2) `[ ]`

Gated on R. **Two repos.** Public: move Dockerfile + `docker/` + `release-image.yml` + `container.yml`; delete the `BOBI_SUPERVISOR_SRC` machinery and rewrite the entrypoint's exec + guard lines onto `bobi agent <name> supervise`; keep the `TEAM_DEPS` hook (D6); migrate the generic container-contract docs; write the reference-image runbook (PID-1 `--init` documented). Private: delete the orphaned force-includes (`bobi_deploy/pyproject.toml:50-62`) and retire `resolve_assets`' context staging + its source-checkout branch (D7). Grant the public repo write access to the `ghcr.io/moda-labs/bobi` package, then prove the new home with a dispatch dry-run.

**Gate:** the deploy wheel still builds after the force-include deletion (`pip wheel` / `hatch build` green — this is the failure D7 exists to prevent); the dispatch dry-run pushes multi-arch tags from `bobi-agent`; a `docker run --init` of the published tag stands up an agent under supervision with no `bobi-supervisor` shim present.

### Phase 7 — Consolidate into moda-agents (Lane 3) `[ ]`

Gated on F and R. Subtree-merge the `bobi-deploy` remainder into `moda-agents` (history preserved); add the three-line team Dockerfile (D7) and cut `deploy-agent-teams.yml` over to it; drop `BOBI_DEPLOY_REF` + the `.bobi-deploy` checkout; migrate open issues and repoint the eng-team bot; rewrite the README identity paragraph and `docs/RELEASE_RUNBOOK.md` (two-repo train); roll the fleet from the merged repo; **only after a green fleet roll**, archive `bobi-deploy` read-only.

**Gate:** a full fleet roll green from the merged repo, with eng-team's baked tools verified present in the running container (the hook's `requires[].check` re-run is the existing mechanism); roll wall-clock recorded against the ~4-minute D7 estimate so the prediction is checked, not assumed.

### Convergence gate `[ ]`

Runs after the last lane merges — lane gates prove the pieces, this proves the seams. Fuse-runnable portion: full `pytest tests/` + both TS suites + both typechecks against a locally merged preview of all lanes. Deferred portion (needs the real merge sequence): the Consumer proof below, and one full two-repo release under the rewritten runbook.

## Proof of work

- **Worker**: spec suite + wrangler-dev protocol suite green in public CI (no Cloudflare credentials); a real `wrangler deploy` to a scratch account applies the `v1` DO migration and `/health` responds, from a clean clone following only the runbook.
- **Sidecar**: `bobi agent <name> supervise` runs from a pip-installed wheel (no checkout, no shim), joins its bubble, publishes a heartbeat, executes a `restart` end to end — including with a deliberately wedged manager, since surviving a wedged manager is the sidecar's reason to exist.
- **Reference image**: from the runbook alone, `docker run --init` on a plain VM stands up an agent under sidecar supervision; the GHCR dispatch dry-run publishes multi-arch tags from the new home.
- **Fleet non-regression**: moda's Fly fleet runs on the pip-installed supervisor before the bundled copy is deleted; the fleet rolls green from the merged `moda-agents` before `bobi-deploy` is archived; eng-team's baked tools survive the move to a `FROM`-the-base bake (D7), verified by the hook's own `requires[].check`.
- **Release**: one full two-repo release executes cleanly under the rewritten runbook.
- **Import boundaries**: *corrected from the draft.* "Green throughout" was not achievable and would have sent a builder into a fight with the guard: `tests/test_import_boundaries.py` asserts the CURRENT split as literal allowlists, so it MUST go red the moment Lane A or B lands and MUST be rewritten in the same PR. The real proof is stronger than green-throughout: after each rewrite the suite is green **and** demonstrably non-vacuous — a planted module that genuinely violates the NEW boundary still fails it. Green-with-a-weakened-guard is the failure mode this criterion exists to catch.
- **Consumer proof**: a Kubernetes pod running the sidecar against a Terraform-deployed Worker, remote monitoring and control observed from outside the cluster, touching no private repo. This is the acceptance test the whole plan exists for.

## Lane map

The draft cut A–E as five concurrent same-repo lanes. Review collapses them to one. Same-repo parallelism is justification-required — it must buy elapsed time that matters — and here it buys almost none while costing a great deal: C depends on B, D and E both depend on A, and all five touch `tests/test_import_boundaries.py`, `ci.yml`, and `event-server/package.json`, so five concurrent branches would spend their savings on conflict resolution and a fuse gate. Five PRs also means five review passes over what is one coherent change. No deadline argues the other way. Phases 1–5 are checkpoints inside one lane, worked through in sequence in one session and PR.

Cross-repo lanes are unaffected by that rule — separate repos force separate PRs — so the private-side deletions ride their own PRs and are called out explicitly below.

**Topology: STACKED.** Lane 2 consumes Lane 1's runtime surface (the image installs the sidecar Lane 1 published); Lane 3 consumes Lane 2's (the fleet consumes the image Lane 2 published). Each lane bases on its predecessor; landings unwind bottom-up.

| Lane | Phases | Repos | Depends on | Marker mode |
|---|---|---|---|---|
| 1 | 1–5: Worker, sidecar, protocol spec, positioning sweep, bridge retirement | `bobi-agent` (+ a small `bobi-deploy` PR: delete `worker-integration.yml` + its test) | — | `concurrent` (cross-repo) |
| — | **Gate R**: public release carrying Phases 1–2 | `bobi-agent` | 1 | — |
| 2 | 6: reference image → public | `bobi-agent` (+ `bobi-deploy` PR: force-includes, `resolve_assets`) | 1, R | `concurrent` |
| 3 | 7: consolidate + archive | `moda-agents` (+ `bobi-deploy` archive) | 2, R | `concurrent` |

Every lane is `concurrent` mode: each spans more than one repo, so plan markers flip as status-only commits to `bobi-agent`'s main referencing the code PR, never inside a feature branch.

**Interface locks to relay.** Lane 1 → Lane 2: the exact `bobi agent <name> supervise` invocation and flag set the entrypoint must call (Lane 2 cannot write the exec line against an assumption of it). Lane 2 → Lane 3: the published base image's tag shape and what the `FROM`-the-base overlay may rely on already being present.

**Dispatch (decided 2026-07-30, Zach): no dispatch issues.** Split cut none. Lane 1 is worked directly in an interactive session against this plan, which is the spec; Lanes 2 and 3 are cut only once their predecessor lands. The reason not to file them ahead: filing an issue in `bobi-agent` or `moda-agents` dispatches the eng-team bot, and Lane 2's real blocker is **Gate R — a hand-run public release with no issue number**, so a `depends on #N` line cannot express it and the bot's Ready gate would bounce the issue on a dependency it can't see. Marker mode is unchanged (`concurrent`, since Lane 1 spans two repos): markers flip as status-only commits to `bobi-agent`'s main referencing the code PR, never inside the feature branch.

## Amendments

### 2026-07-30 — Split: three lanes confirmed, no dispatch issues cut

Split ran against the reviewed Lane map and changed nothing about the topology — three stacked cross-repo lanes, all `concurrent` marker mode, stand as reviewed. What it settled is dispatch: **no dispatch issues** (Zach, 2026-07-30). Lane 1 is built in an interactive session against this plan rather than routed through a filed issue; Lanes 2 and 3 are cut only when their predecessors land. Rationale recorded in the Lane map. No change to scope, phases, gates, or proof of work.

## Notes

### Why the wheel and the image are the delivery mechanisms

The target consumer already does `pip install bobi==<version>` and already extracts the bundled event server from `site-packages`; anything in the wheel reaches them with no grant, no fork, no drift. The sidecar adds zero third-party dependencies. The reference image extends the same posture (public PyPI, public npm core, public GHCR tags) to the container path.

### What publication does not give away

The Worker is an adapter of an already-public protocol whose three siblings are already public. The reference image contains only public inputs and always has. Publishing the producers does not publish the product; the durable tier's moat is operating it, not the source (D5).

*Corrected 2026-07-30:* the draft claimed "the admin read model and hosted console stay private." Only the second half survives. The read model is `fleet.ts` and it publishes (D8) — it has to, since `docs/ADMIN_PROTOCOL.md` documents exactly its schemas and `admin.py:52` binds the two sides byte-for-byte. What stays private is the **hosted console UI** (`bobi_deploy/webapp/`) and moda's operational configuration — the fleet's identities, secrets, and deployment topology — none of which is in the Worker. Publishing a read model that folds heartbeats is not publishing the heartbeats.

Two further things reviewed for leakage and cleared: `internal-auth.ts` bakes no secrets (it encodes and decodes a value injected at runtime), and the `/__test/resource-grants` route is fail-closed — it does not exist unless `TEST_GRANTS_SECRET` is set, and it is bubble-authed on top of that.

### Why consolidation is a correction, not a precedent

The public/private boundary proves something and stays. The `bobi-deploy`/`moda-agents` boundary, after the extractions, protects nothing and costs a pinned checkout, a nine-issue pin changelog, and a third leg on every release. The test for any future repo boundary: does it prove something, or only cost something?
