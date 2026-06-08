"""Embedding sidecar client — auto-starts the sidecar and provides embed().

Follows the events/server.py ensure_running pattern: check health,
start if needed, poll until ready.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def _state_dir() -> Path:
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        raise RuntimeError("project root not set")
    d = root / ".modastack" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def is_running() -> bool:
    pid_p = _pid_path()
    if not pid_p.exists():
        return False
    try:
        pid = int(pid_p.read_text().strip())
    except (ValueError, OSError):
        return False
    if not _is_process_alive(pid):
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

    from modastack.sdk import get_project_root
    project_root = get_project_root()
    if not project_root:
        raise RuntimeError("project root not set")

    log.info("Starting embedding sidecar...")

    with open(_log_path(), "a") as lf:
        subprocess.Popen(
            [sys.executable, "-m", "modastack.kb.sidecar",
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


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts via the sidecar. Auto-starts if needed."""
    port = ensure_running()
    data = json.dumps({"texts": texts}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/embed",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    return result["embeddings"]


def stop() -> None:
    """Stop the sidecar if running."""
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
