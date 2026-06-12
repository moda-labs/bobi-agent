"""WebSocket client for the centralized event server.

Connects outbound to the event server (Cloudflare Worker or local),
receives events via WebSocket with automatic catch-up after downtime.
Pushes raw event envelopes to a thread-safe queue for the drain loop.
"""

import json
import logging
import ssl
import threading
import time
from pathlib import Path
from queue import SimpleQueue

import certifi
import websocket

log = logging.getLogger(__name__)

def _state_path(name: str) -> Path:
    from modastack import paths
    return paths.state_dir() / name

# Normalized events land here for the consumer to drain.
event_queue: SimpleQueue = SimpleQueue()


def _load_cursor(path: Path | None = None) -> int:
    path = path or _state_path("cursor.json")
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data.get("last_seen", 0)
    except (json.JSONDecodeError, OSError):
        pass
    return 0


def _save_cursor(seq: int, path: Path | None = None) -> None:
    path = path or _state_path("cursor.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_seen": seq}))


def _log_event(event: dict) -> None:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": event.get("type", ""),
        "source": event.get("source", ""),
        "payload": event.get("payload", event.get("data", {})),
    }
    path = _state_path("events.jsonl")
    prefix = ""
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                prefix = "\n"
    with open(path, "a") as f:
        f.write(prefix + json.dumps(entry) + "\n")




def format_event_for_manager(event: dict) -> str:
    """Format an event as a concise message for the consuming agent.

    v2 events carry ``text`` (human summary) and ``fields`` (flat scalar map).
    When ``fields`` is absent (e.g. unknown-source events posted via
    ``/events/{topic}``), the formatter falls back to rendering scalar values
    from ``payload`` directly (~200 chars per value, ~20 entries max).

    The ``requested_by`` pretty-printer is preserved for lifecycle events.
    """
    etype = event.get("type", "unknown")
    source = event.get("source", "")

    lines = [f"Event: {source}/{etype}"]

    # v2 text — the adapter's human-readable summary
    text = event.get("text", "")
    if text:
        lines.append(f"  {text}")

    # v2 fields — flat scalar map from the adapter
    fields = event.get("fields")
    if isinstance(fields, dict) and fields:
        for key, val in fields.items():
            if val is not None and val != "":
                lines.append(f"  {key}: {val}")
    elif not fields:
        # Scalar fallback — render payload scalars when fields is absent.
        # Caps: ~200 chars per value, ~20 entries.
        data = event.get("data", event.get("payload", {}))
        if isinstance(data, dict):
            count = 0
            for key, val in data.items():
                if count >= 20:
                    break
                if isinstance(val, (str, int, float, bool)):
                    s = str(val)
                    if len(s) > 200:
                        s = s[:200] + "..."
                    if s:
                        lines.append(f"  {key}: {s}")
                        count += 1

    # Lifecycle events carry requested_by in data/payload.
    # Check both data (internal events) and payload (server events).
    lifecycle_data = event.get("data", event.get("payload", {}))
    if isinstance(lifecycle_data, dict) and lifecycle_data.get("requested_by"):
        lines.append(f"  requested_by: {_format_requester(lifecycle_data['requested_by'])}")

    return "\n".join(lines)


def _format_requester(requester: dict) -> str:
    """Render a `requested_by` block into a one-line human-readable note.

    Gives the manager enough to route async results (e.g. a spawned-work
    completion) back to the originating Slack user and thread.
    """
    if not isinstance(requester, dict):
        return str(requester)
    name = requester.get("from") or requester.get("user_id") or "unknown"
    parts = [name]
    if requester.get("user_id") and requester.get("from"):
        parts.append(f"(user {requester['user_id']})")
    if requester.get("channel"):
        parts.append(f"in channel {requester['channel']}")
    if requester.get("thread_ts"):
        parts.append(f"thread {requester['thread_ts']}")
    return " ".join(parts)


class EventServerClient:
    """WebSocket client that connects to the centralized event server.

    Normalized events are pushed to `event_queue` for the consumer to drain.
    The WebSocket callback never blocks on inject or Slack replies.
    """

    def __init__(self, server_url: str, deployment_id: str, api_key: str,
                 on_event: callable = None, cursor_path: Path | None = None):
        self.server_url = server_url.rstrip("/")
        self.deployment_id = deployment_id
        self.api_key = api_key
        self.on_event = on_event
        # Seq numbers are per-deployment — sessions must not share a cursor.
        self.cursor_path = cursor_path
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reconnect_delay = 1

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="event-client")
        self._thread.start()
        log.info(f"Event client connecting to {self.server_url}")
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect()
            except Exception as e:
                log.warning(f"Event client error: {e}")

            if self._stop.is_set():
                break

            delay = min(self._reconnect_delay, 60)
            log.info(f"Event client reconnecting in {delay}s")
            self._stop.wait(timeout=delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _connect(self) -> None:
        last_seen = _load_cursor(self.cursor_path)
        ws_url = (
            f"{self.server_url.replace('https://', 'wss://').replace('http://', 'ws://')}"
            f"/deployments/{self.deployment_id}/subscribe?last_seen={last_seen}"
        )

        def on_message(ws, message):
            try:
                msg = json.loads(message)
            except json.JSONDecodeError:
                return

            msg_type = msg.get("type")

            if msg_type == "connected":
                log.info(f"Event client connected (next_seq: {msg.get('next_seq')})")
                self._reconnect_delay = 1
                return

            if msg_type == "pong":
                return

            if msg_type in ("event", "replay"):
                data = msg.get("data", {})
                seq = data.get("seq", 0)

                _log_event(data)
                event_queue.put(data)
                log.info(f"Event queued: {data.get('source', '?')}/{data.get('type', '?')}")

                if self.on_event:
                    self.on_event(data)

                if seq > 0:
                    _save_cursor(seq, self.cursor_path)
                    ws.send(json.dumps({"type": "ack", "seq": seq}))

        def on_error(ws, error):
            log.warning(f"Event client WebSocket error: {error}")

        def on_close(ws, close_status, close_msg):
            log.info(f"Event client disconnected: {close_status} {close_msg}")

        self._ws = websocket.WebSocketApp(
            ws_url,
            header={"Authorization": f"Bearer {self.api_key}"},
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._ws.run_forever(ping_interval=30, ping_timeout=10, sslopt={"context": ssl_context})
