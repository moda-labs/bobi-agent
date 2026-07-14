# Issue #751: Guard Bobi's Runtime Installs From Agent Writes

## Problem

Bobi runs autonomous agents with broad local write authority. Claude sessions use
`permission_mode="bypassPermissions"` in `bobi/brain/claude.py`, and Codex
sessions pass `--dangerously-bypass-approvals-and-sandbox` in
`bobi/brain/codex.py`. That is intentional for unattended task work, but it
currently means an agent can also edit Bobi-owned runtime code and installed
package images.

The concrete failure from #751 was an agent investigating #750 and modifying
`site-packages/bobi/subagent.py` in its own installed Bobi package. The edit was
technically the right code change, which makes the failure mode worse:

- It bypassed review, tests, PRs, and source control.
- It would disappear on the next `uv tool install/upgrade bobi`.
- It created a phantom fix where the operator could believe the framework had
  been repaired while the source repo remained unchanged.

Bobi already detects drift in installed team images under `run/package/`, but it
does not make those images non-writable and does not cover the Bobi framework
wheel, its `.dist-info` metadata, Python virtualenvs, or other package-managed
runtime directories.

## Goals

- Make Bobi-owned runtime package images and framework installs read-only by
  default, while preserving agent read and execute access.
- Apply the same protection before any brain runs, including Claude, Codex,
  gateway-backed Claude, and future brain adapters.
- Keep normal task work writable: assigned repos, `run/workspace/`, `run/state/`,
  logs, handoffs, and other runtime state remain available for agents to edit.
- Make accidental writes fail at the filesystem boundary where possible instead
  of by parsing individual model/tool commands.
- Add doctor checks that fail loudly when protected runtime package files are
  writable or when the installed Bobi wheel has drifted from package `RECORD`
  hashes.

## Non-Goals

- Blocking legitimate source-repo edits to Bobi when the assigned task checkout
  is `moda-labs/bobi-agent`.
- Making `run/workspace/` or `run/state/` immutable; those are intentionally
  user/agent writable.
- Building a complete host sandbox in this ticket. Same-UID POSIX permissions
  are a practical guardrail, not a hard security boundary against a process that
  can call `chmod` on files it owns. Stronger isolation backends are designed
  into the framework boundary below.
- Detecting arbitrary dependency drift outside Bobi's own distribution files and
  Bobi-managed installed package images.

## Root Cause

Bobi has a provider-agnostic `BrainFactory` / `BrainSession` boundary, but it
does not have a provider-agnostic runtime filesystem policy. Each brain adapter
is allowed to run with broad local authority, and there is no framework step that
prepares protected roots before handing control to the brain.

The existing install integrity check records hashes for the installed team image
in `run/package/`, but it only reports drift later. It does not set permissions
after install, does not verify permissions in doctor, and does not cover Bobi's
own installed Python distribution.

## Proposed Solution

### 1. Add A Runtime Write-Policy Module

Add a framework module, tentatively `bobi/runtime_guard.py`, that owns the
provider-independent filesystem policy. It should expose a small API:

- `protected_runtime_roots(runtime_root: Path | None) -> list[ProtectedRoot]`
- `apply_runtime_write_policy(runtime_root: Path | None) -> GuardReport`
- `check_runtime_write_policy(runtime_root: Path | None) -> CheckResult`
- `with_mutable_runtime_package(runtime_root: Path) -> ContextManager[None]`

`ProtectedRoot` should include:

- `path: Path`
- `kind: Literal["team-package", "bobi-package", "bobi-dist-info", "venv", "dependency"]`
- `mode: Literal["readonly"]`
- `reason: str`

The first implementation should protect:

- The installed team package image at `$BOBI_HOME/agents/<name>/run/package/`.
- Bobi's imported package directory, resolved from `Path(bobi.__file__).parent`,
  when it is not the active assigned source checkout.
- Bobi's installed `.dist-info` directory, discovered from
  `importlib.metadata.distribution("bobi")`.
- The nearest concrete `site-packages` or `dist-packages` package metadata roots
  that contain the installed Bobi distribution, scoped to Bobi files only where
  possible.
- Common package-manager directories only when they are Bobi-created runtime
  dependency state with a positive ownership marker. Do not protect dependency
  directories merely because they exist under the assigned task checkout; agents
  may legitimately install, update, and test task-repo dependencies.

Do not protect arbitrary ancestors such as `/usr`, `/usr/local`, `/`, or the
user's full home directory. A target is protected only when Bobi can identify a
concrete runtime/package-managed root.

### 2. Enforce With Filesystem Permissions First

