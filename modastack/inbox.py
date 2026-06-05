"""Session inbox — in-memory queue with HTTP delivery.

Every session has an inbox: a queue fed by a tiny HTTP server on a
random port. Messages arrive via POST /inbox and are picked up by
the session's drain loop. Blocking (wait) mode holds the HTTP
connection open until the session responds or timeout.
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

log = logging.getLogger(__name__)


def _msg_id() -> str:
    ts = int(time.time() * 1000)
    return f"{ts:013x}-{secrets.token_hex(4)}"


@dataclass
class Message:
    id: str
    sender: str
    text: str
    wait: bool = False


class _PendingReply:
    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: str = ""
        self.error: str = ""


class _InboxHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/inbox":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, {"ok": False, "error": "invalid JSON"})
            return

        inbox: Inbox = self.server.inbox  # type: ignore[attr-defined]

        msg = Message(
            id=body.get("id", _msg_id()),
            sender=body.get("sender", ""),
            text=body.get("text", ""),
            wait=body.get("wait", False),
        )

        if not msg.text:
            self._json_response(400, {"ok": False, "error": "empty message"})
            return

        if msg.wait:
            pending = _PendingReply()
            inbox._pending[msg.id] = pending
            inbox._queue.put(msg)

            timeout = body.get("timeout", 300)
            if pending.event.wait(timeout=timeout):
                if pending.error:
                    self._json_response(500, {"ok": False, "error": pending.error})
                else:
                    self._json_response(200, {"ok": True, "response": pending.response})
            else:
                inbox._pending.pop(msg.id, None)
                self._json_response(408, {"ok": False, "error": f"no response within {timeout}s"})
        else:
            inbox._queue.put(msg)
            self._json_response(200, {"ok": True})

    def do_GET(self) -> None:
        if self.path == "/health":
            inbox: Inbox = self.server.inbox  # type: ignore[attr-defined]
            self._json_response(200, {"ok": True, "session": inbox.session_name})
        else:
            self.send_error(404)

    def _json_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Inbox:
    """In-memory message queue with an HTTP server for delivery."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self.port: int = 0
        self._queue: queue.SimpleQueue[Message] = queue.SimpleQueue()
        self._pending: dict[str, _PendingReply] = {}
        self._server: _ThreadedHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> int:
        """Start the HTTP server. Returns the assigned port."""
        if self._server:
            return self.port

        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), _InboxHandler)
        self._server.inbox = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"inbox-{self.session_name}",
        )
        self._server_thread.start()
        log.info(f"Inbox for '{self.session_name}' listening on port {self.port}")
        return self.port

    def recv(self, timeout: float = 2.0) -> Message | None:
        """Block until a message arrives. Returns None on timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def respond(self, msg_id: str, response: str) -> None:
        """Respond to a wait-mode message. Unblocks the sender's HTTP request."""
        pending = self._pending.pop(msg_id, None)
        if pending:
            pending.response = response
            pending.event.set()

    def close(self) -> None:
        """Shut down: unblock all pending asks, stop HTTP server."""
        self._closed = True
        for pending in self._pending.values():
            pending.error = "session closed"
            pending.event.set()
        self._pending.clear()

        if self._server:
            self._server.shutdown()
            self._server = None
        if self._server_thread:
            self._server_thread.join(timeout=5)
            self._server_thread = None


def deliver(
    to: str,
    text: str,
    sender: str = "",
    wait: bool = False,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Deliver a message to a session by name.

    Looks up the target's inbox port from the session registry and
    POSTs to its HTTP server. If wait=True, blocks until the session
    responds or timeout.

    Returns (success, response_text).
    """
    from modastack.sdk import get_registry, _pid_alive

    entry = get_registry().get(to)
    if not entry:
        return False, f"session '{to}' not found"

    if not entry.inbox_port:
        return False, f"session '{to}' has no inbox"

    if entry.pid and not _pid_alive(entry.pid):
        return False, f"session '{to}' process is dead"

    payload = json.dumps({
        "id": _msg_id(),
        "sender": sender,
        "text": text,
        "wait": wait,
        "timeout": timeout,
    }).encode()

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{entry.inbox_port}/inbox",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        http_timeout = timeout + 10 if wait else 10
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                return True, result.get("response", "")
            return False, result.get("error", "unknown error")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return False, body.get("error", f"HTTP {e.code}")
        except Exception:
            return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"cannot reach session '{to}': {e}"
    except TimeoutError:
        return False, f"no response within {timeout}s"
