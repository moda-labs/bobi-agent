"""Embedding sidecar client — auto-starts the sidecar and provides embed().

Follows the events/server.py ensure_running pattern: check health,
start if needed, poll until ready.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _state_dir() -> Path:
    from bobi import paths
    return paths.state_dir()


def _pid_path() -> Path:
    return _state_dir() / "embedding-sidecar.pid"


def _port_path() -> Path:
    return _state_dir() / "embedding-sidecar.port"


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
    except OSError:
        # Sidecar died since the last call — restart once and retry.
        _verified_port = None
        port = ensure_running()
        embeddings = _post_embed(port, texts)
    _verified_port = port
    return embeddings


def stop() -> None:
    """Stop the sidecar if running."""
    global _verified_port
    _verified_port = None
    pid_p = _pid_path()
    if not pid_p.exists():
        return
    try:
        pid = int(pid_p.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        log.info("Stopped embedding sidecar (pid %d)", pid)
    except (ValueError, ProcessLookupError, OSError):
        pass
    pid_p.unlink(missing_ok=True)
    _port_path().unlink(missing_ok=True)
