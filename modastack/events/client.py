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
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        raise RuntimeError("project root not set — call set_project_root() first")
    d = root / ".modastack" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / name

# Normalized events land here for the consumer to drain.
event_queue: SimpleQueue = SimpleQueue()


def _load_cursor() -> int:
    try:
        if _state_path("cursor.json").exists():
            data = json.loads(_state_path("cursor.json").read_text())
            return data.get("last_seen", 0)
    except (json.JSONDecodeError, OSError):
        pass
    return 0


def _save_cursor(seq: int) -> None:
    _state_path("cursor.json").write_text(json.dumps({"last_seen": seq}))


def _log_event(event: dict) -> None:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": event.get("type", ""),
        "source": event.get("source", ""),
        "payload": event.get("payload", event.get("data", {})),
    }
    with open(_state_path("events.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")




def format_event_for_manager(event: dict) -> str:
    """Format an event as a concise message for the consuming agent.

    Works with raw server events (type/source/payload at top level)
    and internal events (type/source/data at top level).
    """
    etype = event.get("type", "unknown")
    source = event.get("source", "")
    data = event.get("data", event.get("payload", {}))

    lines = [f"Event: {source}/{etype}"]

    for key in ("repo", "team_key", "workspace", "channel",
                "installation_id"):
        val = event.get(key)
        if val:
            lines.append(f"  {key}: {val}")

    if isinstance(data, dict):
        for key in ("issue_id", "pr_number", "title", "from", "user_id",
                     "state", "branch", "conclusion", "text", "ref",
                     "thread_ts", "phase", "duration", "summary", "error",
                     "action"):
            val = data.get(key)
            if val:
                lines.append(f"  {key}: {val}")
        if data.get("labels"):
            labels = data["labels"]
            if isinstance(labels, list):
                lines.append(f"  labels: {', '.join(str(l) for l in labels)}")
        if data.get("url") or data.get("pr_url") or data.get("html_url"):
            lines.append(f"  url: {data.get('url') or data.get('pr_url') or data.get('html_url')}")
        if data.get("requested_by"):
            lines.append(f"  requested_by: {_format_requester(data['requested_by'])}")

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


def _should_filter(event: dict) -> bool:
    """Drop events that don't match the repo's project filter (if configured)."""
    if event.get("source") != "linear":
        return False
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return False
    try:
        from modastack.config import ProjectConfig
        rc = ProjectConfig.from_file(root)
        if rc.linear_project:
            payload = event.get("payload", event.get("data", {}))
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            event_project = data.get("project", "")
            if isinstance(event_project, dict):
                event_project = event_project.get("name", "")
            if event_project and event_project != rc.linear_project:
                log.debug(f"Filtered Linear event: project={event_project}, want={rc.linear_project}")
                return True
    except Exception:
        pass
    return False




class EventServerClient:
    """WebSocket client that connects to the centralized event server.

    Normalized events are pushed to `event_queue` for the consumer to drain.
    The WebSocket callback never blocks on inject or Slack replies.
    """

    def __init__(self, server_url: str, deployment_id: str, api_key: str,
                 on_event: callable = None):
        self.server_url = server_url.rstrip("/")
        self.deployment_id = deployment_id
        self.api_key = api_key
        self.on_event = on_event
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
        last_seen = _load_cursor()
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

                if not _should_filter(data):
                    _log_event(data)
                    event_queue.put(data)
                    log.info(f"Event queued: {data.get('source', '?')}/{data.get('type', '?')}")

                    if self.on_event:
                        self.on_event(data)

                if seq > 0:
                    _save_cursor(seq)
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


def start_event_client(server_url: str, deployment_id: str, api_key: str,
                       on_event: callable = None) -> threading.Thread:
    client = EventServerClient(server_url, deployment_id, api_key, on_event=on_event)
    return client.start()
