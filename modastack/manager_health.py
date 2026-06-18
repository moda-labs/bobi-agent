"""Manager health endpoint — lightweight HTTP server for container liveness
and readiness probes.

Exposes ``GET /health`` on a localhost port and writes the port number to
``state/manager-health.port`` for discovery.  Designed for PID-1 container
mode where an orchestrator (Fly, ECS, k8s) needs a machine-readable health
signal.

The server runs in a daemon thread so it never blocks the manager's main
loop.  Graceful shutdown via :func:`stop`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

log = logging.getLogger(__name__)

_server: HTTPServer | None = None
_thread: threading.Thread | None = None
_port_file: Path | None = None


def _make_handler(manager_pid: int, project_name: str,
                  session_status_fn):
    """Build the request handler class with closed-over manager state."""

    class HealthHandler(BaseHTTPRequestHandler):

        def do_GET(self):
            if self.path == "/health":
                status = session_status_fn()
                body = {
                    "status": "ok",
                    "pid": manager_pid,
                    "project": project_name,
                    "sessions": status,
                }
                self._json_response(200, body)
            else:
                self._json_response(404, {"error": "not found"})

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


def _session_status_from_registry():
    """Pull live session info from the on-disk registry (best-effort)."""
    try:
        from modastack.sdk import get_registry
        registry = get_registry()
        active = registry.list_active()
        return [
            {"name": e.name, "role": e.role, "status": e.status}
            for e in active
        ]
    except Exception:
        return []


def start(state_dir: Path, project_name: str,
          session_status_fn=None) -> int:
    """Start the health server on a free port.  Returns the bound port.

    ``session_status_fn`` is a callable returning a list of session dicts
    for the ``sessions`` key in the health payload.  Defaults to reading
    the on-disk session registry.
    """
    global _server, _thread, _port_file

    if _server is not None:
        return _server.server_address[1]

    manager_pid = os.getpid()
    status_fn = session_status_fn or _session_status_from_registry

    handler = _make_handler(manager_pid, project_name, status_fn)
    _server = HTTPServer(("127.0.0.1", 0), handler)
    port = _server.server_address[1]

    _port_file = state_dir / "manager-health.port"
    _port_file.write_text(str(port))

    _thread = threading.Thread(target=_server.serve_forever, daemon=True,
                               name="manager-health")
    _thread.start()

    log.info("Manager health server listening on 127.0.0.1:%d", port)
    return port


def stop():
    """Shut down the health server and clean up the port file."""
    global _server, _thread, _port_file

    if _server is not None:
        _server.shutdown()
        _server = None
    _thread = None

    if _port_file is not None:
        _port_file.unlink(missing_ok=True)
        _port_file = None


def health(base_url: str, timeout: float = 2) -> dict | None:
    """Probe the manager health endpoint.  Returns the parsed payload or None."""
    from modastack import http as pooled

    try:
        resp = pooled.get(f"{base_url}/health", timeout=timeout)
        data = resp.json()
        return data if data.get("status") == "ok" else None
    except Exception:
        return None
