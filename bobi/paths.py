"""Canonical filesystem layout — the ONLY place .bobi paths come from.

There is exactly one .bobi/ directory per installation, holding both
config and state. Exactly one function resolves it from the filesystem:
resolve_root(). Every path in the system derives from that answer via the
helpers below. No module may build a '.bobi' literal, walk the
filesystem for one, or chain alternatives ("passed value or global or
cwd") — those fallbacks are how agents historically forked their identity
and scattered state across repo checkouts.

Binding contract — every process binds its root exactly once, at its
entry point, then everything downstream reads the binding:
- the manager binds when `bobi start` resolves its target
- a child agent binds the `root` its spawner passed in the args blob
- a CLI command binds what resolve_root() finds from cwd
There is no resolution at use sites: bobi_root() returns the bound
root or raises. An unbound process deep in library code is a programming
error; walking from cwd there would mask it.

The one sanctioned exception is `bobi install`, which targets its
literal cwd because it CREATES the root; walking up there would nest new
projects into enclosing ones.

Trust model (#249):
- Ownership: on Unix, resolve_root() refuses candidates whose
  .bobi/ directory is not owned by the current process uid.  This
  prevents a second party on a shared host from planting a marker in a
  writable ancestor and capturing identity, credentials, and code
  execution.
- Environment pin: bind_root() propagates BOBI_ROOT into
  os.environ so every subprocess (monitors, CLI children) inherits the
  bound root without re-walking the filesystem.  Combined with the
  explicit pin in subagent.py, this removes the walk from all managed
  child contexts.
"""

from __future__ import annotations

import os
from pathlib import Path

# The artifact that marks an installed root. Written verbatim by
# `bobi install`; state-only .bobi/ dirs never contain it.
ROOT_MARKER = "agent.yaml"

_root: Path | None = None

# BOBI_ROOT present in the environment when this process STARTED is a
# pin inherited from a spawner (subagent.py sets it for the CLI commands a
# child agent runs inside its worktree, #249). resolve_root honors THIS
# value — captured once at import, before any bind_root runs.
#
# It deliberately does NOT track the value bind_root later writes to
# os.environ for our own subprocesses: that write is this process's own
# echo, and consulting it would make a later in-process resolve_root ignore
# an explicit, different `start` once we have bound (#375). The env var is
# still the channel subprocesses inherit through — they capture it at their
# own import, the same way we capture it here.
_inherited_root_env: str | None = os.environ.get("BOBI_ROOT")


def bind_root(path: Path | None) -> None:
    """Bind this process's installation root (None unbinds, tests only).

    A process has exactly one identity: rebinding to a DIFFERENT root is
    a bug (it would silently re-identify a running process — registry,
    state, and log writes would split across two roots), so it raises.
    Rebinding to the same resolved path is a no-op.

    On successful bind, BOBI_ROOT is set in os.environ so every
    child process (monitors, CLI sub-invocations) inherits the pinned
    root without re-walking the filesystem (#249).
    """
    global _root
    if path is None:
        _root = None
        os.environ.pop("BOBI_ROOT", None)
        return
    resolved = path.resolve()
    if _root is not None and resolved != _root:
        raise RuntimeError(
            f"Bobi root already bound to {_root} — refusing to rebind "
            f"to {resolved}. A process binds its identity exactly once."
        )
    _root = resolved
    os.environ["BOBI_ROOT"] = str(resolved)


def bound_root() -> Path | None:
    """The bound root, or None. For advisory contexts (doctor, status
    displays) that must not raise. Everything else should call
    bobi_root()."""
    return _root


def bobi_root() -> Path:
    """The bound installation root. Raises when no entry point has bound
    one — there is no fallback; a process that cannot identify its
    installation must not invent one."""
    if _root is None:
        raise RuntimeError(
            "Bobi root not bound — the process entry point must call "
            "bind_root(resolve_root(...)) (CLI/manager) or bind the root "
            "passed by its spawner (child agents)."
        )
    return _root


def _is_owned_by_current_user(marker_dir: Path) -> bool:
    """True when the .bobi/ directory is owned by the running process's uid.

    On non-Unix platforms (no os.getuid), returns True unconditionally —
    the ownership check is a Unix defense layer and is not the sole guard.
    """
    if not hasattr(os, "getuid"):
        return True  # non-Unix: skip ownership check
    try:
        return marker_dir.stat().st_uid == os.getuid()
    except OSError:
        return False


def _is_linked_worktree(path: Path) -> bool:
    """True when path is a git linked worktree (not a main working tree).

    Git linked worktrees have a .git FILE (not directory) whose content
    starts with 'gitdir:'. The main working tree has a .git DIRECTORY.
    """
    dot_git = path / ".git"
    if dot_git.is_file():
        try:
            return dot_git.read_text().strip().startswith("gitdir:")
        except OSError:
            return False
    return False


