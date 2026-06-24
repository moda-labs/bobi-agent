"""Environment construction for spawned agents.

Single source of truth for the environment used to launch agent subprocesses
(``subagent.py``) and to probe MCP servers during preflight (``validate.py``).
Keeping these identical by construction is what makes "preflight green ⇒ agent
works" hold for bare-name stdio MCP commands (MDS-64): a command on the user
PATH (e.g. ``~/.local/bin`` from ``uv tool install``) resolves the same way in
both paths, so a server can no longer pass preflight in the rich foreground
shell yet fail to spawn under the daemon's stripped PATH.
"""

from __future__ import annotations

import os
from pathlib import Path


def _user_bin_dirs() -> list[str]:
    """User-local executable dirs to prepend to PATH (D-64b).

    Kept minimal: ``~/.local/bin`` (the documented ``uv tool install`` target)
    and ``$XDG_BIN_HOME`` when set (which itself defaults to ``~/.local/bin``).
    """
    dirs = [str(Path.home() / ".local" / "bin")]
    xdg = os.environ.get("XDG_BIN_HOME")
    if xdg:
        dirs.append(xdg)
    return dirs


def agent_spawn_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment used to spawn agents / probe MCP servers.

    A copy of *base* (default ``os.environ``) with the user-bin dirs
    (:func:`_user_bin_dirs`) prepended to ``PATH``, de-duplicated while
    preserving order so the user-bin dirs win. Used by both ``subagent.py``'s
    detached agent launch and ``validate.py``'s MCP preflight probe so the two
    can never diverge (MDS-64).
    """
    env = dict(os.environ if base is None else base)
    existing = env.get("PATH", "")
    parts = existing.split(os.pathsep) if existing else []

    seen: set[str] = set()
    ordered: list[str] = []
    for p in _user_bin_dirs() + parts:
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    env["PATH"] = os.pathsep.join(ordered)
    return env
