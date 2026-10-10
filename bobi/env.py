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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bobi.config import Config


def _configured_brain(
    root: Path, env: dict[str, str] | None = None,
) -> "Config":
    """Return the team's ``brain`` config from the installation root as a
    :class:`bobi.config.Config`, or an empty one when absent/unreadable.

    Interpolation is ``bobi.config._interpolate_env`` - the same resolver
    ``Config.load`` applies to this mapping - so ``${VAR:-default}`` resolves
    identically here and in the validate/manager paths (a divergence would
    pass validate yet pin an empty gateway base URL into every child).

    Only the ``brain`` key is read, rather than running the whole file through
    ``Config._parse``: this is the SPAWN path, and ``_parse`` raises on a
    malformed ``services:``/``requires:`` section, which would turn a
    surviving-but-broken team's gateway pin into a silent native session.
    Wrapping the mapping in a ``Config`` still routes every default and
    predicate through the ``brain_*`` properties, which is the duplication
    that mattered.
    """
    from bobi.config import Config
    try:
        import yaml
        from bobi import paths
        from bobi.config import _interpolate_env
        raw = yaml.safe_load(
            paths.agent_yaml_path(root).read_text()
        ) or {}
    except Exception:
        return Config()
    brain = raw.get("brain", {})
    if not isinstance(brain, dict):
        return Config()
    lookup = dict(os.environ) if env is None else env
    return Config(brain={
        str(key): _interpolate_env(str(value or ""), lookup)
        for key, value in brain.items()
    })


def pin_brain_from_root(
    root: Path, env: dict[str, str] | None = None,
) -> None:
    """Pin the installed team's brain selection from *root* into *env*.

    The single place agent.yaml ``brain.*`` becomes process env (``BOBI_BRAIN``
    / ``BOBI_BRAIN_MODEL`` / ``BOBI_BRAIN_EFFORT`` / the gateway pins, #655), shared by
    ``child_agent_env`` and the spawned child's own re-pin at startup
    (``subagent.py``).

    The expansion is ``pin_process_brain_from_config``, the same one the
    process-startup path takes, so the spawn path cannot fall behind a new
    ``brain.*`` field.
    """
    from bobi.brain import pin_process_brain_from_config

    pin_process_brain_from_config(_configured_brain(root, env), env)


def _load_dotenv_into(env: dict[str, str], root: Path) -> None:
    """Merge the runtime ``.env`` into *env* without overriding parent values."""
    try:
        from bobi import paths
        from bobi.config import _DOTENV_LOADED, parse_env_file
        values = parse_env_file(paths.env_path(root))
    except Exception:
        return
    for key, value in list(env.items()):
        if _DOTENV_LOADED.get(key) == value:
            env.pop(key)
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
      configured ``brain.kind`` when present;
    - strip any inherited ``BOBI_LAUNCH_LINEAGE``.

    Stale parent identity values are never inherited: ``BOBI_ROOT`` is always
    rewritten to *root*, ``BOBI_BRAIN`` is rewritten when the installed team
    declares a brain, and the launch chain is removed.

    The chain is stripped rather than propagated because this helper has four
    callers and only one is an agent launch (``subagent.launch_agent``; the
    others are the manager daemon spawn, dependency install, and the MCP
    probe). Stamping here would mean an agent running the routine ``bobi agent
    <x> restart`` copies its own chain into the new manager daemon, which then
    lives for days at depth N, so every launch it makes starts at N+1 and
    legitimate work is refused team-wide with no visible cause. Stripping makes
    a manager spawn a chain root for free, with no per-caller clearing to
    forget; ``launch_agent`` stamps the chain into the dict this returns,
    immediately before spawning.
    """
    from bobi.launch_lineage import strip as strip_launch_lineage

    resolved_root = root.resolve()
    env = agent_spawn_env(base)
    _load_dotenv_into(env, resolved_root)
    env["BOBI_ROOT"] = str(resolved_root)
    strip_launch_lineage(env)
    pin_brain_from_root(resolved_root, env)
    return env
