# Issue #751: Guard Bobi's Own Install From Agent Writes

## Problem

Bobi launches Claude-backed agents with `permission_mode="bypassPermissions"` in
`bobi/brain/claude.py`, so Claude Code does not prompt before tool calls that
write files. That is intentional for unattended agents, but it currently also
lets an agent edit Bobi's own runtime framework install.

The concrete failure from #751 was an agent investigating #750 and modifying
`site-packages/bobi/subagent.py` in its own installed Bobi package. The edit was
technically the right code change, which makes the failure mode worse:

- It bypassed review, tests, PRs, and source control.
- It would disappear on the next `uv tool install/upgrade bobi`.
- It created a phantom fix where the operator could believe the framework had
  been repaired while the source repo remained unchanged.

Bobi already detects drift in installed team images under `run/package/`, but
that does not cover the framework wheel, Python venvs, or `site-packages`.

## Goals

- Deny known Claude write tools before they mutate Bobi framework installs,
  Python virtualenvs, and selected package-manager managed dependency
  directories.
- Preserve unattended operation for normal task work inside the assigned repo,
  workspace, and runtime state.
- Make blocked writes visible to the agent with instructions to report the
  attempted framework patch to the operator.
- Add a doctor integrity check that fails loudly when the installed Bobi wheel
  has drifted from its package `RECORD` hashes.
- Keep the protection framework-owned and enabled by default for Claude agents.

## Non-Goals

- Replacing `bypassPermissions` for all agents.
- Building a complete OS sandbox or claiming this hook is a filesystem write
  barrier. It is framework-owned defense in depth; filesystem permissions or
  read-only mounts remain the stronger boundary where the runtime can provide
  them.
- Blocking legitimate source-repo edits to Bobi when the user has checked out
  `moda-labs/bobi-agent` as the assigned task repository.
- Enforcing the guard for non-Claude brains that do not support Claude hooks.
- Detecting arbitrary dependency drift outside Bobi's own distribution files in
  the doctor check.

## Root Cause

Claude permission prompts are bypassed globally, but Bobi does not currently add
a framework-level write policy. The only default `PreToolUse` hook is
`_make_defer_hook()` in `bobi/subagent.py`, and it is only installed for
`AskUserQuestion` deferral on the blocking subagent path. Session-backed agents
and one-shot Claude calls receive no default guard hook.

The framework also has install integrity detection for team packs via
`bobi/install.py` and `bobi/doctor.py`, but not for the Bobi Python distribution
installed in `site-packages`.

## Proposed Solution

### 1. Centralize Claude Hook Composition

Add a small hook helper module, tentatively `bobi/brain/claude_hooks.py`, that
exports:

- `make_default_pre_tool_use_hooks(cwd: Path | None, existing: dict | None)`
- `is_protected_agent_write(tool_name: str, tool_input: dict, cwd: Path | None)`
- `protected_roots(cwd: Path | None) -> list[Path]`

`ClaudeBrain.make_session()` and `ClaudeBrain.stream_once()` should merge these
default hooks with caller-provided hooks before constructing
`ClaudeAgentOptions`. Existing call sites should not need to remember to opt in.

If a caller already provides `PreToolUse` matchers, preserve them and ensure the
default guard cannot be bypassed by an earlier hook. Prefer prepending the guard
matcher unless the SDK guarantees that any `deny` decision wins after all
matching hooks run. Add a test that covers a caller-provided hook for the same
tool returning `allow`.

The guard should run for known Claude built-in write-capable tools only:

- `Write`
- `Edit`
- `MultiEdit`
- `NotebookEdit`
- `Bash`

The existing `AskUserQuestion` defer hook remains separate and composes through
the same hook dictionary.

MCP tools, custom tools, future Claude write tools, and shell code that does not
surface a protected path in the command text are explicitly out of scope for the
preventive hook. They are covered only by the doctor drift check and by any
runtime filesystem isolation available outside Bobi.

### 2. Protected Path Policy

The guard should deny writes that target package-managed or framework-managed
paths:

- Bobi's imported package directory, resolved from `Path(bobi.__file__).parent`.
  Skip this root when Bobi is installed editable from the current assigned
  source checkout, so a `moda-labs/bobi-agent` task can still edit source files
  through the normal PR flow.
- The installed Bobi distribution root when derivable from
  `importlib.metadata.distribution("bobi").locate_file("")`.
- The nearest concrete `site-packages` or `dist-packages` directory containing
  Bobi's installed package. Do not add arbitrary ancestors such as `/usr`,
  `/usr/local`, or `/`.
- The active Python virtualenv root when `sys.prefix != sys.base_prefix`.
- Common dependency directories under the current working tree:
  `.venv/`, `venv/`, `node_modules/`, `.tox/`, `.nox/`, and `__pycache__/`.
  This list is intentionally narrow and starts with dependency directories where
  generated or package-managed content should not be hand-patched by agents.

The policy should normalize and resolve candidate paths before comparison. A
target is protected when it is equal to or inside one of the protected roots.
Missing target paths should still be resolved against the closest existing
parent so new-file writes into protected directories are blocked.

This is not race-free. A symlink swap or directory replacement between hook
approval and tool execution can bypass a hook-level policy. The implementation
should still resolve symlinked candidates and test symlink paths into protected
roots, while documenting that OS-level read-only mounts are needed for a hard
filesystem boundary.

