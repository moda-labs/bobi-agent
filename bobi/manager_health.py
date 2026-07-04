"""Manager health endpoint — lightweight HTTP server for container liveness
and readiness probes.

Exposes ``GET /health`` on a bind address/port and writes the port number to
``state/manager-health.port`` for discovery. Defaults preserve the original
localhost ephemeral-port behavior. Designed for PID-1 container mode where an
orchestrator (Fly, ECS, k8s) needs a machine-readable health signal.

The server runs in a daemon thread so it never blocks the manager's main
loop.  Graceful shutdown via :func:`stop`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

log = logging.getLogger(__name__)

_server: HTTPServer | None = None
_thread: threading.Thread | None = None
_port_file: Path | None = None


class _HealthServer(HTTPServer):
    allow_reuse_address = True


def _make_handler(manager_pid: int, project_name: str,
                  session_status_fn, manager_block_fn):
    """Build the request handler class with closed-over manager state."""

    class HealthHandler(BaseHTTPRequestHandler):

        def do_GET(self):
            if self.path == "/health":
                self._json_response(200, self._health_body())
            elif self.path == "/ready":
                body = self._ready_body()
                code = 200 if body["status"] == "ready" else 503
                self._json_response(code, body)
            else:
                self._json_response(404, {"error": "not found"})

        def _health_body(self):
            body = {
                "status": "ok",
                "pid": manager_pid,
                "project": project_name,
            }
            # The entry-point (director) session's progress signal — the
            # input the #464 watchdog needs to tell a wedged director apart
            # from a healthy idle one. Additive; omitted when no manager
            # session is wired so existing consumers keep the old shape.
            manager = manager_block_fn() if manager_block_fn else None
            if manager is not None:
                body["manager"] = manager
            body["sessions"] = session_status_fn()
            return body

        def _ready_body(self):
            manager = manager_block_fn() if manager_block_fn else None
            if manager and manager.get("status") in {"running", "idle"}:
                return {"status": "ready", "manager": manager}
            body = {"status": "not_ready"}
            if manager is not None:
                body["manager"] = manager
            return body

        def _json_response(self, code: int, data: dict):
            payload = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return HealthHandler


def _manager_block_from_registry(manager_session: str | None):
    """Build the entry-point session's progress block from the registry.

    Server-side derivation of ``idle_seconds`` keeps the watchdog dumb (no
    clock-skew handling). Returns None when no manager session is wired so the
    payload stays backward-compatible. Fails open: a missing entry (the
    pre-spawn window) reports ``status="starting"`` with ``idle_seconds=0`` so
    the watchdog never restarts a manager that has not finished booting.
    """
    if not manager_session:
        return None
    try:
        from bobi.sdk import get_registry
        entry = get_registry().get(manager_session)
    except Exception:
        return None
    if entry is None:
        return {
            "session": manager_session,
            "status": "starting",
            "last_activity": None,
            "idle_seconds": 0.0,
        }
    return {
        "session": entry.name,
        "status": entry.status,
        "last_activity": entry.last_activity,
        "idle_seconds": max(0.0, time.time() - entry.last_activity),
    }


def _session_status_from_registry():
    """Pull live session info from the on-disk registry (best-effort)."""
    try:
        from bobi.sdk import get_registry
        registry = get_registry()
        active = registry.list_active()
        return [
            {"name": e.name, "role": e.role, "status": e.status}
            for e in active
        ]
    except Exception:
        return []


def _configured_bind() -> str:
    return os.environ.get("BOBI_HEALTH_BIND", "127.0.0.1") or "127.0.0.1"


def _configured_port() -> int:
    raw = os.environ.get("BOBI_HEALTH_PORT")
    if raw in (None, ""):
        return 0
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("BOBI_HEALTH_PORT must be an integer") from exc
    if port < 0 or port > 65535:
        raise ValueError("BOBI_HEALTH_PORT must be between 0 and 65535")
    return port


def start(state_dir: Path, project_name: str,
          session_status_fn=None, manager_session: str | None = None,
          manager_status_fn=None) -> int:
    """Start the health server. Returns the bound port.

    ``session_status_fn`` is a callable returning a list of session dicts
    for the ``sessions`` key in the health payload.  Defaults to reading
    the on-disk session registry.

    ``manager_session`` names the entry-point (director) session; when given,
    the payload gains a top-level ``manager`` block with that session's
    ``status``, ``last_activity`` and server-derived ``idle_seconds`` — the
    progress signal the #464 self-heal watchdog observes. ``manager_status_fn``
    overrides the default registry lookup (used by tests).
    """
    global _server, _thread, _port_file

    if _server is not None:
        return _server.server_address[1]

    manager_pid = os.getpid()
    status_fn = session_status_fn or _session_status_from_registry
    if manager_status_fn is not None:
        manager_block_fn = manager_status_fn
    else:
        manager_block_fn = lambda: _manager_block_from_registry(manager_session)

    handler = _make_handler(manager_pid, project_name, status_fn,
                            manager_block_fn)
    bind = _configured_bind()
    configured_port = _configured_port()
    _server = _HealthServer((bind, configured_port), handler)
    port = _server.server_address[1]

    _port_file = state_dir / "manager-health.port"
    _port_file.write_text(str(port))

    _thread = threading.Thread(target=_server.serve_forever, daemon=True,
                               name="manager-health")
    _thread.start()

    log.info("Manager health server listening on %s:%d", bind, port)
    return port


def stop():
    """Shut down the health server and clean up the port file."""
    global _server, _thread, _port_file

    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    _thread = None

    if _port_file is not None:
        _port_file.unlink(missing_ok=True)
        _port_file = None


def health(base_url: str, timeout: float = 2) -> dict | None:
    """Probe the manager health endpoint.  Returns the parsed payload or None."""
    from bobi import http as pooled

    try:
        resp = pooled.get(f"{base_url}/health", timeout=timeout)
        data = resp.json()
        return data if data.get("status") == "ok" else None
    except Exception:
        return None
