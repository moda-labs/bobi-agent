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
    from bobi import paths
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


def _log_event(event: dict, session_id: str = "",
               state_dir: Path | None = None) -> None:
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
    path = (state_dir / filename) if state_dir is not None else _state_path(filename)
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

    # Channel-agnostic reply address (#618) - the agent echoes this back
    # verbatim via `bobi reply <conversation>`.
    conversation = event.get("conversation", "")
    if conversation:
        lines.append(f"  conversation: {conversation}")

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
                 queue: SimpleQueue | None = None,
                 on_deaf_reconnect: callable = None):
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
        # Receive-side liveness (#425). The transport ping (run_forever's
        # ping_interval) only proves the SOCKET is alive — on Cloudflare a
        # hibernated WebSocket is kept warm by the edge and answers protocol
        # pings even when the Durable Object behind it has stopped delivering
        # events. The result is a "deaf manager": the connected frame arrives
        # (so the session logs "ready"), next_seq freezes, and no events are
        # ever queued, until a manual --fresh restart. The app-level heartbeat
        # below round-trips a ping through the SERVER (which replies "pong"); if
        # pongs stop while the socket still reports connected, the receive path
        # is deaf and we force a reconnect to self-heal.
        self._last_pong_at: float | None = None
        self._deaf_reconnects = 0
        # Set after a deaf-triggered reconnect so the next "connected" frame can
        # re-assert subscriptions (covers a stale server-side subscription index
        # in addition to a zombie socket).
        self._needs_resubscribe = False
        self._on_deaf_reconnect = on_deaf_reconnect
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
        # Highest seq already enqueued in THIS process. With ACK-after-
        # processing (#688) the saved cursor can trail the delivery point by
        # the whole inbox backlog, so reconnecting with the cursor alone
        # would make every routine CF cycle replay events that are still
        # queued in memory - duplicate turns and duplicate chat replies.
        # In-process events don't need replay (the queue survives a
        # reconnect); only a process restart does, and there this floor
        # starts back at 0 so the cursor drives a full replay.
        self._max_enqueued_seq = 0

    # A connection that stayed up at least this long before ending is treated
    # as a routine CF cycle, not instability.
    _STABLE_AFTER_S = 30.0
    # This many short-lived connections in a row flips logging to a warning.
    _FLAP_WARN_STREAK = 5
    # App-level heartbeat cadence. A ping is sent every interval; if no pong has
    # come back within the timeout (~3 missed beats) while the socket still
    # claims connected, the receive path is deaf and we force a reconnect.
    _HEARTBEAT_INTERVAL_S = 30.0
    _HEARTBEAT_TIMEOUT_S = 95.0

    def _handle_pong(self) -> None:
        """Record a pong — proof the receive path round-tripped the server."""
        self._last_pong_at = time.monotonic()

    def seconds_since_pong(self) -> float | None:
        """Seconds since the last pong, or None if none seen yet."""
        if self._last_pong_at is None:
            return None
        return time.monotonic() - self._last_pong_at

    def is_live(self) -> bool:
        """True when the receive path is verified live, not merely connected.

        Distinct from ``wait_connected`` (which only proves the connect frame
        arrived): a deaf-but-connected socket reports connected forever, so
        "ready" overstated health (#425). Liveness requires a recent pong.
        """
        since = self.seconds_since_pong()
        return (self._connected.is_set() and since is not None
                and since <= self._HEARTBEAT_TIMEOUT_S)

    def _heartbeat(self, ws: "websocket.WebSocketApp",
                   hb_stop: threading.Event) -> None:
        """Per-connection watchdog: ping the server, force-reconnect if deaf.

        Runs for the life of one connection. ``hb_stop`` is set when that
        connection closes, so the loop never outlives its socket.
        """
        while not hb_stop.wait(self._HEARTBEAT_INTERVAL_S):
            if self._stop.is_set():
                return
            since = self.seconds_since_pong()
            if since is not None and since > self._HEARTBEAT_TIMEOUT_S:
                self._deaf_reconnects += 1
                self._connected.clear()
                self._needs_resubscribe = True
                log.warning(
                    "Event client deaf: no pong in %.0fs though the socket "
                    "reports connected — forcing reconnect (deaf #%d)",
                    since, self._deaf_reconnects,
                )
                try:
                    ws.close()
                except Exception:
                    pass
                return
            try:
                ws.send(json.dumps({"type": "ping"}))
            except Exception:
                return  # socket already broken; the run loop will reconnect

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

    def _connect(self) -> None:
        last_seen = max(_load_cursor(self.cursor_path), self._max_enqueued_seq)
        ws_url = (
            f"{self.server_url.replace('https://', 'wss://').replace('http://', 'ws://')}"
            f"/deployments/{self.deployment_id}/subscribe?last_seen={last_seen}"
        )
        # One watchdog per connection; closing the socket sets this so the
        # heartbeat thread never outlives its socket.
        hb_stop = threading.Event()

        def on_open(ws):
            # Baseline the pong clock so the watchdog has a reference before the
            # first beat completes, then run the heartbeat for this connection.
            self._last_pong_at = time.monotonic()
            threading.Thread(target=self._heartbeat, args=(ws, hb_stop),
                             daemon=True, name="event-client-hb").start()

        def on_message(ws, message):
            try:
                msg = json.loads(message)
            except json.JSONDecodeError:
                return

            msg_type = msg.get("type")

            if msg_type == "connected":
                self._last_connected_at = time.monotonic()
                self._reconnect_delay = 1
                # Treat the connect frame as a fresh liveness proof so the
                # watchdog doesn't immediately fire on a just-opened socket.
                self._last_pong_at = time.monotonic()
                # First connect of the process is worth surfacing; routine
                # reconnects after CF cycling are not, so they go to debug.
                if not self._ever_connected:
                    log.info(f"Event client connected (next_seq: {msg.get('next_seq')})")
                else:
                    log.debug(f"Event client reconnected (next_seq: {msg.get('next_seq')})")
                self._ever_connected = True
                self._connected.set()
                # After a deaf reconnect, re-assert subscriptions in case the
                # server-side subscription index went stale (e.g. eviction
                # during a long redeploy gap), not just the socket.
                if self._needs_resubscribe:
                    self._needs_resubscribe = False
                    cb = self._on_deaf_reconnect
                    if cb:
                        threading.Thread(
                            target=self._safe_resubscribe, args=(cb,),
                            daemon=True, name="event-client-resub").start()
                return

            if msg_type == "pong":
                self._handle_pong()
                return

            if msg_type in ("event", "replay"):
                data = msg.get("data", {})

                # An explicit cursor pins the whole client to that team's
                # state dir (the reply channel in an unbound multi-team host);
                # without one, the bound-root default applies.
                _log_event(data, session_id=self.deployment_id,
                           state_dir=self.cursor_path.parent
                           if self.cursor_path else None)
                seq = data.get("seq") or 0
                if seq > self._max_enqueued_seq:
                    self._max_enqueued_seq = seq
                self._queue.put(data)
                log.info(f"Event queued: {data.get('source', '?')}/{data.get('type', '?')}")

                if self.on_event:
                    self.on_event(data)

        def on_error(ws, error):
            # Per-error noise is demoted: the run loop owns stability-aware
            # logging (warns only on a sustained flap streak). Stash the error
            # so that warning can name the cause.
            self._last_ws_error = error
            log.debug(f"Event client WebSocket error: {error}")

        def on_close(ws, close_status, close_msg):
            hb_stop.set()  # stop this connection's heartbeat watchdog
            log.debug(f"Event client disconnected: {close_status} {close_msg}")

        self._ws = websocket.WebSocketApp(
            ws_url,
            header={"Authorization": f"Bearer {self.api_key}"},
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            self._ws.run_forever(ping_interval=30, ping_timeout=10,
                                 sslopt={"context": ssl_context})
        finally:
            # Belt-and-suspenders: ensure the watchdog stops even if on_close
            # never fired (e.g. run_forever raised before opening). _connected
            # is left as-is for routine reconnects (sub-second, lossless) so
            # subscribe-before-publish waiters aren't stalled; the deaf watchdog
            # clears it explicitly when the path is genuinely dead.
            hb_stop.set()

    def _safe_resubscribe(self, cb: callable) -> None:
        """Run the deaf-reconnect resubscribe hook, swallowing its errors.

        The hook re-asserts this deployment's subscriptions (and re-registers on
        failure) so a reconnect restores delivery even when the server-side
        subscription index — not just the socket — went stale.
        """
        try:
            cb()
        except Exception as e:
            log.debug("Resubscribe after deaf reconnect failed: %s", e)
