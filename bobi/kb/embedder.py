"""Embedding sidecar client — auto-starts the sidecar and provides embed().

Follows the events/server.py ensure_running pattern: check health,
start if needed, poll until ready.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


def _state_dir(root: Path | None = None) -> Path:
    from bobi import paths
    return paths.state_dir(root)


def _pid_path(root: Path | None = None) -> Path:
    return _state_dir(root) / "embedding-sidecar.pid"


def _port_path(root: Path | None = None) -> Path:
    return _state_dir(root) / "embedding-sidecar.port"


def _log_path() -> Path:
    return _state_dir() / "embedding-sidecar.log"


def _read_port() -> int | None:
    pp = _port_path()
    if not pp.exists():
        return None
    try:
        return int(pp.read_text().strip())
    except (ValueError, OSError):
        return None


def _check_health(port: int) -> bool:
    from bobi import http as pooled

    try:
        resp = pooled.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
        data = resp.json()
        return data.get("status") == "ok"
    except Exception:
        return False


def is_running() -> bool:
    from bobi.sdk import pid_alive, read_pid

    if not pid_alive(read_pid(_pid_path())):
        return False
    port = _read_port()
    if port is None:
        return False
    return _check_health(port)


def ensure_running() -> int:
    """Start the sidecar if not running. Returns the port."""
    port = _read_port()
    if port and _check_health(port):
        return port

    _pid_path().unlink(missing_ok=True)
    _port_path().unlink(missing_ok=True)

    from bobi.sdk import get_project_root
    project_root = get_project_root()
    if not project_root:
        raise RuntimeError("project root not set")

    log.info("Starting embedding sidecar...")

    with open(_log_path(), "a") as lf:
        subprocess.Popen(
            [sys.executable, "-m", "bobi.kb.sidecar",
             "--project-root", str(project_root)],
            stdout=lf, stderr=lf,
            start_new_session=True,
        )

    for _ in range(60):
        time.sleep(0.5)
        port = _read_port()
        if port and _check_health(port):
            log.info("Embedding sidecar ready on port %d", port)
            return port

    raise RuntimeError("Embedding sidecar failed to start within 30 seconds")


# Port of the last sidecar that answered an embed, so repeated embed()
# calls skip the health-check roundtrip. Invalidated on request failure.
_verified_port: int | None = None


def _post_embed(port: int, texts: list[str]) -> list[list[float]]:
    from bobi import http as pooled

    resp = pooled.post(
        f"http://127.0.0.1:{port}/embed",
        json={"texts": texts},
        timeout=60.0,
    )
    result = resp.json()
    return result["embeddings"]


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts via the sidecar. Auto-starts if needed."""
    global _verified_port
    port = _verified_port or ensure_running()
    try:
        embeddings = _post_embed(port, texts)
    except (OSError, httpx.RequestError):
        # Sidecar died since the last call - restart once and retry. The
        # pooled httpx client raises ConnectError, which is NOT an OSError,
        # so catching OSError alone would leave the dead port cached forever.
        _verified_port = None
        port = ensure_running()
        embeddings = _post_embed(port, texts)
    _verified_port = port
    return embeddings


def is_sidecar_argv(argv: list[str]) -> bool:
    """True when *argv* is an embedding sidecar process.

    `ensure_running` spawns it as ``python -m bobi.kb.sidecar
    --project-root <root>``, and that module name is what identifies it.
    """
    return "bobi.kb.sidecar" in argv


def stop(root: Path | None = None) -> None:
    """Stop the sidecar if running.

    ``root`` selects the team whose sidecar state to read; None means the
    process's bound root (in-team callers).

    Routed through `service.stop_pidfile` rather than signalling the pid
    directly: this sidecar holds a fastembed model, so it is the likeliest
    OOM victim on the box, and an OOM kill runs no cleanup - leaving
    `embedding-sidecar.pid` naming a pid the OS is free to reuse. `stop_team`
    calls this two lines after stopping the manager through that same seam,
    so a bare `os.kill` here would reintroduce, on the very next line, the
    stranger-killing bug the seam exists to prevent.
    """
    global _verified_port
    _verified_port = None
    from bobi.service import stop_pidfile

    result = stop_pidfile(
        _pid_path(root),
        identity=is_sidecar_argv,
        kind="the embedding sidecar",
        also_remove=[_port_path(root)],
    )
    if result.stopped or result.killed:
        log.info("Stopped embedding sidecar (pid %d)", result.pid)
    elif result.unidentified:
        log.warning("Embedding sidecar pid %d is unidentifiable - leaving it "
                    "running rather than signalling a stranger", result.pid)
