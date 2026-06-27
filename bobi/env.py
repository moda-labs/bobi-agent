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
import re
from pathlib import Path

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _configured_brain_kind(root: Path, env: dict[str, str] | None = None) -> str:
    """Return the team's configured brain kind from the installation root."""
    try:
        import yaml
        from bobi import paths
        raw = yaml.safe_load(
            paths.agent_yaml_path(root).read_text()
        ) or {}
    except Exception:
        return ""
    brain = raw.get("brain", {})
    if not isinstance(brain, dict):
        return ""
    kind = str(brain.get("kind", "") or "")
    if not kind:
        return ""
    lookup = os.environ if env is None else env
    return _ENV_VAR_RE.sub(lambda m: lookup.get(m.group(1), ""), kind)


def _load_dotenv_into(env: dict[str, str], root: Path) -> None:
    """Merge the runtime ``.env`` into *env* without overriding parent values."""
    try:
        from bobi import paths
        from bobi.config import parse_env_file
        values = parse_env_file(paths.env_path(root))
    except Exception:
        return
    for key, value in values.items():
        env.setdefault(key, value)


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


def child_agent_env(root: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Return the full environment contract for a launched child agent.

    The contract is intentionally centralized here:

    - inherit the parent runtime environment so tool configuration and ambient
      credentials (``OPENAI_API_KEY``, ``VENN_API_KEY``, ``GH_TOKEN``, etc.)
      follow child agents;
    - merge the installed team's runtime ``.env`` for credentials captured
      during setup, without overriding explicit parent process values;
    - normalize ``PATH`` through :func:`agent_spawn_env`, keeping MCP preflight
      and runtime tool lookup identical;
    - pin identity with the current installation ``BOBI_ROOT``;
    - override stale parent ``BOBI_BRAIN`` with the installed team's
      configured ``brain.kind`` when present.

    Stale parent identity values are never inherited: ``BOBI_ROOT`` is
    always rewritten to *root*, and ``BOBI_BRAIN`` is rewritten when the
    installed team declares a brain.
    """
    resolved_root = root.resolve()
    env = agent_spawn_env(base)
    _load_dotenv_into(env, resolved_root)
    env["BOBI_ROOT"] = str(resolved_root)

    from bobi.brain import BRAIN_ENV

    brain_kind = _configured_brain_kind(resolved_root, env)
    if brain_kind:
        env[BRAIN_ENV] = brain_kind
    else:
        env.pop(BRAIN_ENV, None)
    return env