def resolve_root(start: Path) -> Path:
    """THE filesystem resolver for the Bobi working directory.

    Walks up from `start` to the nearest ancestor whose .bobi/
    contains agent.yaml and returns it. Raises when no installed root
    exists. Only entry points call this, and they bind what it returns.

    When BOBI_ROOT was set in the environment at process start, it
    short-circuits the walk-up — managed child processes inherit the
    correct root without filesystem guessing. A set-but-invalid inherited
    BOBI_ROOT raises: the spawning process is broken (stale env,
    typo, deleted install) and silently walking from cwd would risk
    binding a different root — the same identity-fork failure mode as #245.

    Only the pin INHERITED at process start short-circuits — not the value
    this process's own bind_root later writes to os.environ for its
    subprocesses. Honoring our own echo would make a later resolve_root
    ignore an explicit, different `start` once we have bound (#375).

    Trust guards applied during the walk (#249):
    - Linked git worktrees (where .git is a file, not a directory) are
      skipped even when they carry a checked-in agent.yaml (#247).
    - On Unix, candidates whose .bobi/ directory is not owned by
      the current uid are skipped — a second party on a shared host
      planting a marker in a writable ancestor cannot capture identity.
    """
    env_root = _inherited_root_env
    if env_root:
        p = Path(env_root).resolve()
        if (p / ".bobi" / ROOT_MARKER).is_file():
            return p
        raise RuntimeError(
            f"BOBI_ROOT is set to {p} but it is not a valid "
            f"Bobi installation (missing .bobi/{ROOT_MARKER}). "
            f"The spawning process has a stale or incorrect root — fix "
            f"the environment rather than falling back to walk-up."
        )

    origin = start.resolve()
    for candidate in (origin, *origin.parents):
        marker_dir = candidate / ".bobi"
        if (marker_dir / ROOT_MARKER).is_file():
            if _is_linked_worktree(candidate):
                continue
            if not _is_owned_by_current_user(marker_dir):
                continue
            return candidate
    raise RuntimeError(
        f"no Bobi installation found above {origin} — "
        f"expected an ancestor with .bobi/{ROOT_MARKER}. "
        f"Run `bobi install` to create one."
    )


# --- Path constructors -----------------------------------------------------
# Each takes an explicit root when the caller already has one (passing it
# down is parameterization, not fallback) and otherwise derives it from
# bobi_root() — the bound root, never a guess.

def bobi_dir(root: Path | None = None) -> Path:
    return (root if root is not None else bobi_root()) / ".bobi"


def agent_yaml_path(root: Path | None = None) -> Path:
    return bobi_dir(root) / ROOT_MARKER


def install_manifest_path(root: Path | None = None) -> Path:
    return bobi_dir(root) / "install-manifest.json"


def workflows_dir(root: Path | None = None) -> Path:
    return bobi_dir(root) / "workflows"


def roles_dir(root: Path | None = None) -> Path:
    return bobi_dir(root) / "roles"


def tools_dir(root: Path | None = None) -> Path:
    return bobi_dir(root) / "tools"


def context_dir(root: Path | None = None) -> Path:
    return bobi_dir(root) / "context"


def agents_dir(root: Path | None = None) -> Path:
    return bobi_dir(root) / "agents"


def monitors_dir(root: Path | None = None) -> Path:
    return bobi_dir(root) / "monitors"


def state_path(root: Path | None = None) -> Path:
    """State directory path only — no mkdir, safe for read-only contexts
    (doctor probes, list commands, path constructors)."""
    return bobi_dir(root) / "state"


def state_dir(root: Path | None = None) -> Path:
    """Runtime state directory (created on demand). Writers use this;
    read-only contexts use state_path()."""
    d = state_path(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def manager_pid_path(root: Path | None = None) -> Path:
    """Path only — no mkdir, safe to probe on unowned directories."""
    return state_path(root) / "manager.pid"


def policy_path(root: Path | None = None) -> Path:
    """The team-scoped policy.md — the single curated learning substrate (#456).

    Path only (no mkdir), safe for read-only contexts (prompt injection,
    doctor probes). The curator is the sole writer; every working agent reads
    it injected read-only. Replaces the per-session memory/<session>/INDEX.md
    decision log."""
    return state_path(root) / "policy.md"


def policy_cursor_path(root: Path | None = None) -> Path:
    """The curator's consumption watermark — the highest messages.id it has
    ingested (#456). A plain integer, advanced only after a successful rewrite,
    so a curator run that dies mid-distillation re-reads its window. Path only;
    the curator ensures the parent exists when it writes."""
    return state_path(root) / "policy_cursor"


def sessions_dir(root: Path | None = None) -> Path:
    """Session registry directory (created on demand)."""
    d = bobi_dir(root) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def worktrees_dir(root: Path | None = None) -> Path:
    return bobi_dir(root) / "worktrees"
