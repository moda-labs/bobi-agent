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


def _log_event(event: dict, session_id: str = "") -> None:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": event.get("type", ""),
        "source": event.get("source", ""),
        "payload": event.get("payload", event.get("data", {})),
    }
    # Include seq and deployment_id for cross-session dedup when present.
    seq = event.get("seq")
    deployment_id = event.get("deployment_id")
    if seq is not None:
        entry["seq"] = seq
    if deployment_id is not None:
        entry["deployment_id"] = deployment_id

    filename = f"events-{session_id or 'default'}.jsonl"
    path = _state_path(filename)
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

    Normalized events are pushed to the client's queue for the consumer to
    drain. Each session/deployment uses its OWN queue (passed in) so that
    multiple clients in one process — e.g. sequential workflow phase sessions —
    never compete for events on a shared queue. Callers that don't pass one
    fall back to the process-global ``event_queue``. The WebSocket callback
    never blocks on inject or Slack replies.
    """

    def __init__(self, server_url: str, deployment_id: str, api_key: str,
                 on_event: callable = None, cursor_path: Path | None = None,
                 queue: SimpleQueue | None = None):
        self.server_url = server_url.rstrip("/")
        self.deployment_id = deployment_id
        self.api_key = api_key
        self.on_event = on_event
        self._queue = queue if queue is not None else event_queue
        # Seq numbers are per-deployment — sessions must not share a cursor.
        self.cursor_path = cursor_path
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Set when the server sends its "connected" frame, i.e. the subscription
        # is live and future matching events will be pushed. A fresh deployment
        # connects with last_seen=0 and the server only REPLAYS buffered events
        # when last_seen>0, so a publisher that needs subscribe-before-publish
        # (the reply channel in inbox.deliver) must wait on this before
        # publishing — otherwise an event sent during the connect window is
        # buffered but never replayed to this brand-new subscriber.
        self._connected = threading.Event()
        self._reconnect_delay = 1
        # Connection-stability tracking so routine reconnects stay quiet but
        # genuine flapping still surfaces. A Cloudflare Durable Object cycles
        # hibernating WebSockets from the runtime side every few minutes; the
        # client reconnects losslessly (replay via last_seen cursor), so a
        # long-lived connection that ends is NOT worth a warning. A connection
        # that never stays up IS. See investigate report (CF DO cycling).
        self._last_connected_at: float | None = None
        self._short_drop_streak = 0
        self._ever_connected = False
        self._last_ws_error: object = None
        # Highest seq number seen — used to drop duplicate events when a
        # stale WebSocket overlaps with a fresh one on the server (#322).
        # This happens routinely (Cloudflare WS cycling, process restarts),
        # not just on network blips.
        self._highest_seq = 0

    # A connection that stayed up at least this long before ending is treated
    # as a routine CF cycle, not instability.
    _STABLE_AFTER_S = 30.0
    # This many short-lived connections in a row flips logging to a warning.
    _FLAP_WARN_STREAK = 5

    def _record_disconnect(self, uptime: float | None) -> str:
        """Classify a just-ended connection and update flap-streak state.

        ``uptime`` is seconds the connection stayed up, or ``None`` if the
        "connected" frame never arrived. Returns the log category:
        ``"routine"`` (long-lived, CF cycled it), ``"flapping"`` (streak of
        short connections crossed the threshold), or ``"reconnecting"``
        (a short drop below the warn threshold).
        """
        if uptime is not None and uptime >= self._STABLE_AFTER_S:
            self._short_drop_streak = 0
            return "routine"
        self._short_drop_streak += 1
        if self._short_drop_streak >= self._FLAP_WARN_STREAK:
            return "flapping"
        return "reconnecting"

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="event-client")
        self._thread.start()
        log.info(f"Event client connecting to {self.server_url}")
        return self._thread

    def wait_connected(self, timeout: float) -> bool:
        """Block until the WS subscription is live. Returns False on timeout."""
        return self._connected.wait(timeout)

    def ack_through(self, seq: int) -> None:
        """Save cursor and ACK a seq number after delivery.

        Called by the drain loop AFTER an event batch has been delivered
        to the session inbox, so a crash before delivery never loses the
        event — the server will replay it on reconnect (#278 bug 2).
        """
        if seq <= 0:
            return
        _save_cursor(seq, self.cursor_path)
        ws = self._ws
        if ws:
            try:
                ws.send(json.dumps({"type": "ack", "seq": seq}))
            except Exception:
                pass  # next reconnect replays from saved cursor

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect()
            except Exception as e:
                # Couldn't even establish — counts as a short connection below.
                self._last_ws_error = e

            if self._stop.is_set():
                break

            uptime = None
            if self._last_connected_at is not None:
                uptime = time.monotonic() - self._last_connected_at
                self._last_connected_at = None

            category = self._record_disconnect(uptime)
            if category == "flapping":
                log.warning(
                    "Event client unstable: %d short-lived connections in a row "
                    "(last error: %s) — check event server / auth / network",
                    self._short_drop_streak, self._last_ws_error,
                )
            else:
                # Routine CF cycle or a single short blip — the reconnect below
                # recovers losslessly, so keep it at debug.
                up = f"{uptime:.0f}s" if uptime is not None else "n/a"
                log.debug("Event client reconnecting (%s, last uptime: %s)",
                          category, up)

            delay = min(self._reconnect_delay, 60)
            self._stop.wait(timeout=delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _handle_message(self, message: str) -> None:
        """Process a single WebSocket message (called from on_message callback).

        Extracted as a method so tests can call it directly without standing
        up a real WebSocket connection.
        """
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        if msg_type == "connected":
            self._last_connected_at = time.monotonic()
            self._reconnect_delay = 1
            if not self._ever_connected:
                log.info(f"Event client connected (next_seq: {msg.get('next_seq')})")
            else:
                log.debug(f"Event client reconnected (next_seq: {msg.get('next_seq')})")
            self._ever_connected = True
            self._connected.set()
            return

        if msg_type == "pong":
            return

        if msg_type in ("event", "replay"):
            data = msg.get("data", {})

            # Deduplicate: routine reconnections (CF cycling, restarts)
            # leave a stale + fresh WebSocket in the server's connection
            # set, causing the same seq to arrive twice. Drop the
            # duplicate before it reaches the queue so only one
            # placeholder is ever posted (#322).
            seq = data.get("seq", 0)
            if seq > 0 and seq <= self._highest_seq:
                log.debug("Dropping duplicate seq %d (highest seen: %d)",
                          seq, self._highest_seq)
                return
            if seq > self._highest_seq:
                self._highest_seq = seq

            _log_event(data, session_id=self.deployment_id)
            self._queue.put(data)
            log.info(f"Event queued: {data.get('source', '?')}/{data.get('type', '?')}")

            if self.on_event:
                self.on_event(data)

    def _connect(self) -> None:
        last_seen = _load_cursor(self.cursor_path)
        ws_url = (
            f"{self.server_url.replace('https://', 'wss://').replace('http://', 'ws://')}"
            f"/deployments/{self.deployment_id}/subscribe?last_seen={last_seen}"
        )

        def on_message(ws, message):
            self._handle_message(message)

        def on_error(ws, error):
            # Per-error noise is demoted: the run loop owns stability-aware
            # logging (warns only on a sustained flap streak). Stash the error
            # so that warning can name the cause.
            self._last_ws_error = error
            log.debug(f"Event client WebSocket error: {error}")

        def on_close(ws, close_status, close_msg):
            log.debug(f"Event client disconnected: {close_status} {close_msg}")

        self._ws = websocket.WebSocketApp(
            ws_url,
            header={"Authorization": f"Bearer {self.api_key}"},
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._ws.run_forever(ping_interval=30, ping_timeout=10, sslopt={"context": ssl_context})
