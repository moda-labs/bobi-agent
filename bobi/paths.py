"""Canonical filesystem layout for Bobi Agents.

All managed Bobi paths derive from one low-level home root:

    BOBI_HOME if set, else ~/.bobi

The home root is not read from config. A named Bobi Agent has one slot under
``<home>/agents/<name>/``:

    src/     editable source, default location
    run/     selected runtime root, exported to children as BOBI_ROOT

Inside ``run/``, generated package files live in ``package/`` and mutable
runtime state lives in ``state/``. Runtime code binds exactly one ``run/`` root
per process; no code should infer identity from cwd.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT_MARKER = "agent.yaml"

_root: Path | None = None


# --- Binding ---------------------------------------------------------------

def bind_root(path: Path | None) -> None:
    """Bind this process to one Bobi Agent runtime root.

    ``None`` unbinds for tests. A non-None binding is written to ``BOBI_ROOT``
    so child processes inherit the same runtime identity without cwd probing.
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
    return _root


def bobi_root() -> Path:
    if _root is None:
        raise RuntimeError(
            "Bobi root not bound — run through `bobi agent <name> ...` "
            "or bind the BOBI_ROOT runtime root passed by a spawner."
        )
    return _root


# --- Home / agent slots ----------------------------------------------------

def home_dir() -> Path:
    raw = os.environ.get("BOBI_HOME")
    return Path(raw).expanduser().resolve() if raw else (Path.home() / ".bobi").resolve()


def global_config_path() -> Path:
    return home_dir() / "config.yaml"


def agents_root() -> Path:
    return home_dir() / "agents"


def agent_dir(name: str) -> Path:
    return agents_root() / name


def agent_source_dir(name: str) -> Path:
    return agent_dir(name) / "src"


def agent_run_root(name: str) -> Path:
    return agent_dir(name) / "run"


def agent_runtime_root(name: str) -> Path:
    return agent_run_root(name)


def agent_name_for_root(root: Path | None = None) -> str:
    r = (root if root is not None else bobi_root()).resolve()
    return r.parent.name if r.name == "run" else r.name


def list_agents() -> list[str]:
    root = agents_root()
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and (d / "run" / "package" / ROOT_MARKER).is_file()
    )


def resolve_root_for_agent(name: str) -> Path:
    root = agent_run_root(name).resolve()
    if agent_yaml_path(root).is_file():
        return root
    installed = list_agents()
    suffix = f" Installed Bobi Agents: {', '.join(installed)}." if installed else ""
    raise RuntimeError(
        f"Bobi Agent '{name}' is not installed at {root} "
        f"(missing package/{ROOT_MARKER}).{suffix}"
    )


def resolve_root(start: Path | None = None) -> Path:
    """Resolve a runtime root only from inherited BOBI_ROOT or an explicit root.

    The old cwd walk-up is intentionally gone. ``start`` is accepted only when
    it is itself a valid runtime root; callers that want an installed agent by
    name should use :func:`resolve_root_for_agent`.
    """
    env_root = os.environ.get("BOBI_ROOT")
    if env_root:
        p = Path(env_root).resolve()
        if agent_yaml_path(p).is_file():
            return p
        raise RuntimeError(
            f"BOBI_ROOT is set to {p} but it is not a valid Bobi Agent "
            f"runtime (missing package/{ROOT_MARKER})."
        )
    if start is not None:
        p = Path(start).resolve()
        if agent_yaml_path(p).is_file():
            return p
    raise RuntimeError(
        "No Bobi Agent runtime selected. Use `bobi agents list`, then "
        "`bobi agent <name> ...`."
    )


def ensure_global_config() -> Path:
    path = global_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Bobi machine config. BOBI_HOME controls this file's location.\n"
            "registries: []\n"
        )
    return path


# --- Runtime path constructors --------------------------------------------

def _runtime_root(root: Path | None = None) -> Path:
    return (root if root is not None else bobi_root()).resolve()


def package_dir(root: Path | None = None) -> Path:
    return _runtime_root(root) / "package"


def agent_yaml_path(root: Path | None = None) -> Path:
    return package_dir(root) / ROOT_MARKER


def install_manifest_path(root: Path | None = None) -> Path:
    return package_dir(root) / "install-manifest.json"


def compose_lock_path(root: Path | None = None) -> Path:
    return package_dir(root) / "compose-lock.json"


def workflows_dir(root: Path | None = None) -> Path:
    return package_dir(root) / "workflows"


def roles_dir(root: Path | None = None) -> Path:
    return package_dir(root) / "roles"


def tools_dir(root: Path | None = None) -> Path:
    return package_dir(root) / "tools"


def context_dir(root: Path | None = None) -> Path:
    return package_dir(root) / "context"


def monitors_dir(root: Path | None = None) -> Path:
    return package_dir(root) / "monitors"


def workspace_dir(root: Path | None = None) -> Path:
    return _runtime_root(root) / "workspace"


def env_path(root: Path | None = None) -> Path:
    return _runtime_root(root) / ".env"


def state_path(root: Path | None = None) -> Path:
    return _runtime_root(root) / "state"


def state_dir(root: Path | None = None) -> Path:
    d = state_path(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def manager_pid_path(root: Path | None = None) -> Path:
    return state_path(root) / "manager.pid"


def long_term_memory_path(root: Path | None = None) -> Path:
    return state_path(root) / "long_term_memory.md"


def long_term_memory_cursor_path(root: Path | None = None) -> Path:
    return state_path(root) / "long_term_memory_cursor"


def migrate_long_term_memory_state(root: Path | None = None) -> None:
    """Rename legacy policy state files in place when present.

    This is intentionally path-level and best-effort: fresh installs only use
    the new names, while dev installs that already produced the old files keep
    their document and cursor without a manual migration step.
    """
    state = state_path(root)
    legacy_memory = state / "policy.md"
    memory = long_term_memory_path(root)
    legacy_cursor = state / "policy_cursor"
    cursor = long_term_memory_cursor_path(root)
    try:
        if legacy_memory.is_file() and not memory.exists():
            legacy_memory.rename(memory)
        if legacy_cursor.is_file() and not cursor.exists():
            legacy_cursor.rename(cursor)
    except OSError:
        return


def policy_path(root: Path | None = None) -> Path:
    """Deprecated alias for one release; use long_term_memory_path."""
    return long_term_memory_path(root)


def policy_cursor_path(root: Path | None = None) -> Path:
    """Deprecated alias for one release; use long_term_memory_cursor_path."""
    return long_term_memory_cursor_path(root)


def sessions_dir(root: Path | None = None) -> Path:
    d = state_dir(root) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def worktrees_dir(root: Path | None = None) -> Path:
    return state_dir(root) / "worktrees"


def agent_cache_dir() -> Path:
    """Shared cache for registry-downloaded source packages."""
    return home_dir() / "cache" / "agents"


def build_cache_dir() -> Path:
    """Shared cache for generated build/deploy artifacts."""
    return home_dir() / "cache" / "build"
