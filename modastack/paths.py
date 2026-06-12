"""Canonical filesystem layout — the ONLY place .modastack paths come from.

There is exactly one .modastack/ directory per installation, holding both
config and state. Exactly one function resolves it from the filesystem:
resolve_root(). Every path in the system derives from that answer via the
helpers below. No module may build a '.modastack' literal, walk the
filesystem for one, or chain alternatives ("passed value or global or
cwd") — those fallbacks are how agents historically forked their identity
and scattered state across repo checkouts.

Binding contract — every process binds its root exactly once, at its
entry point, then everything downstream reads the binding:
- the manager binds when `modastack start` resolves its target
- a child agent binds the `root` its spawner passed in the args blob
- a CLI command binds what resolve_root() finds from cwd
There is no resolution at use sites: modastack_root() returns the bound
root or raises. An unbound process deep in library code is a programming
error; walking from cwd there would mask it.

The one sanctioned exception is `modastack install`, which targets its
literal cwd because it CREATES the root; walking up there would nest new
projects into enclosing ones.
"""

from __future__ import annotations

import os
from pathlib import Path

# The artifact that marks an installed root. Written verbatim by
# `modastack install`; state-only .modastack/ dirs never contain it.
ROOT_MARKER = "agent.yaml"

_root: Path | None = None


def bind_root(path: Path | None) -> None:
    """Bind this process's installation root (None unbinds, tests only).

    A process has exactly one identity: rebinding to a DIFFERENT root is
    a bug (it would silently re-identify a running process — registry,
    state, and log writes would split across two roots), so it raises.
    Rebinding to the same resolved path is a no-op.
    """
    global _root
    if path is None:
        _root = None
        return
    resolved = path.resolve()
    if _root is not None and resolved != _root:
        raise RuntimeError(
            f"Modastack root already bound to {_root} — refusing to rebind "
            f"to {resolved}. A process binds its identity exactly once."
        )
    _root = resolved


def bound_root() -> Path | None:
    """The bound root, or None. For advisory contexts (doctor, status
    displays) that must not raise. Everything else should call
    modastack_root()."""
    return _root


def modastack_root() -> Path:
    """The bound installation root. Raises when no entry point has bound
    one — there is no fallback; a process that cannot identify its
    installation must not invent one."""
    if _root is None:
        raise RuntimeError(
            "Modastack root not bound — the process entry point must call "
            "bind_root(resolve_root(...)) (CLI/manager) or bind the root "
            "passed by its spawner (child agents)."
        )
    return _root


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
    """THE filesystem resolver for the Modastack working directory.

    Walks up from `start` to the nearest ancestor whose .modastack/
    contains agent.yaml and returns it. Raises when no installed root
    exists. Only entry points call this, and they bind what it returns.

    When MODASTACK_ROOT is set in the environment, it short-circuits the
    walk-up — managed child processes inherit the correct root without
    filesystem guessing. A set-but-invalid MODASTACK_ROOT raises: the
    spawning process is broken (stale env, typo, deleted install) and
    silently walking from cwd would risk binding a different root —
    the same identity-fork failure mode as #245.

    Linked git worktrees (where .git is a file, not a directory) are
    skipped even when they carry a checked-in agent.yaml, preventing
    the worktree from capturing root resolution (#247).
    """
    env_root = os.environ.get("MODASTACK_ROOT")
    if env_root:
        p = Path(env_root).resolve()
        if (p / ".modastack" / ROOT_MARKER).is_file():
            return p
        raise RuntimeError(
            f"MODASTACK_ROOT is set to {p} but it is not a valid "
            f"Modastack installation (missing .modastack/{ROOT_MARKER}). "
            f"The spawning process has a stale or incorrect root — fix "
            f"the environment rather than falling back to walk-up."
        )

    origin = start.resolve()
    for candidate in (origin, *origin.parents):
        if (candidate / ".modastack" / ROOT_MARKER).is_file():
            if _is_linked_worktree(candidate):
                continue
            return candidate
    raise RuntimeError(
        f"no Modastack installation found above {origin} — "
        f"expected an ancestor with .modastack/{ROOT_MARKER}. "
        f"Run `modastack install` to create one."
    )


# --- Path constructors -----------------------------------------------------
# Each takes an explicit root when the caller already has one (passing it
# down is parameterization, not fallback) and otherwise derives it from
# modastack_root() — the bound root, never a guess.

def modastack_dir(root: Path | None = None) -> Path:
    return (root if root is not None else modastack_root()) / ".modastack"


def agent_yaml_path(root: Path | None = None) -> Path:
    return modastack_dir(root) / ROOT_MARKER


def install_manifest_path(root: Path | None = None) -> Path:
    return modastack_dir(root) / "install-manifest.json"


def workflows_dir(root: Path | None = None) -> Path:
    return modastack_dir(root) / "workflows"


def roles_dir(root: Path | None = None) -> Path:
    return modastack_dir(root) / "roles"


def tools_dir(root: Path | None = None) -> Path:
    return modastack_dir(root) / "tools"


def context_dir(root: Path | None = None) -> Path:
    return modastack_dir(root) / "context"


def agents_dir(root: Path | None = None) -> Path:
    return modastack_dir(root) / "agents"


def monitors_dir(root: Path | None = None) -> Path:
    return modastack_dir(root) / "monitors"


def state_path(root: Path | None = None) -> Path:
    """State directory path only — no mkdir, safe for read-only contexts
    (doctor probes, list commands, path constructors)."""
    return modastack_dir(root) / "state"


def state_dir(root: Path | None = None) -> Path:
    """Runtime state directory (created on demand). Writers use this;
    read-only contexts use state_path()."""
    d = state_path(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def manager_pid_path(root: Path | None = None) -> Path:
    """Path only — no mkdir, safe to probe on unowned directories."""
    return state_path(root) / "manager.pid"


def sessions_dir(root: Path | None = None) -> Path:
    """Session registry directory (created on demand)."""
    d = modastack_dir(root) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def worktrees_dir(root: Path | None = None) -> Path:
    return modastack_dir(root) / "worktrees"
