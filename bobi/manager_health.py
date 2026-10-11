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
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

_server: HTTPServer | None = None
_thread: threading.Thread | None = None
_port_file: Path | None = None
_token_file: Path | None = None

# Header carrying the reload token. Follows the local web UIs' convention
# (bobi/webui_common/security.py) rather than Authorization: this is a local
# control-plane secret, not a bearer credential for an upstream service.
RELOAD_TOKEN_HEADER = "x-bobi-manager-token"

# How long a connection may hold a handler without completing a request.
# BaseHTTPRequestHandler defaults to None — wait forever — which is what let
# a single half-open socket park a handler in rfile.readline() indefinitely.
_HANDLER_TIMEOUT = 10


class _HealthServer(ThreadingHTTPServer):
    """Threaded on purpose (D045).

    A serial HTTPServer handles one connection at a time, so any client that
    connects and never finishes a request — a port scanner, a stalled proxy,
    a half-open socket left by a dropped network — blocks /health and /ready
    for every probe behind it. Under BOBI_HEALTH_BIND=0.0.0.0, where the
    endpoint is reachable by anything on the network, that turns a healthy
    manager into one the supervisor diagnoses as wedged and restarts.

    Daemon threads so a stalled handler can never hold up interpreter exit or
    ``stop()``; the handler timeout bounds how long one can linger at all.
    """

    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        # HTTPServer's implementation performs a synchronous reverse-DNS
        # lookup after binding. Health startup must not depend on DNS: the
        # manager cannot publish its port or continue booting until this
        # constructor returns.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _make_handler(manager_pid: int, project_name: str,
                  session_status_fn, manager_block_fn,
                  manager_session: str | None = None, token: str = ""):
    """Build the request handler class with closed-over manager state."""

    class HealthHandler(BaseHTTPRequestHandler):

        # Applied to the connection socket by StreamRequestHandler.setup(),
        # so a client that never sends a request line is dropped instead of
        # holding its thread forever.
        timeout = _HANDLER_TIMEOUT

        def do_GET(self):
            if self.path == "/health":
                self._json_response(200, self._health_body())
            elif self.path == "/ready":
                body = self._ready_body()
                code = 200 if body["status"] == "ready" else 503
                self._json_response(code, body)
            elif self.path == "/subscriptions":
                if not self._authorized():
                    return
                self._subscriptions_read()
            else:
                self._json_response(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/subscriptions/reload":
                self._json_response(404, {"error": "not found"})
                return
            if not self._authorized():
                return
            self._subscriptions_reload()

        def _authorized(self) -> bool:
            """Token gate for the mutating and introspection routes.

            /health and /ready stay unauthenticated so Kubernetes and Fly
            probes are unaffected. These two do not: BOBI_HEALTH_BIND=0.0.0.0
            ships in the reference image, and an unauthenticated reload would
            let anything on the network trigger credential-bearing authorize
            POSTs and state-changing subscription writes.
            """
            import hmac

            presented = self.headers.get(RELOAD_TOKEN_HEADER) or ""
            if token and hmac.compare_digest(presented, token):
                return True
            self._json_response(401, {"error": "bad or missing token"})
            return False

        def _live_subscription(self):
            from bobi.subagent import get_live_subscription

            if not manager_session:
                return None
            return get_live_subscription(manager_session)

        def _subscriptions_read(self):
            controller = self._live_subscription()
            if controller is None:
                self._json_response(409, {
                    "status": "no_live_subscription",
                    "session": manager_session or "",
                })
                return
            self._json_response(200, controller.accepted_record())

        def _subscriptions_reload(self):
            controller = self._live_subscription()
            if controller is None:
                self._json_response(409, {
                    "status": "no_live_subscription",
                    "session": manager_session or "",
                })
                return
            try:
                body = controller.reload(authorize=True)
            except Exception as e:
                log.warning("Subscription reload failed: %s", e, exc_info=True)
                self._json_response(503, {
                    "status": "error", "error": str(e),
                })
                return
            self._json_response(200, body)

        def _health_body(self):
            body = {
                "status": "ok",
                "pid": manager_pid,
                "project": project_name,
            }
            # The entry-point (director) session's progress signal — the
            # input the supervisor sidecar needs to tell a wedged director apart
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

    Server-side derivation of ``idle_seconds`` keeps the supervisor dumb (no
    clock-skew handling). Returns None when no manager session is wired so the
    payload stays backward-compatible. Fails open: a missing entry (the
    pre-spawn window) reports ``status="starting"`` with ``idle_seconds=0`` so
    the supervisor never restarts a manager that has not finished booting.
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
            "inbox": {"depth": 0, "oldest_age_seconds": 0.0},
        }
    return {
        "session": entry.name,
        "status": entry.status,
        "last_activity": entry.last_activity,
        "idle_seconds": max(0.0, time.time() - entry.last_activity),
        "inbox": _inbox_status(entry),
        "error": getattr(entry, "error", "") or None,
        "terminal_at": getattr(entry, "terminal_at", 0.0) or None,
    }