### 3. Tool Input Extraction

For file-native tools, extract the path fields directly:

- `Write.file_path`
- `Edit.file_path`
- `MultiEdit.file_path`
- `NotebookEdit.notebook_path`

For `Bash`, use a conservative detector rather than a broad command ban. The
first implementation should block when the command string contains an explicit
protected path or a write redirection / mutation command whose destination
normalizes into a protected root. It should cover the known incident class and
common variants:

- `cat > /path`, `echo ... > /path`, `tee /path`
- `python -c ... /path`, `python script.py /path` when the protected path is
  explicit in the command
- `sed -i`, `perl -pi`, `mv`, `cp`, `rm`, `touch`, `mkdir`, `chmod`, `chown`
  with explicit protected-path operands
- `cd /protected && ... > file` when the command establishes a protected working
  directory before a write-like operation

The guard should not try to parse every shell construct. If a command explicitly
mentions a protected path and uses a write-like operation, deny it. Commands that
only read protected files for diagnostics can remain allowed unless the command
also matches a mutating form. Known non-goals for the first implementation
include environment-variable expansion, glob-only targets, here-doc scripts with
hidden destinations, and external scripts whose contents are not visible in the
tool input.

### 4. Denial Response

When denying, return a synchronous `PreToolUse` hook output:

```python
{
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "Bobi blocks agents from editing its installed framework, venv, "
            "or package-managed dependency directories. Report the attempted "
            "change and implement it in the source repo via PR instead."
        ),
    }
}
```

The reason should include the matched path class, but not expose secrets or full
environment dumps. Absolute local paths are acceptable because the agent already
attempted to write them and Bobi transcripts are local operational records.

### 5. Doctor Integrity Check For Bobi Distribution

Add `_check_bobi_install_integrity()` to `bobi/doctor.py` and include it in
`run_doctor()`.

The check should use `importlib.metadata.distribution("bobi")` and the wheel's
`RECORD` metadata when available:

- For each file with a `sha256=` hash in `dist.files`, compare the current bytes
  to the recorded hash.
- Skip files without hashes, missing metadata, editable source installs, and
  local source checkouts where no wheel `RECORD` exists.
- Skip non-`sha256` hashes and report unreadable hashed files as failures.
- Fail when hashed files are missing or differ.
- The hint should instruct the operator to reinstall or upgrade Bobi and move
  any desired changes into a source PR.

Implement the metadata lookup behind a small helper so tests can inject a
fixture distribution without depending on the test runner's own installation
layout.

This is detection only. It complements the hook guard and catches:

- Drift that happened before the guard existed.
- Out-of-band edits made outside Claude tool hooks.
- Tooling paths that bypass the hook layer.

## Testing Plan

Unit tests:

- Hook composition preserves existing `AskUserQuestion` defer behavior.
- `Write`, `Edit`, `MultiEdit`, and `NotebookEdit` are denied for protected
  Bobi, site-packages, venv, and dependency-directory paths.
- The same tools are allowed for normal repo/workspace paths.
- Missing destination files inside protected roots are denied.
- Bash commands with explicit protected-path mutation are denied.
- Read-only Bash commands against protected paths are allowed.
- Existing caller-provided hooks still run and default hooks are appended.
- A caller-provided hook for the same tool cannot bypass the guard by returning
  `allow` before the guard runs.
- `stream_once()` receives the same default protection as persistent sessions.
- Editable Bobi source checkouts used as the assigned task repo remain writable.
- Non-editable Bobi wheel installs under `site-packages` are protected.
- Symlinked workspace paths that resolve into protected roots are denied.
- Relative paths from a protected `cwd` are denied.
- Bash mutation after `cd <protected-root>` is denied.
- Doctor passes for editable/source installs without `RECORD`.
- Doctor fails with a clear detail when a hashed Bobi wheel file differs from
  its `RECORD` digest.
- Doctor handles missing distributions, missing `dist.files`, unreadable hashed
  files, missing hashed files, mismatched hashes, and non-`sha256` hashes.

Integration or smoke tests:

- A focused real-Claude smoke, gated on the CLI, attempts to write a temporary
  file under a synthetic protected root and asserts the write is blocked.
- The real-Claude smoke verifies that the `permissionDecision: "deny"` hook
  output shape actually blocks a tool call with the installed SDK.
- `bobi agent <name> doctor` reports Bobi install drift when the check is fed a
  fixture distribution with a mismatched file.

## Rollout

1. Ship the hook guard enabled by default for Claude-backed agents.
2. Ship doctor drift detection in the same release.
3. Document the boundary in `docs/SECURITY.md`: agents may edit task source and
   workspace files, but framework installs, venvs, and package-managed
   dependency directories are protected.
4. Keep the existing `bypassPermissions` default for now; revisit a narrower
   permission mode after the guard has production coverage.

## Risks And Mitigations

- **False positives in source checkouts:** only protect Bobi's imported package
  path and package-managed directories, not every path named `bobi/`.
- **Shell parsing gaps:** start conservative and deny obvious write forms with
  explicit protected paths; do not frame this as a complete shell sandbox, and
  rely on doctor plus external filesystem isolation as defense in depth.
- **Hook compatibility:** merge hooks rather than replacing caller hooks, and
  keep tests around existing deferred question behavior.
- **Non-Claude brains:** document that this enforcement depends on Claude hook
  support; keep doctor detection brain-agnostic.
