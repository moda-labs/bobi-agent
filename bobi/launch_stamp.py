"""What each long-lived local process was launched from (#928).

A local `uv tool install --upgrade` replaces bobi's files *underneath* the
processes already running them. The manager and the local event server keep
serving code that no longer exists on disk, and nothing noticed: doctor probed
`/health`, got an answer, and reported a six-day-old event server healthy while
the `dist/local.js` it executes had long since been overwritten.

So every long-lived local process records what it launched from - the installed
bobi version, plus the digest of the artifact it executes where there is a
single one - next to the pid file it already writes. :func:`inspect_processes`
compares those records against what is installed *now*. That is a comparison of
two recorded facts, not a heuristic about process age versus file mtime: a
touched file is not drift, and replaced bytes are drift even at the same
version.

Container deployments need no special case. The image is the unit of update
there, so every process is started from the image's own bobi and the recorded
version is always the installed one - the comparison simply never fires. A
container someone hot-patches in place *has* drifted, and should hear about it
like any other host.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from bobi import paths
from bobi.fsutil import atomic_write_json

log = logging.getLogger(__name__)

MANAGER = "manager"
EVENT_SERVER = "event-server"

# component id -> (operator-facing name, the `bobi agent <name> ...` verb that
# replaces the process with one running the installed code).
_COMPONENTS: dict[str, tuple[str, str]] = {
    MANAGER: ("manager", "restart"),
    EVENT_SERVER: ("event server", "event-server restart"),
}


@dataclass(frozen=True)
class ProcessLaunch:
    """A running local process, and whether it still matches what is installed."""

    name: str  # how the process is named to an operator, e.g. "event server"
    pid: int
    stale: bool
    detail: str
    remedy: str


def installed_bobi_version() -> str:
    """The bobi version installed on this host right now."""
    from bobi.__version__ import __version__

    return __version__


def stamp_path(root: Path | None, component: str) -> Path:
    return paths.state_path(root) / f"{component}.launch.json"


def pid_path(root: Path | None, component: str) -> Path:
    if component == MANAGER:
        return paths.manager_pid_path(root)
    return paths.event_server_pid_path(root)


def record_launch(root: Path | None, component: str, pid: int, *,
                  artifact: Path | None = None) -> None:
    """Record what *component* just launched from, beside its pid file.

    Best-effort by design: a start must never fail over its own bookkeeping.
    A stamp that could not be written reads back as "launch version unknown",
    which is what doctor reports.
    """
    stamp: dict[str, object] = {
        "component": component,
        "pid": int(pid),
        "bobi_version": installed_bobi_version(),
    }
    if artifact is not None:
        stamp["artifact"] = str(artifact)
        stamp["artifact_sha256"] = _digest(artifact)
    try:
        atomic_write_json(stamp_path(root, component), stamp)
    except OSError as exc:
        log.warning("Could not record the %s launch stamp: %s", component, exc)


def clear_launch(root: Path | None, component: str, *,
                 pid: int | None = None) -> None:
    """Drop *component*'s stamp on shutdown.

    Guarded by *pid* the way the pid files are: a process that lost a restart
    race must not delete its successor's stamp.
    """
    stamp = _read_stamp(root, component)
    if pid is not None and (stamp or {}).get("pid") != pid:
        return
    try:
        stamp_path(root, component).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not clear the %s launch stamp: %s", component, exc)


def inspect_processes(root: Path | None) -> list[ProcessLaunch]:
    """Every long-lived local process that is running, and its launch verdict."""
    from bobi.sdk import pid_alive, read_pid

    installed = installed_bobi_version()
    agent = _agent_name(root)
    out: list[ProcessLaunch] = []
    for component, (name, verb) in _COMPONENTS.items():
        pid = read_pid(pid_path(root, component))
        if not pid_alive(pid):
            continue
        remedy = f"bobi agent {agent} {verb}"
        detail = _verdict(root, component, pid, installed)
        out.append(ProcessLaunch(
            name=name,
            pid=pid,
            stale=detail is not None,
            detail=detail or f"pid {pid}, launched from bobi {installed}",
            remedy=remedy,
        ))
    return out


def stale_processes(root: Path | None) -> list[ProcessLaunch]:
    """The running processes executing code the installed bobi has replaced."""
    return [p for p in inspect_processes(root) if p.stale]


def _verdict(root: Path | None, component: str, pid: int,
             installed: str) -> str | None:
    """Why *component*'s running process is stale, or None when it matches."""
    stamp = _read_stamp(root, component)
    # A stamp is written at launch and names its own pid, so one that names a
    # different pid belongs to an earlier process and says nothing about this
    # one. Either way the running process was launched by a bobi that recorded
    # nothing - which is every version before this check existed.
    if not stamp or stamp.get("pid") != pid:
        return (f"pid {pid} was launched by an older bobi than the installed "
                f"{installed} (launch version unknown)")

    launched_with = str(stamp.get("bobi_version") or "")
    if launched_with != installed:
        return (f"pid {pid} is running bobi {launched_with or 'unknown'}, "
                f"installed is {installed}")

    artifact = stamp.get("artifact")
    if artifact and _digest(Path(str(artifact))) != stamp.get("artifact_sha256"):
        return (f"pid {pid} is running a replaced "
                f"{Path(str(artifact)).name} (the file changed on disk since "
                f"it was launched)")
    return None


def _read_stamp(root: Path | None, component: str) -> dict | None:
    try:
        stamp = json.loads(stamp_path(root, component).read_text())
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) else None


def _digest(path: Path) -> str:
    """sha256 of *path*, or "" when it cannot be read (deleted, unreadable).

    "" never equals a recorded digest, so an artifact that vanished under a
    running process reads as replaced - which it is.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _agent_name(root: Path | None) -> str:
    try:
        return paths.agent_name_for_root(root)
    except Exception:
        return "<name>"