After installing or composing an agent package, Bobi should make
`run/package/` read-only:

- Directories keep execute/search bits and lose write bits.
- Files keep their existing read/execute bits and lose write bits.
- Symlinks are not followed when changing permissions; their resolved targets
  are checked during doctor so a package cannot smuggle writes outside the image.
- The install manifest, compose lock, and generated `.gitignore` are also
  Bobi-owned package metadata and should be read-only after install.

Agents must still be able to:

- Read prompts, roles, workflows, monitors, context, and tool definitions from
  `run/package/`.
- Execute scripts shipped by the package when the executable bit is present.

Agents must not be able to directly edit files inside `run/package/`. Reinstall
is the mutation path: `install_pack()` enters `with_mutable_runtime_package()`,
regenerates the image, writes manifests, then reapplies read-only permissions.

For Bobi's own framework install, apply the same read-only mode to Bobi's package
directory and `.dist-info` metadata when Bobi can safely identify that it is
running from a wheel/tool install. Skip this step for editable/source installs so
development checkouts and legitimate `moda-labs/bobi-agent` tasks remain
writable through the normal PR flow.

### 3. Add Pluggable Enforcement Backends

Filesystem permissions should be the default local backend because they are
simple, brain-agnostic, and preserve read/execute access. The module should make
the backend explicit so stronger deployments can use a stronger mechanism
without changing brain adapters:

- `chmod` backend: remove write bits from protected roots. This is the portable
  default and catches accidental direct writes from Claude, Codex, shell tools,
  MCP tools, and future adapters. It is not a security boundary when the agent
  process owns the files, because the same UID can deliberately restore write
  bits with `chmod`.
- `readonly-mount` backend: for containerized deployments, mount protected roots
  read-only into the agent process namespace.
- `owner-split` backend: for managed hosts that can run agents as a worker user,
  keep protected roots owned by the controller/install user and run brain
  subprocesses without permission to `chmod` or rewrite them.

The first implementation only needs to ship the `chmod` backend plus the
abstraction and doctor visibility. It must document that same-UID processes can
potentially undo chmod, so operators that need a hard boundary should configure
the stronger backend once available.

Every backend must preserve read access, directory search access, and existing
execute bits for the agent execution identity. A stronger backend is invalid if
it prevents agents from reading package prompts/workflows/context or executing
package-provided scripts that were executable before protection was applied.

### 4. Apply The Policy At Framework Boundaries

Apply and verify the runtime write policy in framework-owned paths, not inside a
specific brain adapter:

- `bobi.install.install_pack()` should make the package mutable for the duration
  of install and read-only at the end, using `finally` semantics so a failed
  install does not leave a previously protected image writable.
- Add a shared helper, tentatively `bobi.runtime_guard.prepare_brain_runtime()`,
  and call it from every framework-owned brain invocation path before
  constructing or invoking a `BrainSession`.
- The first implementation should wire that helper into the concrete current
  call sites: `Session._make_brain_session()` in `bobi/session.py`,
  `_run_agent_supervised()` / `_build_client()` in `bobi/subagent.py`,
  workflow agent creation in `bobi/workflow/orchestrator.py`, setup one-shots in
  `bobi/setup/llm.py`, and MCP probe sessions in `bobi/validate.py`.
- `bobi agent <name> doctor` should call `check_runtime_write_policy()` and
  report protected roots that are still writable.

This handles Claude, Codex, gateway-backed Claude, and future model adapters
because they all run after the same Bobi runtime preparation. No Claude
`PreToolUse` hook or Codex-specific command filter is required for the primary
guard.

### 5. Preserve Writable Development Checkouts

The guard must distinguish runtime installs from assigned source repos:

- If Bobi is imported from a source checkout that is equal to or inside the
  current assigned task repo, do not mark that checkout read-only.
- If the assigned task repo is `moda-labs/bobi-agent`, edits under that checkout
  are allowed; that is the correct path for framework changes.
- Detect editable/source installs using distribution metadata where available
  (`direct_url.json` editable markers, missing wheel `RECORD`, and path
  comparison), not only by inspecting `bobi.__file__`.
- If the active runtime uses an installed wheel or tool-managed virtualenv,
  protect the installed package copy even if the agent's assigned repo happens
  to contain a different Bobi checkout.

This keeps the desired workflow intact: agents can read installed runtime files
for diagnostics, but framework changes must be made in the source repo and sent
through PR review.

### 6. Doctor Integrity Check For Bobi Distribution

Add `_check_bobi_install_integrity()` to `bobi/doctor.py` and include it in
`run_doctor()`.