def _inbox_status(entry) -> dict:
    """Return live queue telemetry when this process owns the inbox."""
    depth = getattr(entry, "inbox_depth", 0)
    oldest_age = getattr(entry, "inbox_oldest_age_seconds", 0.0)
    oldest_enqueued_at = getattr(entry, "inbox_oldest_enqueued_at", 0.0)
    if depth and oldest_enqueued_at:
        oldest_age = max(oldest_age, time.time() - oldest_enqueued_at)
    try:
        from bobi.inbox import get_local_inbox
        inbox = get_local_inbox(entry.name)
        if inbox is not None:
            depth = inbox.depth()
            oldest_age = inbox.oldest_age()
    except Exception:
        pass
    return {
        "depth": max(0, int(depth)),
        "oldest_age_seconds": round(max(0.0, float(oldest_age)), 1),
    }


def _session_status_from_registry():
    """Pull live session info from the on-disk registry (best-effort)."""
    try:
        from bobi.sdk import get_registry
        registry = get_registry()
        active = registry.list_active()
        sessions = []
        for entry in active:
            session = {
                "name": entry.name, "role": entry.role,
                "status": entry.status, "inbox": _inbox_status(entry),
            }
            if isinstance(entry.ack_watermark, dict) and entry.ack_watermark:
                watermark = dict(entry.ack_watermark)
                pending_at = watermark.pop("oldest_pending_at", 0.0)
                watermark["oldest_age_seconds"] = (
                    max(0.0, time.time() - pending_at)
                    if watermark.get("pending_batches") and
                    isinstance(pending_at, (int, float)) else 0.0)
                session["ack_watermark"] = watermark
            sessions.append(session)
        return sessions
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


def _mint_token(path: Path) -> str:
    """Mint the reload token, created at mode 0600 rather than chmod'd after.

    Deliberately NOT `webui_common.launcher.write_secret`: that writes the file
    and chmods it afterwards, so on a 022 umask the secret is observable at 0644
    in between, and permanently so if the process dies there. For the local UIs
    the loopback Host guard is the primary boundary; here the token IS the
    boundary for a server that may bind 0.0.0.0. `events.state.save_bubble_state`
    is the house precedent for a secret whose confidentiality depends on its
    mode (CLAUDE.md names it as the one exception to the fsutil rule).
    """
    import secrets

    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unlink first: O_CREAT does not apply its mode to an EXISTING inode, so a
    # leftover world-readable file from an older version would keep its mode.
    path.unlink(missing_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    return token


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
    progress signal the self-heal supervisor sidecar observes. ``manager_status_fn``
    overrides the default registry lookup (used by tests).

    It also names the session that owns the event-server deployment
    ``POST /subscriptions/reload`` re-applies (#952). That route and
    ``GET /subscriptions`` require the token minted here into
    ``state/manager-health.token``; ``/health`` and ``/ready`` do not.
    """
    global _server, _thread, _port_file, _token_file

    if _server is not None:
        return _server.server_address[1]

    manager_pid = os.getpid()
    status_fn = session_status_fn or _session_status_from_registry
    if manager_status_fn is not None:
        manager_block_fn = manager_status_fn
    else:
        manager_block_fn = lambda: _manager_block_from_registry(manager_session)

    token_file = state_dir / "manager-health.token"
    token = _mint_token(token_file)

    handler = _make_handler(manager_pid, project_name, status_fn,
                            manager_block_fn, manager_session, token)
    bind = _configured_bind()
    configured_port = _configured_port()
    try:
        _server = _HealthServer((bind, configured_port), handler)
    except BaseException:
        # A failed bind must not leave a usable token on disk for a server
        # that is not listening.
        token_file.unlink(missing_ok=True)
        raise
    port = _server.server_address[1]

    _token_file = token_file
    _port_file = state_dir / "manager-health.port"
    _port_file.write_text(str(port))

    _thread = threading.Thread(target=_server.serve_forever, daemon=True,
                               name="manager-health")
    _thread.start()

    log.info("Manager health server listening on %s:%d", bind, port)
    return port


def stop():
    """Shut down the health server and clean up the port and token files."""
    global _server, _thread, _port_file, _token_file

    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    _thread = None

    if _port_file is not None:
        _port_file.unlink(missing_ok=True)
        _port_file = None

    if _token_file is not None:
        _token_file.unlink(missing_ok=True)
        _token_file = None


def health(base_url: str, timeout: float = 2) -> dict | None:
    """Probe the manager health endpoint.  Returns the parsed payload or None."""
    from bobi import http as pooled

    try:
        resp = pooled.get(f"{base_url}/health", timeout=timeout)
        data = resp.json()
        return data if data.get("status") == "ok" else None
    except Exception:
        return None
