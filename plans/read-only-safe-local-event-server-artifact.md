# Read-only-safe local event server artifact

> **Status:** In review
> **Tracking issue:** moda-labs/bobi-agent#798 · **Created:** 2026-07-23 · **Last amended:** 2026-07-23 (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Make every standard Bobi Python distribution a complete, immutable runtime artifact for its embedded local event server.
Fresh wheel, `uv tool`, and Homebrew-backed installations must start the local Node server while the installed `bobi` package remains read-only, without running npm or generating JavaScript inside `site-packages`.

The complete version is the simplest version here: build and ship one self-contained JavaScript bundle, then execute it directly.
Shipping `node_modules`, adding a mutable runtime cache, weakening the write guard, or replacing Node would add more system than the defect requires.

## Problem

Issue [#798](https://github.com/moda-labs/bobi-agent/issues/798) was reported against Bobi v0.45.0 installed through Homebrew and `uv tool` on macOS `darwin 25.5.0`.
The affected `gtm-team` runtime expected a local event server on `localhost:8080`.
The defect remains present on `main` at `50ff18c` after the v0.48.0 Slack Socket Mode work.

Fresh standard wheels contain event-server TypeScript sources and npm manifests, but not `event-server/node_modules` or `event-server/dist/local.js`.
`bobi.events.server.ensure_running()` finds the bundled event-server directory, sees its workspace dependency or bundle missing, and runs `npm install --omit=dev` followed by a local build.
`bobi.runtime_guard` intentionally removes write bits from non-editable installed Bobi package and `.dist-info` roots before normal runtime work.
The runtime npm command therefore tries to create `site-packages/bobi/event-server/node_modules` inside a read-only tree, fails with `EACCES`, and never produces `dist/local.js`.
The local server never binds, manager retries back off, `doctor` reports it unavailable, and inbound Slack, GitHub, Linear, WhatsApp, and Discord traffic cannot reach local agent sessions.

The production failure chain is:

```text
Python distribution build
  -> force-includes event-server sources and npm manifests
  -> excludes dist/local.js and node_modules

Installed runtime guard
  -> recognizes a non-editable Bobi distribution
  -> removes all write bits from bobi/ and bobi-*.dist-info/

Local event-server startup
  -> packaged bundle is absent
  -> workspace dependencies are absent
  -> npm install runs inside the frozen package
  -> EACCES while creating node_modules
  -> local.js is never built
  -> Node server never starts
```

The original report reproduced the failure with representative directory mode `555` and file mode `444`.
Temporarily restoring owner-write permission, running `npm ci --no-audit --no-fund`, restarting the local server, and freezing the populated tree again restored service.
That workaround confirms package mutation is the failing boundary, but it is not acceptable product behavior because it weakens the immutable-install contract and cannot self-heal safely.

A production-shaped local reproduction built a real wheel, installed it in isolation, applied the runtime guard, and invoked the installed launcher.
The wheel contained the TypeScript and manifests but neither `node_modules` nor `dist/local.js`.
The first startup invoked npm and failed against the frozen package.
An exploratory build without `--external:ws` produced a bundle that started with runtime module resolution pointed at an empty location, validating the selected artifact shape.

The concrete code conflict, reverified on 2026-07-23, is:

- `pyproject.toml` force-includes event-server sources and manifests into the wheel while the sdist explicitly excludes `event-server/dist` and both `node_modules` trees.
- `bobi/runtime_guard.py` deliberately protects non-editable installed Bobi package and distribution metadata roots as read-only.
- `bobi/events/server.py::_needs_install()` and `ensure_running()` cause `npm install --omit=dev` to run in the bundled installed directory.
- `bobi/events/server.py::_build_local()` has a second Python-owned esbuild command and may create `.npm-cache` in that directory.
- `event-server/package.json` marks `ws` external in `build:local`, so a shipped `local.js` still requires runtime package resolution.
- The local entry now also imports the Discord and Slack persistent-socket drivers.
  The packaged artifact must therefore prove HTTP, WebSocket delivery, Discord Gateway wiring, and Slack Socket Mode wiring still bundle and start from the exact distribution artifact.

Existing seams already solve most of the problem:

- `_find_event_server_dir()` already distinguishes the installed bundled candidate from the repository candidate by ordered paths.
- `_needs_build()` already detects missing and stale bundles in a source checkout.
- `_run_npm()` already captures bounded npm output and surfaces the failed command.
- The remote `event_server_url` guard and healthy-server check already return before local build work.
- `bobi.runtime_guard` already distinguishes non-editable distributions from source and editable installs.
- `event-server/package-lock.json` already pins the complete Node build graph.
- `tests/integration/test_event_server.py` already exercises real health, registration, webhook ingestion, WebSocket delivery, bind behavior, and provider protocol paths.
- `tests/integration/test_slack_socket_mode.py` and the event-server Vitest suites already cover the socket transports that are now part of `local.js`.
- `tests/test_event_server_launch.py` already covers launcher ordering, remote behavior, source rebuilds, and npm diagnostics.
- `tests/test_packaging.py` already guards the sdist-to-wheel relationship.
- Main CI already provides Node 20 to event-server and non-Claude integration jobs.
- Release CI already builds one sdist and wheel and installs the exact wheel, but its public wheel smoke checks only `bobi --version`.
- The current [Homebrew formula](https://github.com/moda-labs/homebrew-bobi-agent/blob/main/Formula/bobi.rb) downloads the published PyPI sdist and installs its build path into a formula virtualenv.
  A verified no-Node wheel-from-sdist path therefore models the formula boundary.

## Solution

Build `event-server/dist/local.js` once from the committed Node lockfile while producing the Python distribution.
Bundle all required JavaScript, including `ws`, into that file.
Generate a deterministic `event-server/dist/local.inputs.json` manifest with the bundle-input hashes and exact build-tool versions.
Ship both files under `event-server/dist/` in the sdist and `bobi/event-server/dist/` in the wheel.
Installed startup will treat that wheel-owned file as canonical and execute it directly without npm, JavaScript compilation, timestamp freshness checks, or package-tree mutation.

Source and editable checkouts keep lazy developer rebuilds.
That path uses the same `npm run build:local` definition, installs exact locked build dependencies with `npm ci --no-audit --no-fund` only when needed, and compares content digests to distinguish dependency-lock drift from source-only drift.

### Distribution artifact contract

- Every standard Bobi wheel contains a non-empty `bobi/event-server/dist/local.js`.
- Every standard wheel and sdist contains the matching non-empty `local.inputs.json` manifest.
- Every Bobi sdist contains a non-empty `event-server/dist/local.js`, its manifest, and the custom build hook needed by wheel-from-sdist builds.
- A wheel built from an unmodified sdist verifies the manifest and reuses that sdist artifact without Node, npm, registry, or network access.
- A wheel build from a patched source archive rebuilds with the supported Node toolchain or fails explicitly.
  It never reuses a manifest that no longer matches the archive inputs.
- A direct standard wheel built from a VCS checkout generates a fresh artifact even if an ignored local bundle already exists.
- An sdist built from a VCS checkout always generates a fresh artifact.
- A standard wheel built outside VCS may reuse a present, non-empty source-archive artifact.
- A standard wheel built outside VCS without a valid artifact builds one or fails explicitly.
  It never emits an incomplete wheel.
- Editable wheel builds skip packaged-artifact generation and continue to import the writable source checkout.
- The wheel keeps the existing event-server sources and manifests for traceability and current path discovery.
- Neither archive contains `node_modules`, `.npm-cache`, temporary staging paths, or any other npm install state.
- Generated JavaScript remains ignored in a source checkout and is never committed.
- The bundle targets Node 20 and contains all required workspace and third-party JavaScript.
- `ws` optional native performance modules remain optional.
  The artifact must start and complete a WebSocket round trip with no runtime modules available, exercising the supported pure-JavaScript fallback.
- Direct-wheel and sdist-derived builds in the same clean build environment produce byte-identical `local.js` artifacts.
- The build emits an esbuild metafile for dependency and external-import inspection but does not ship it as runtime state.
- Every bundled third-party dependency is license-audited, required legal comments are preserved, and any required notices ship in both archives.
- No public Python API, event protocol, webhook route, authentication rule, environment variable, or wire payload changes.

Hatch build hooks run before target construction and may add an absolute staged file to `build_data["force_include"]`.
This is the supported extension seam for target-specific artifact inclusion.
The implementation should use Hatchling's built-in custom hook and Python standard-library code, without another Python build dependency.
Reference: [Hatch build-hook interface](https://hatch.pypa.io/dev/plugins/build-hook/reference/) and [Hatch build configuration](https://hatch.pypa.io/dev/config/build/).

### Build path

Add repository-level `hatch_build.py` and enable its custom hook in `pyproject.toml`.
The hook owns one isolated staging flow:

```text
source checkout
  -> create temporary staging directory
  -> copy package.json, package-lock.json, tsconfig, src/, and core/
  -> use a staging-local writable npm cache
  -> verify Node major version 20
  -> npm ci --no-audit --no-fund
  -> npm run build:local
  -> require non-empty dist/local.js and inspect the esbuild metafile
  -> generate and validate local.inputs.json
  -> force-include the runtime artifact, manifest, and required notices
  -> keep staging alive while Hatch constructs the target
  -> clean staging during hook finalization or any earlier failure

sdist source archive
  -> validate carried local.js against local.inputs.json
  -> force-include both under bobi/event-server/dist/ in the wheel
  -> invoke no npm command
```

The staged input set must include every input that can affect the local bundle:

- `event-server/package.json`
- `event-server/package-lock.json`
- `event-server/tsconfig.json`
- `event-server/src/**/*.ts`
- `event-server/core/package.json`
- `event-server/core/tsconfig.json`
- `event-server/core/src/**/*.ts`

This includes the current Discord and Slack socket driver sources behind `event-server/src/local.ts`.
The lockfile provides `esbuild`, `ws`, `@chat-adapter/slack`, TypeScript types, and the workspace link needed to resolve all bundle imports.

The hook records Node, npm, and locked esbuild versions in build diagnostics and in `local.inputs.json`.
It verifies Node major version 20 before running npm.
Failures name the command, exit code, relevant path, and bounded stdout or stderr.
Missing Node, missing npm, lockfile drift, registry failure, staging-copy failure, esbuild failure, missing output, and empty output all abort distribution construction.
Temporary staging stays on the hook instance until Hatch has consumed every forced include and is cleaned by hook finalization.
Earlier failures clean it before raising.
No build step writes `dist`, `node_modules`, or a cache into the source checkout.

### One JavaScript build definition

Change only the existing `event-server/package.json` `build:local` script to bundle `ws` instead of marking it external, preserve legal comments, and emit an esbuild metafile for the build audit.
Both the Hatch hook and the source-checkout launcher call that script.
Delete the Python-side duplicate esbuild command and `npm exec` fallback from `bobi/events/server.py`.

esbuild recursively inlines imported dependencies when bundling unless they are marked external.
The release-critical runtime check therefore starts the output from an isolated directory with runtime module resolution unavailable and proves real WebSocket traffic.
The metafile may list Node built-ins and the explicitly allowlisted optional `ws` native-addon probes as external.
Any other external import fails the build.
Reference: [esbuild bundle behavior](https://esbuild.github.io/api/#bundle) and [`ws` optional performance modules](https://github.com/websockets/ws#opt-in-for-performance).

### Installed and source startup paths

Preserve the remote-server guard and healthy-server check as the first two decisions.
After `_find_event_server_dir()` returns, use the existing ordered candidate boundary to distinguish the installed bundled layout from the source layout.

```text
remote configured? -> return "skipped"
server healthy?    -> return "connected"

installed layout
  -> require dist/local.js
  -> present and non-empty: spawn Node directly
  -> missing or empty: raise PackagedEventServerArtifactError
  -> never call npm or any builder

source/editable layout
  -> compare local.inputs.json with all declared build inputs
  -> fresh: spawn Node directly
  -> stale or missing:
       -> compare the dependency-input digest with the ignored source dependency stamp
       -> missing, partial, or lock-drifted dependencies: npm ci --no-audit --no-fund
       -> npm run build:local
       -> require non-empty output and refresh the manifest
       -> spawn Node
```

`PackagedEventServerArtifactError` is an actionable `RuntimeError` subclass.
Its message states that the installed Bobi distribution is incomplete and directs the operator to reinstall or upgrade.
The installed path must not call `_needs_install()`, `_run_npm()`, `_build_local()`, or another write-producing command.
It must not compare source mtimes because archive extraction timestamps are not artifact freshness.

Source freshness hashes the root and workspace package manifests, lockfile, TypeScript configuration, and all root/workspace TypeScript sources.
The source dependency check requires the workspace link, local esbuild binary, and an ignored dependency stamp matching the manifest and lockfile digest.
Missing, partial, or lock-drifted source dependencies use `npm ci`, not `npm install`, so development rebuilds remain lockfile-exact and include required build dependencies.
Existing npm error output and exit-code reporting remains intact.

### Runtime guard, security, and observability

Do not change `bobi/runtime_guard.py` protected-root selection, chmod policy, mutation window, integrity checks, or rescue behavior.
The wheel's `RECORD` continues to cover the shipped JavaScript artifact.
The packaged regression test applies the real guard and proves the installed event-server tree remains read-only and byte-for-byte unchanged through startup.

The design reduces runtime attack surface because ordinary agent startup no longer downloads npm packages or executes dependency build scripts.
No protected root becomes writable and no same-UID chmod workaround is automated.
Build-time dependency resolution uses the committed lockfile.
The npm registry remains a distribution-build supply-chain boundary, covered by lockfile review and CI dependency review.

Existing process logs, health checks, startup timeout, and manager retry behavior remain the observability surface for Node startup.
Build failures move earlier and fail the artifact pipeline loudly.
A missing packaged bundle raises its named error with reinstall guidance instead of attempting silent repair.

### Performance and capacity

Installed startup becomes faster and more predictable because dependency inspection, npm installation, and esbuild compilation leave the runtime path.
The only added runtime work is a packaged-artifact existence and non-empty check before spawning Node.
The compressed wheel grows by one self-contained bundle and remains much smaller and simpler than a wheel containing `node_modules`.
Standard source distribution builds become slower because they install the locked Node graph in isolated staging.
CI npm caching may reduce download cost, but correctness must not depend on a shared writable cache.
The running server gains no database, fan-out, memory-growth, request-latency, or concurrency change.

### Delivery, rollback, and non-goals

The feature lands on `main` without a version bump or `CHANGELOG.md` edit.
It reaches users through the next normal release artifact.
Released installations through v0.48.0 remain affected until upgraded and may use the reporter's manual permission workaround only as an emergency recovery.
Post-release verification installs the published artifact through Homebrew or `uv tool`, starts the embedded local server, and receives a successful health response.

There is no mixed-version protocol concern because the shipped bundle comes from the same local server source and changes no wire contract.
Rollback is a normal git revert followed by a replacement release if distribution construction or Node startup regresses.
There is no database, schema, state, data, or protocol migration.

In scope:

- A custom Hatch build hook that generates the local event-server bundle outside the source tree.
- Required inclusion of the generated artifact in both sdist and wheel.
- A standalone local Node 20 bundle with no required runtime npm packages.
- Installed-package startup that requires and executes the shipped artifact without npm or package writes.
- A deterministic `npm ci` plus `npm run build:local` source and editable fallback.
- A named installed-artifact error with reinstall or upgrade guidance.
- Unit, packaging, production-shaped integration, protocol, socket-transport, CI, and release-smoke coverage.
- Documentation updates for embedded installed startup and cloned standalone startup.
- Pull-request proof showing the failing frozen-wheel reproduction and the passing immutable-wheel flow.

Not in scope:

- Weakening, bypassing, or adding an event-server exception to the runtime write guard.
- Temporarily unfreezing `site-packages` during ordinary startup.
- Vendoring the full `node_modules` tree into the wheel.
- Moving npm state into a user cache or versioned writable application directory.
- Adding an `event-server repair` command.
- Changing Cloudflare Worker code, local event protocol, webhook routes, provider normalizers, authentication, or payloads.
- Removing Node 20 as the embedded server runtime.
- Producing a native executable.
- Changing manager retry policy or process lifecycle beyond surfacing a missing artifact clearly.
- Changing Discord Gateway or Slack Socket Mode behavior.
- Frontend, setup UI, or other visual changes.
- Version bumps or `CHANGELOG.md` edits in the implementation PR.

Alternatives considered:

1. **Ship the self-contained bundle - selected.**
   This preserves immutable installs, adds one executable artifact, works across Python installation methods, and removes npm from ordinary runtime startup.
2. **Vendor `node_modules`.**
   This ships thousands of files, retains runtime module resolution, grows the wheel substantially, and may carry platform-sensitive optional packages.
3. **Install into a writable runtime cache.**
   This preserves runtime npm and creates a new subsystem for cache versioning, locking, integrity, partial-install recovery, garbage collection, and path wiring.
4. **Unfreeze or exempt the event server.**
   This contradicts the reviewed runtime security boundary and permits framework-owned executable code to mutate outside the release artifact.
5. **Add a repair command.**
   This still depends on runtime npm and package mutation.
   Reinstalling or upgrading to a complete wheel is the safe repair.

## Relevant files

### Existing (verified 2026-07-23)

- `pyproject.toml` - Hatchling target configuration currently includes event-server sources and excludes generated bundle/install state.
- `event-server/package.json` - owns the single `build:local` command and currently leaves `ws` external.
- `event-server/package-lock.json` - pins the Node build and runtime dependency graph.
- `event-server/tsconfig.json` - root TypeScript build input.
- `event-server/src/local.ts` - local Node entry point bundled into `dist/local.js`.
- `event-server/src/discord-gateway-local.ts` - Discord persistent-socket runtime included by the local entry.
- `event-server/src/slack-socket-local.ts` - Slack Socket Mode runtime included by the local entry.
- `event-server/src/socket-driver-common.ts` - shared socket-driver mechanics included by the local entry.
- `event-server/core/package.json` - workspace manifest and export map used during bundling.
- `event-server/core/tsconfig.json` - workspace TypeScript build input.
- `event-server/core/src/` - shared protocol, normalizers, channel code, and socket protocols included by the local entry.
- `bobi/events/server.py` - local directory discovery, npm/build helpers, startup ordering, Node spawn, health, registration, and process logging.
- `bobi/runtime_guard.py` - immutable installed-package policy that the fix must preserve unchanged.
- `tests/test_event_server_launch.py` - launcher decision and failure-path unit coverage.
- `tests/test_packaging.py` - archive inclusion and public-distribution invariants.
- `tests/test_runtime_guard.py` - guard policy coverage.
- `tests/integration/test_event_server.py` - real local protocol, bind, registration, webhook, and WebSocket integration harness.
- `tests/integration/test_slack_socket_mode.py` - Python-to-local-server Slack Socket Mode integration coverage.
- `event-server/test/` - TypeScript protocol and local socket-driver tests.
- `.github/workflows/ci.yml` - Node 20 event-server and non-Claude integration jobs.
- `.github/workflows/release.yml` - exact wheel/sdist build, installation, smoke, and publication flow.
- `docs/EVENT_SERVER.md` - embedded local runtime architecture and startup contract.
- `docs/SELF_HOSTED_EVENT_SERVER.md` - cloned standalone local-server setup.

### New

- `hatch_build.py` - custom Hatch hook that stages, generates, validates, and force-includes the target-specific bundle artifact.
- `tests/integration/test_packaged_event_server.py` - production-shaped regression and release-smoke harness crossing archive construction, immutable install, Node startup, HTTP health, registration, webhook ingestion, and WebSocket delivery.

Generated but not committed:

- `event-server/dist/local.js` - self-contained local-server runtime.
- `event-server/dist/local.inputs.json` - sorted bundle-input hashes plus the exact Node, npm, and esbuild versions used to produce the runtime.
- `event-server/dist/local.meta.json` - esbuild dependency and external-import evidence consumed by the build audit but omitted from the archives.
- `event-server/node_modules/.bobi-lock-digest` - ignored source-only dependency stamp written after successful `npm ci`.
- A third-party notice artifact if the bundled-dependency license audit requires one.

## Questionables

Gate 1 approval resolves each Questionable to its recommended option unless Zach explicitly selects another.
Implementation remains blocked until that approval.

The reviewed issue design selected a self-contained build-time bundle over full `node_modules` vendoring, runtime caches, write-guard exceptions, repair commands, or Node replacement.
Gate 1 approval accepts that architecture, the recommended resolutions below, and the one-lane scope.

### Q1 - How should a carried bundle prove that it matches its inputs?

**A. Content-addressed input manifest - recommended.**
Generate a deterministic manifest of SHA-256 hashes for every declared bundle input plus the relevant build-tool versions.
Ship it beside `local.js` in the sdist, verify it before wheel-from-sdist reuse, and use the same input digest for source-checkout freshness and lockfile-exact dependency decisions.
If a source archive was patched, the hook rebuilds when the supported Node toolchain is available or fails explicitly rather than shipping stale executable code.

**B. Trust a non-empty source-archive bundle and retain mtime freshness.**
This keeps the hook smaller, but an exported or patched source archive can silently pair changed TypeScript or manifests with stale JavaScript, and source mtimes remain vulnerable to extraction and clock behavior.

### Q2 - What reproducibility guarantee should the artifact make?

**A. Same clean build environment - recommended.**
Require byte-identical bundles for direct-wheel and sdist paths built in the same clean CI job with the same Node, npm, lockfile, esbuild, OS, locale, and environment.
Record the exact tool versions in diagnostics and treat cross-platform byte identity as out of scope.

**B. Universal cross-platform reproducibility.**
Pin and normalize the full Node/npm/esbuild toolchain and every environment input across supported builders, then require identical output across operating systems.
This is substantially broader than proving that the release pipeline cannot drift between its two artifact paths.

### Q3 - How should bundled third-party license obligations be handled?

**A. Make the audit and required notices a ship gate - recommended.**
Inventory every third-party package actually included in the esbuild metafile, verify its redistribution terms, preserve required legal comments, and ship any required notice text in both sdist and wheel.
Fail CI when the bundled dependency set and notice inventory drift.

**B. Rely on the existing package license metadata.**
The repository has no existing bundled-JavaScript notice or SBOM mechanism, so this risks publishing third-party code without required attribution.

The fresh reviews also established these non-optional sequencing and proof constraints:

- Keep hook staging alive until Hatch finishes consuming `force_include`, then clean it in hook finalization and every failure path.
- Prove runtime module isolation from a temporary working directory with no ancestor `node_modules`, an empty `NODE_PATH`, and an esbuild metafile that allows only Node built-ins plus explicitly allowlisted optional `ws` native-addon probes as external imports.
- Exercise packaged Slack Socket Mode and Discord Gateway setup far enough to trigger their dependency loading, not only HTTP health and the generic WebSocket route.
- Use a recording pass-through npm shim for the before-fix `EACCES` reproduction and a hard-failure npm shim for the after-fix no-invocation assertion.
- Mirror the current Homebrew formula path, which installs the published PyPI sdist into a formula virtualenv, with the wheel-from-sdist no-Node test.
- Assert the named incomplete-artifact error reaches the CLI and manager logs with reinstall guidance before any registration retry can obscure it.

## Phases

### Phase 1 - Pin the production boundary and bundle contract

- [ ] Add `tests/integration/test_packaged_event_server.py` first and prove it fails on `main` because the real wheel lacks `bobi/event-server/dist/local.js` and installed startup invokes npm against the frozen package.
- [ ] Capture the pre-fix wheel contents, installed modes, npm invocation, `EACCES`, unavailable health endpoint, and unchanged absence of a runnable server.
- [ ] Change only `event-server/package.json`'s `build:local` command so `ws` and all required JavaScript are in the bundle, legal comments are preserved, and an esbuild metafile is emitted.
- [ ] Remove the duplicate Python esbuild command and `npm exec` fallback so the npm script is the single build definition.
- [ ] Audit the metafile and fail on every external import except Node built-ins and explicitly allowlisted optional `ws` native-addon probes.
- [ ] Prove the output starts under Node 20 from an isolated directory with no ancestor module tree and runtime module resolution unavailable.
- [ ] Exercise HTTP health plus a real WebSocket round trip.
- [ ] Exercise packaged Discord Gateway and Slack Socket Mode setup far enough to trigger lazy dependency loading and connection setup without contacting production services.

**Validation gate**

- [ ] `cd event-server && npm ci --no-audit --no-fund && npx tsc --noEmit && npx vitest run && npm run build:local`
- [ ] Launch the resulting `dist/local.js` from a temporary directory with no ancestor `node_modules`, set `NODE_PATH` to an empty directory, and assert `/health` returns `{"status":"ok","mode":"local"}`.
- [ ] Register a deployment, open a WebSocket subscription, post a representative GitHub issue webhook, and assert the normalized event is delivered.
- [ ] Assert the metafile has no unexpected external imports and both packaged socket-driver setup paths execute against controlled local fakes.
- [ ] Confirm the failing packaged integration test fails for the expected missing-artifact/runtime-npm reason before implementation changes make it green.

### Phase 2 - Build and publish the immutable artifact

- [ ] Add `hatch_build.py` with one isolated staging implementation for sdist and standard VCS wheel builds.
- [ ] Enable the custom build hook in `pyproject.toml` and include the hook plus generated artifact, input manifest, and any required notices in the correct sdist and wheel paths.
- [ ] Validate Node 20, run lockfile-exact npm installation in staging, run the one build script, require non-empty output, generate the sorted input manifest, audit the metafile and licenses, and force-include the runtime files through `build_data["force_include"]`.
- [ ] Keep staging alive until Hatch calls hook finalization, then clean it; also clean every earlier failure path.
- [ ] Verify the carried sdist artifact and manifest before wheel-from-sdist reuse without invoking npm.
- [ ] Rebuild a patched source archive when the supported Node toolchain is available or fail explicitly.
- [ ] Skip artifact work for editable wheels.
- [ ] Make VCS, source-archive, missing-artifact, and target-path decisions explicit and tested.
- [ ] Keep the checkout unchanged across every success and failure.
- [ ] Surface actionable command, exit, path, and bounded-output diagnostics for every build failure.

**Validation gate**

- [ ] `python -m build`
- [ ] `python -m build --wheel`
- [ ] Inspect both build paths and assert the sdist carries `hatch_build.py` plus both generated runtime files, and both wheels carry those files under `bobi/event-server/dist/`.
- [ ] Assert direct and sdist-derived wheels built in the same clean job contain byte-identical bundle bytes.
- [ ] Assert neither archive contains `node_modules`, `.npm-cache`, or staging paths.
- [ ] Assert wheel-from-sdist invokes no npm command and starts successfully without network or runtime modules.
- [ ] Modify one hashed source-archive input and assert stale bundle reuse is rejected.
- [ ] Assert required third-party notices are complete for the packages in the esbuild metafile and drift fails CI.
- [ ] Assert `git status --short` and ignored bundle/install paths are unchanged after source builds.

### Phase 3 - Separate installed startup from source development

- [ ] Preserve remote-server and healthy-server early returns.
- [ ] Classify the existing installed and repository candidates after directory discovery.
- [ ] Make installed startup require and execute the shipped non-empty bundle directly.
- [ ] Raise `PackagedEventServerArtifactError` with reinstall or upgrade guidance when the installed artifact is missing or empty.
- [ ] Prove the installed branch never calls npm, a builder, a timestamp freshness check, or a write-producing command.
- [ ] Hash source freshness inputs across both manifests, lockfile, TypeScript configuration, and all root/workspace TypeScript sources.
- [ ] Make the source dependency check require the workspace link, local esbuild binary, and a matching ignored dependency stamp.
- [ ] Use `npm ci --no-audit --no-fund` for missing, partial, or lock-drifted source dependencies, then the single `npm run build:local` command.
- [ ] Update the existing bind-address integration helper so no test helper preserves the removed installed-runtime npm behavior.
- [ ] Apply the real runtime guard in the packaged integration test and compare package file path, mode, size, and hash snapshots before and after startup.
- [ ] Prove `PackagedEventServerArtifactError` reaches direct CLI and manager logs with reinstall guidance before registration retries.

**Validation gate**

- [ ] `pytest tests/test_event_server_launch.py tests/test_packaging.py tests/test_runtime_guard.py --timeout=60 -q`
- [ ] `pytest tests/integration/test_packaged_event_server.py -m "not claude and not docker" --timeout=180 -v`
- [ ] `pytest tests/integration/test_event_server.py -m "not claude and not docker" --timeout=60 -v`
- [ ] Assert installed complete, installed missing, source fresh, source stale, source dependency-incomplete, npm failure, remote, and already-healthy branches all have named tests.
- [ ] Assert the frozen installed package snapshot is identical before and after successful health, registration, webhook, and WebSocket delivery.

### Phase 4 - Close CI, release, documentation, and proof gaps

- [ ] Run the packaged regression in a main CI job with Python, Node 20, and npm.
- [ ] Keep the production-shaped packaged-server harness as the single definition used by CI and the release smoke.
- [ ] Add Node 20 setup to the release `build-wheel` job before `python -m build`.
- [ ] Expand the exact-wheel release smoke from `bobi --version` to immutable installed startup with runtime npm made unusable.
- [ ] Require health success, no npm call, no runtime dependency/cache creation, no package-tree mutation, and reliable child-process cleanup on failure.
- [ ] Mirror the current Homebrew formula's PyPI-sdist-to-virtualenv path with the verified wheel-from-sdist no-Node test.
- [ ] Update `docs/EVENT_SERVER.md` to distinguish prebuilt installed startup from writable source-checkout rebuilds.
- [ ] Update `docs/SELF_HOSTED_EVENT_SERVER.md` to use `npm ci` for a cloned standalone server and state that Python wheels already contain the runnable bundle.
- [ ] Preserve Node 20, environment-variable, loopback, tunnel, TLS, Socket Mode, Discord Gateway, and restart guidance.
- [ ] Attach the required before-and-after transcript and archive-size result to the unified pull request.

**Validation gate**

- [ ] `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q`
- [ ] `pytest tests/integration/ -m "not claude and not docker" --timeout=180 -q`
- [ ] `cd event-server && npx tsc --noEmit && npx vitest run && npm run build:local`
- [ ] Run the exact release wheel smoke locally against the built artifact and preserve the Node log on any failure.
- [ ] Verify documentation describes installed and source behavior accurately and no feature commit changes `VERSION`, `pyproject.toml`'s project version, or `CHANGELOG.md`.

## Proof of work

This bug requires a failing production-shaped integration test first.
The test must cross the real distribution boundary instead of importing the source checkout in-process:

1. Build the real sdist and a wheel from that sdist from a clean source tree.
2. Build a real direct wheel from the same clean source tree.
3. Inspect archives and compare the bundle bytes.
4. Install the sdist-derived wheel into an isolated virtual environment or target directory.
5. Launch a subprocess outside the repository with `PYTHONPATH` removed so imports resolve only from the installed artifact.
6. Apply the real Bobi runtime write policy and assert no installed event-server path has write bits.
7. Put a recording pass-through npm shim ahead of the runtime path only after artifact construction for the before-fix run.
8. Start the installed local event server on an ephemeral port.
9. Before the fix, prove startup invokes npm and fails with `EACCES`.
10. Replace the shim with a hard-failure recorder for the after-fix run and prove it is never invoked and no install/cache path appears.
11. Assert `/health` returns `status: ok` and `mode: local`.
12. Register a test deployment, subscribe over a real WebSocket, post a representative GitHub issue webhook, and assert normalized delivery.
13. Compare installed file path, mode, size, and hash snapshots before and after startup.
14. Terminate the Node process in every terminal path and preserve its log on failure.

The pull request must show a concise before-and-after transcript from that same harness.

Before-fix evidence:

- The real wheel lacks `dist/local.js` and `node_modules`.
- The installed event-server directory is read-only.
- Startup invokes npm.
- npm fails with `EACCES` while creating `node_modules`.
- The health endpoint never becomes available.

After-fix evidence:

- The real sdist and both wheel build paths contain the generated bundle and no npm install state.
- The installed event-server directory remains read-only.
- Startup invokes no npm command.
- `/health` returns the expected local-server payload.
- A representative webhook reaches a real WebSocket subscriber.
- The installed package snapshot is identical before and after startup.
- The bundle starts with no runtime modules available and preserves Slack Socket Mode and Discord Gateway imports.
- Focused, protocol, TypeScript, event-server, Python unit, and non-Claude integration suites pass.
- Archive sizes are reported.

Build-hook unit coverage belongs in `tests/test_packaging.py` and must cover:

- Fresh sdist generation.
- Fresh direct VCS wheel generation.
- Verified sdist artifact and input-manifest reuse without npm.
- Patched source-archive rejection or supported rebuild.
- Incomplete source archive behavior.
- Editable-wheel skip behavior.
- Correct sdist and wheel force-include destinations for the bundle, manifest, and required notices.
- Missing Node and wrong Node major version.
- Failed `npm ci`.
- Failed bundle command.
- Missing and empty outputs.
- Staging lifetime through target construction plus cleanup in finalization and every earlier failure.
- Unexpected metafile external imports and bundled-license notice drift.
- Checkout immutability.

Launcher unit coverage belongs in `tests/test_event_server_launch.py` and must cover:

- Healthy existing server before artifact checks.
- Remote server before Node or npm checks.
- Complete installed layout with direct bundle spawn.
- Missing and empty installed bundle with the named error and no npm.
- Fresh source bundle with a matching input manifest and no npm.
- Stale or missing source bundle with a matching dependency stamp using only the build script.
- Missing, incomplete, or lock-drifted source dependencies using `npm ci` followed by the build script.
- npm failure output and exit code.
- Manifest, lockfile, config, root source, workspace source, Slack driver, and Discord driver freshness.

The existing local protocol suite must remain green for health, registration, generic events, Slack, GitHub, Linear, WhatsApp, Discord, Socket Mode, and WebSocket replay behavior.
A real Claude session is not required because the defect is in packaging and process lifecycle, not brain behavior.
No screenshot or frontend capture is required because there is no UI surface.

## Lane map

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| A | #798 | 1-4 | Immutable packaged local event-server artifact, launcher split, and release proof | solo | awaiting Gate 1 |

**Lanes:** one lane is the null topology and the default.
All phases converge on the same package artifact, launcher, and production-shaped regression harness, so parallel PRs would add coordination without an independent landing seam.
Issue #798 remains both the task source and the one-lane dispatch record.
The same unified draft PR carries this reviewed spec and, only after Gate 1 approval, its implementation.

## Amendments

- **2026-07-23** (issue-lifecycle run `adhoc-c500673e`): converted the reviewed issue-body design into the checked-in plan source of truth, reverified all current-code claims at `50ff18c`, and added the v0.48.0 Slack Socket Mode and Discord Gateway bundle inputs to the artifact and verification contract.
- **2026-07-23** (fresh review): added Gate 1 Questionables for content-addressed provenance, environment-scoped reproducibility, and bundled-license notices; tightened Hatch finalization, module isolation, socket-driver, Homebrew, and npm-shim proof.

## Notes

### What already exists

The implementation reuses Hatchling, the existing npm lockfile and build script, ordered installed/source directory discovery, runtime write policy, process health and logging, protocol integration harnesses, Node 20 CI setup, and the exact-wheel release pipeline.
It removes the duplicate Python esbuild path instead of adding another builder.
The only new production mechanism is the build hook and its generated artifact manifest.

### Dream-state delta

After this issue, every standard Bobi distribution is self-contained for local event-server startup and the release pipeline proves the installed immutable artifact end to end.
The remaining 12-month gap is broader supply-chain reproducibility across operating systems and independent builders.
That universal guarantee is deliberately outside Q2A and can be pursued separately if product distribution requires it.

### Review diagrams

System architecture:

```text
VCS checkout
  -> Hatch hook staging
     -> npm ci from committed lockfile
     -> one npm build:local definition
     -> local.js + local.inputs.json + audit evidence
  -> sdist
     -> verified no-Node wheel build
  -> direct wheel

installed wheel
  -> runtime write guard
  -> installed/source classifier
  -> verified packaged local.js
  -> Node 20 process
     -> HTTP and generic WebSocket protocol
     -> Slack Socket Mode driver
     -> Discord Gateway driver

source or editable checkout
  -> input and dependency digests
     -> fresh: existing local.js
     -> stale source only: one build script
     -> stale dependency state: npm ci, then one build script
```

Build data and shadow paths:

```text
declared inputs
  -> all present?
     -> no: fail distribution build with the missing path
     -> yes: hash sorted relative paths
        -> dependency digest matches source stamp?
           -> no: npm ci or explicit build failure
           -> yes: reuse installed build dependencies
        -> bundle digest matches carried manifest?
           -> yes: reuse only for an unmodified source archive
           -> no: rebuild with supported tools or fail
        -> build output non-empty?
           -> no: fail distribution build
           -> yes: audit externals and licenses
              -> audit fails: fail distribution build
              -> audit passes: force-include until target finalization
```

Artifact state machine:

```text
ABSENT
  -> GENERATED
     -> HASHED
        -> AUDITED
           -> STAGED
              -> ARCHIVED
                 -> VERIFIED
                    -> EXECUTED

Any missing input, digest mismatch, empty output, unexpected external,
notice drift, or early staging deletion moves to FAILED.
FAILED cannot transition to ARCHIVED.
An installed artifact never transitions back to GENERATED.
```

Runtime error flow:

```text
remote configured -> skip local startup
healthy local     -> connect
installed layout  -> bundle valid? -> spawn -> health ready? -> started
                         | no                    | no
                         v                       v
              PackagedEventServerArtifactError  preserve Node log and fail

source layout -> inputs fresh? -> spawn
                   | no
                   v
             deps exact? -> build -> validate -> spawn
                 | no         | no
                 v            v
               npm ci       specific build error
```

Deployment sequence:

```text
merge approved implementation
  -> release job installs Node 20
  -> build sdist and direct wheel once
  -> verify artifact bytes, manifests, notices, and immutable startup
  -> publish exact sdist and wheel
  -> Homebrew formula consumes published sdist
  -> formula wheel build verifies and reuses carried artifact without Node
  -> post-release uv tool and Homebrew health checks
```

Rollback:

```text
release regression?
  -> no: continue normal rollout
  -> yes: stop publication if still gated
          otherwise revert implementation
          -> build and publish replacement release
          -> affected operator upgrades or reinstalls

No database, protocol, or persisted-state rollback is required.
```

### Test coverage map

```text
CODE PATHS                                      PRODUCTION FLOWS
[PLANNED] Hatch direct VCS wheel                [PLANNED] uv tool exact-wheel startup
  -> fresh staged build                           -> guard freezes installed package
  -> manifest and audit                           -> npm hard-failure shim is untouched
  -> forced wheel paths                           -> health + register + webhook + WS

[PLANNED] Hatch sdist                            [PLANNED] Homebrew-shaped install
  -> fresh staged build                           -> published sdist
  -> carried artifact + manifest                  -> no-Node wheel build
                                                  -> immutable installed startup
[PLANNED] Wheel from sdist
  -> matching hashes: reuse, no npm              [PLANNED] Packaged socket drivers
  -> patched input: rebuild or explicit fail      -> Slack setup reaches local fake
  -> missing/empty artifact: explicit fail        -> Discord setup reaches local fake

[PLANNED] Source or editable startup             [PLANNED] Operator-visible failures
  -> fresh digest: direct spawn                   -> incomplete wheel guidance
  -> source-only drift: build script              -> npm/build diagnostics
  -> lock drift: npm ci + build                   -> Node log on startup failure
  -> remote/healthy: early return

Coverage target: 100% of planned branches.
No UI or LLM evaluation path applies.
```

### Failure modes registry

| Codepath | Failure mode | Rescued? | Test? | Operator sees | Logged? |
|---|---|---:|---:|---|---:|
| Hook input scan | Missing or unreadable input | Yes, abort build | Yes | Exact path and operation | Yes |
| Hook dependency install | npm, registry, lockfile, or disk failure | Yes, abort build | Yes | Command, exit, bounded output | Yes |
| Hook bundle build | esbuild or workspace resolution failure | Yes, abort build | Yes | Command, exit, bounded output | Yes |
| Hook artifact audit | Empty output, stale digest, unexpected external, or notice drift | Yes, abort build | Yes | Specific failed invariant | Yes |
| Hatch target construction | Forced include disappears early | Yes, staging retained through finalization | Yes | Build failure with path context | Yes |
| Installed startup | Bundle or manifest missing or empty | Yes, named permanent error | Yes | Reinstall or upgrade guidance | Yes |
| Installed Node process | Node missing, process exits, or health timeout | Yes, existing startup failure | Yes | Event-server log path | Yes |
| Source dependency state | Missing, partial, or lock-drifted modules | Yes, exact `npm ci` | Yes | Specific npm failure if repair fails | Yes |
| Source bundle state | Missing or stale bundle | Yes, one build script | Yes | Specific build failure | Yes |
| Remote or healthy server | Local artifact unavailable | Yes, early return | Yes | Nothing, expected path | Yes |

No row has `Rescued? = No`, `Test? = No`, and silent operator impact.

### Error and rescue registry

| Codepath | Failure | Handling | Operator impact |
|---|---|---|---|
| Hatch staging | Temporary directory or input copy fails | Abort with source and destination context; clean staging | Distribution is not published |
| Build-time Node check | Node missing or not major version 20 | Abort with detected version or missing-command diagnostic | CI or release build fails before publication |
| Build-time `npm ci` | npm missing, registry unavailable, lock mismatch, or disk failure | Abort with command, exit code, and bounded output | CI or release build fails before publication |
| Build-time bundle | TypeScript resolution, workspace resolution, or esbuild fails | Abort with command, exit code, and bounded output | CI or release build fails before publication |
| Artifact validation | `dist/local.js` missing or empty | Abort with explicit artifact diagnostic | Incomplete distribution cannot be produced |
| Input-manifest validation | A declared input is missing or its digest differs | Rebuild with the supported toolchain or fail explicitly | Stale executable code cannot be published |
| Wheel from sdist | Carried artifact or manifest is missing, empty, or mismatched | Build or fail explicitly; never silently omit or trust it | Distribution is not published |
| Bundle audit | Metafile contains an unexpected external or notice inventory drifts | Abort with dependency and license context | Distribution is not published |
| Installed startup | Shipped bundle missing or empty | Raise `PackagedEventServerArtifactError` with reinstall or upgrade guidance | Server stays down without package mutation |
| Source startup | Locked dependencies missing or partial | Run `npm ci` and surface any specific failure | Developer repairs environment and retries |
| Source startup | Bundle build fails | Surface the specific build failure | Developer fixes source or toolchain and retries |
| Node process startup | Node missing, bundle exits, or health never succeeds | Preserve existing process log and startup timeout behavior | CLI and manager logs identify startup failure |
| Remote event server | Non-loopback URL configured | Preserve early `skipped` result | No local Node or npm requirement |

No planned failure is silent and untested.

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| The sdist carries a stale generated artifact | Always generate a fresh artifact when producing an sdist from VCS, then prove wheel-from-sdist startup |
| A source archive is patched after bundle generation | Verify every declared input against `local.inputs.json`; rebuild or fail on any mismatch |
| Wheel-from-sdist accidentally invokes npm | Put an npm sentinel in the packaging test and fail on any invocation |
| Direct and sdist-derived bundles drift | Compare exact bundle bytes inside the same clean build environment and record the complete toolchain |
| `ws` bundling breaks optional native-module probing | Start in an isolated module-free directory, audit external imports, and run a real WebSocket delivery using the supported pure-JavaScript path |
| New socket-driver imports are omitted from staging or freshness | Stage all root/workspace sources and explicitly test Slack and Discord driver changes |
| A lazy socket-driver dependency is omitted | Execute packaged Slack and Discord setup against local fakes, not only generic server health |
| The build hook mutates the checkout | Stage outside the tree and assert repository status plus ignored install/build paths stay unchanged |
| Hatch reads a forced include after staging was deleted | Retain staging on the hook instance through target construction and clean it in finalization |
| npm or registry failure blocks Python packaging | Fail at build time with specific diagnostics; use CI cache only as an optimization |
| Wheel size grows unexpectedly | Ship one compressed bundle, reject `node_modules`, and report archive size in proof |
| Bundled code lacks required attribution | Audit the esbuild metafile against license and notice inputs and fail CI on drift |
| Installed artifact is missing or corrupt | Fail without mutation and direct the operator to reinstall; wheel `RECORD` and guard checks remain authoritative |
| Source developers lose lazy rebuilds | Retain source/editable detection, complete freshness inputs, `npm ci`, and the same build script |
| Release workflow still proves only imports | Reuse the packaged integration harness for the exact wheel smoke |
| Child process leaks on a failed smoke | Require cleanup in `finally` and preserve the process log |

### Acceptance criteria

- A clean sdist-to-wheel build and a clean direct-wheel build both succeed.
- The sdist and both wheels contain the self-contained local server bundle at their required paths.
- The sdist and both wheels contain the matching input manifest and any required third-party notices.
- Direct and sdist-derived bundle bytes match when built in the same clean environment.
- A patched source archive cannot silently reuse a stale bundle.
- No archive contains `node_modules`, npm cache, or staging state.
- A fresh exact-wheel install starts the local event server after the real runtime guard makes the package read-only.
- Installed startup performs zero npm commands and zero writes under the installed Bobi package.
- Health, deployment registration, webhook ingestion, and WebSocket delivery work from the installed artifact.
- The bundled output starts from a directory with no reachable runtime module tree, has no unexpected external imports, and retains current Slack Socket Mode and Discord Gateway behavior.
- Missing or empty installed-bundle behavior is actionable and never attempts self-repair in `site-packages`.
- Editable and source checkouts use content digests, rebuild stale local bundles through the one npm build script, and run lockfile-exact `npm ci` when dependency state is not proven current.
- Remote event-server configuration still skips local Node and npm work.
- Existing provider protocol, socket transport, and replay tests pass.
- CI and release prove the exact artifact they build and upload.
- The wheel-from-sdist test mirrors the current Homebrew formula installation boundary without requiring Node during formula installation.
- The runtime guard remains unchanged and effective.
- Documentation accurately distinguishes installed, source, and cloned standalone startup.
- The pull request includes concrete failing and passing proof.
- No frontend, public API, event payload, version, or changelog change is included.

### Review history

The issue-body spec was reviewed before this conversion.
CEO scope review held scope around the immutable artifact fix.
Engineering review required explicit VCS-wheel, sdist, wheel-from-sdist, editable, installed, source, remote, and healthy-server paths.
It also required direct-wheel proof and removal of the existing integration helper's dependency on launcher helpers that will be removed.
Design review was not applicable because there is no UI.
An independent Codex review found no blocker after the issue spec made archive reuse, source detection, runtime module isolation, real guard use, direct-wheel coverage, and Node 20 diagnostics explicit.

The fresh CEO review held scope around the one immutable artifact and one production-shaped harness.
The fresh engineering review covered architecture, errors, security, data flow, code quality, tests, performance, observability, deployment, long-term trajectory, failure modes, and sequential delivery.
It found no critical gap after the recommended Questionable resolutions and non-optional proof constraints were captured.
The fresh design-applicability review exited as not applicable because the change has no UI.
The fresh independent Codex pass raised artifact provenance, toolchain reproducibility, module isolation, lazy socket-driver coverage, Homebrew-path, licensing, hook-lifecycle, error-propagation, and npm-shim concerns.
Repository verification rejected its `RECORD` and swallowed-launcher-error claims, confirmed the Homebrew sdist boundary, and captured the remaining findings in Q1-Q3 plus the proof constraints above.

Gate 1 remains human approval by Zach.
No implementation may start before that approval.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|---|---|---|---:|---|---|
| CEO Review | `/gstack-plan-ceo-review` | Scope and strategy | 1 | CLEAR | HOLD_SCOPE, one coherent lane, 0 critical gaps |
| Codex Review | `codex exec` | Independent second opinion | 1 | NEEDS GATE 1 | 14 concerns triaged into Q1-Q3, proof constraints, or evidence-backed rejections |
| Eng Review | `/gstack-plan-eng-review` | Architecture and tests | 1 | NEEDS GATE 1 | 3 unresolved decisions, 0 critical gaps, full branch and failure coverage planned |
| Design Review | `/gstack-plan-design-review` | UI and UX gaps | 1 | NOT APPLICABLE | No UI, interaction, frontend, or design-system scope |
| DX Review | `/gstack-plan-devex-review` | Developer experience gaps | 0 | NOT APPLICABLE | No separate review required for this production packaging bug |

**CODEX:** The outside voice tightened artifact provenance, Hatch staging lifetime, runtime-module isolation, packaged socket-driver proof, Homebrew parity, license handling, and the before/after npm recorder.

**CROSS-MODEL:** Both reviews support the self-contained bundle and immutable installed-runtime boundary.
The only remaining tension is how strongly to attest inputs, define reproducibility, and package notices.

**VERDICT:** Scope and design applicability are cleared.
Engineering approval awaits Zach's Gate 1 resolution of Q1-Q3.
Implementation is blocked.

**UNRESOLVED DECISIONS:**

- Q1: content-addressed input manifest and digest-based freshness, recommended A.
- Q2: byte identity within one clean build environment, recommended A.
- Q3: bundled-license audit and required notices as a ship gate, recommended A.