The check should use `importlib.metadata.distribution("bobi")` and the wheel's
`RECORD` metadata when available:

- For each file with a `sha256=` hash in `dist.files`, compare the current bytes
  to the recorded hash.
- Resolve `PackagePath` entries through the metadata API and reject/report any
  resolved path that escapes the expected distribution package or `.dist-info`
  roots.
- Skip files without hashes, missing metadata, editable source installs, and
  local source checkouts where no wheel `RECORD` exists.
- Skip non-`sha256` hashes and report unreadable hashed files as failures.
- Fail when hashed files are missing or differ.
- The hint should instruct the operator to reinstall or upgrade Bobi and move
  any desired framework changes into a source PR.

Implement the metadata lookup behind a small helper so tests can inject a
fixture distribution without depending on the test runner's own installation
layout.

This is detection only. It complements the filesystem guard and catches:

- Drift that happened before the guard existed.
- Out-of-band edits made outside Bobi-controlled launch paths.
- Same-UID or privileged edits that intentionally bypass read-only permissions.

## Testing Plan

Unit tests:

- `install_pack()` writes the package image and leaves files/directories
  non-writable while preserving read and execute bits.
- Reinstall temporarily restores mutability, regenerates the package image, and
  reapplies read-only permissions.
- `run/package/` scripts with executable bits remain executable after the guard.
- `run/workspace/` and `run/state/` remain writable.
- `protected_runtime_roots()` includes the team package image for a bound runtime.
- Bobi wheel installs include the imported package and `.dist-info` roots.
- Editable/source Bobi checkouts used as the assigned task repo are skipped.
- Symlinks inside protected package images are not chmod-followed and are
  reported by doctor as package-integrity failures when they resolve outside the
  protected image, even if the resolved target is not currently writable.
- `check_runtime_write_policy()` fails with a clear detail for writable protected
  files or directories.
- The guard preparation path is invoked before Claude session creation.
- The guard preparation path is invoked before Codex subprocess execution.
- A fake future brain enters through the public session/launch path and observes
  that `prepare_brain_runtime()` ran without adding provider-specific write
  filters.
- Same-owner `chmod` bypass is documented by test: direct writes fail after the
  `chmod` backend, but a same-UID process can deliberately restore write bits.
  The test should assert this limitation is reported in docs/doctor expectations,
  not treat `chmod` as a hard sandbox.
- Doctor passes for editable/source installs without `RECORD`.
- Doctor fails with a clear detail when a hashed Bobi wheel file differs from
  its `RECORD` digest.
- Doctor handles missing distributions, missing `dist.files`, unreadable hashed
  files, missing hashed files, mismatched hashes, and non-`sha256` hashes.

Integration or smoke tests:

- Install a fixture team, verify `run/package/` is readable and executable but a
  direct file write fails without first making it mutable through Bobi install.
- Run a Claude-backed smoke, gated on the CLI, that reads and executes a script
  from `run/package/` and fails to edit the same package image.
- Run a Codex-backed smoke when the CLI is available with the same read/execute
  and write-denial expectations.
- `bobi agent <name> doctor` reports a writable protected package file and
  reports Bobi install drift when fed a fixture distribution with a mismatched
  file.

## Rollout

1. Ship the runtime write-policy module with the `chmod` backend enabled by
   default.
2. Update install and launch paths to apply the policy for all brains.
3. Ship doctor checks for writable protected roots and Bobi wheel drift.
4. Document the boundary in `docs/SECURITY.md`: agents may read and execute
   runtime package files, and may edit assigned repos/workspaces/state, but
   Bobi-owned installed framework and package images are protected.
5. Add deployment notes for stronger `readonly-mount` and `owner-split`
   backends as follow-up work for managed hosts that need a hard security
   boundary.

## Risks And Mitigations

- **Same-UID chmod bypass:** document the limitation clearly, detect drift in
  doctor, and keep the backend abstraction ready for read-only mounts or
  owner-split execution where a hard boundary is required.
- **False positives in source checkouts:** skip editable Bobi source checkouts
  that are the assigned task repo, and avoid protecting arbitrary ancestors.
- **Broken package scripts:** preserve execute bits and add tests that execute a
  package-provided script after permissions are applied.
- **Install failures after read-only mode:** centralize mutable install windows
  in `with_mutable_runtime_package()` so reinstall, compose, and manifest writes
  do not hand-roll permission changes.
- **Brain-specific gaps:** keep enforcement at install/launch boundaries so
  Claude, Codex, MCP tools, shell commands, and future brains all encounter the
  same protected filesystem state.
