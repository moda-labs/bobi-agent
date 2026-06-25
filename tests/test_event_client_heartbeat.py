"""Receive-side liveness regression tests (#425).

A *resumed* manager session could come up reporting ``connected`` (and so log
"Session ready") yet silently stop receiving inbox events — the "deaf manager".
The transport ping (``run_forever(ping_interval=...)``) only proves the SOCKET
is alive; on Cloudflare a hibernated WebSocket is kept warm by the edge and
answers protocol pings even when the Durable Object behind it has stopped
delivering events. ``next_seq`` freezes, no ``Event queued`` is ever logged,
and only a ``--fresh`` boot recovers.

The fix is an application-level heartbeat that round-trips through the server
(which replies ``{"type": "pong"}``): if pongs stop arriving while the socket
still reports connected, the client declares the receive path deaf and forces a
reconnect — self-healing without a manual restart.

These tests fake only the transport (``websocket.WebSocketApp``) and drive the
real client state machine — heartbeat watchdog, reconnect loop, and liveness
signal — end to end, with no external server dependency.
"""

import json
import threading
import time

import pytest

from modastack.events.client import EventServerClient


class FakeWebSocketApp:
    """Stand-in for ``websocket.WebSocketApp`` driven entirely in-process.

    Each instance is one TCP-less "connection". ``run_forever`` blocks (like the
    real reader loop) until ``close()`` is called, after delivering the server's
    ``connected`` frame. In ``healthy`` mode an inbound application ``ping`` is
    echoed back as a ``pong`` synchronously; in deaf mode pings are dropped on
    the floor — exactly the zombie-websocket failure mode from #425.
    """

    def __init__(self, url, header=None, on_message=None, on_open=None,
                 on_error=None, on_close=None, **kwargs):
        self.url = url
        self.header = header
        self.kwargs = kwargs
        self.on_message = on_message
        self.on_open = on_open
        self.on_error = on_error
        self.on_close = on_close
        self.healthy = True  # set by the factory before run_forever
        self._closed = threading.Event()

    def run_forever(self, **_kwargs):
        if self.on_open:
            self.on_open(self)
        # Server's connected frame — this is what makes the client log
        # "connected" / mark itself ready, the crux of the deaf-but-ready bug.
        if self.on_message:
            self.on_message(self, json.dumps({"type": "connected", "next_seq": 9}))
        self._closed.wait()
        if self.on_close:
            self.on_close(self, 1000, "closed")

    def send(self, message):
        data = json.loads(message)
        if data.get("type") == "ping" and self.healthy:
            # Healthy server echoes a pong through the app layer.
            if self.on_message:
                self.on_message(self, json.dumps({"type": "pong"}))
        # deaf: ping is silently dropped — no pong ever comes back.

    def close(self, *_args, **_kwargs):
        self._closed.set()


class _Factory:
    """Hands out FakeWebSocketApps and records every connection attempt.

    ``healthy_from`` lets a test make connection #1 deaf and later connections
    healthy, so we can assert the client *recovers* after detecting deafness.
    """

    def __init__(self, healthy_from=1):
        self.connections = []
        self.healthy_from = healthy_from
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        ws = FakeWebSocketApp(*args, **kwargs)
        with self._lock:
            self.connections.append(ws)
            ws.healthy = len(self.connections) >= self.healthy_from
        return ws

    @property
    def count(self):
        with self._lock:
            return len(self.connections)


def _fast_client(tmp_path, **kwargs):
    client = EventServerClient(
        server_url="http://localhost:9999",
        deployment_id="dep-1",
        api_key="key-1",
        cursor_path=tmp_path / "cursor.json",
        **kwargs,
    )
    # Shrink the heartbeat cadence so the watchdog fires in well under a second.
    client._HEARTBEAT_INTERVAL_S = 0.1
    client._HEARTBEAT_TIMEOUT_S = 0.3
    return client


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_client_sends_authorization_header_without_subprotocol(tmp_path, monkeypatch):
    factory = _Factory()
    monkeypatch.setattr("websocket.WebSocketApp", factory)

    client = _fast_client(tmp_path)
    try:
        client.start()
        assert _wait_until(lambda: factory.connections, timeout=1)
    finally:
        client.stop()

    assert factory.connections
    assert factory.connections[0].header == {
        "Authorization": "Bearer key-1",
    }
    assert "subprotocols" not in factory.connections[0].kwargs


class TestDeafManagerSelfHeal:
    """The headline #425 regression: a deaf-but-"connected" session recovers."""

    def test_deaf_connection_forces_reconnect(self, tmp_path, monkeypatch):
        # Connection #1 is deaf (never pongs); #2+ are healthy.
        factory = _Factory(healthy_from=2)
        monkeypatch.setattr("websocket.WebSocketApp", factory)

        client = _fast_client(tmp_path)
        # The deployment connected fine — without the heartbeat the client would
        # report itself live forever and never notice the dead receive path.
        client.start()
        try:
            assert client.wait_connected(timeout=2.0), "never got connected frame"

            # The watchdog must notice the missing pongs and reconnect.
            assert _wait_until(lambda: factory.count >= 2), (
                "deaf connection was never force-reconnected — client stayed "
                "silently deaf (the #425 bug)"
            )
            # After reconnecting to a healthy server the receive path is live.
            assert _wait_until(lambda: client.is_live()), (
                "client did not recover liveness after reconnect"
            )
            assert client._deaf_reconnects >= 1
        finally:
            client.stop()

    def test_deaf_connection_marks_itself_not_live(self, tmp_path, monkeypatch):
        # Stay deaf forever so we can observe the not-live window.
        factory = _Factory(healthy_from=10_000)
        monkeypatch.setattr("websocket.WebSocketApp", factory)

        client = _fast_client(tmp_path)
        client.start()
        try:
            assert client.wait_connected(timeout=2.0)
            # Even though the socket reported "connected", liveness must drop
            # once pongs stop — "ready" no longer overstates health.
            assert _wait_until(lambda: not client.is_live()), (
                "is_live() stayed True on a deaf connection"
            )
        finally:
            client.stop()


class TestHealthyConnectionStable:
    """A healthy server must not be force-reconnected by the heartbeat."""

    def test_healthy_connection_stays_up(self, tmp_path, monkeypatch):
        factory = _Factory(healthy_from=1)  # always healthy
        monkeypatch.setattr("websocket.WebSocketApp", factory)

        client = _fast_client(tmp_path)
        client.start()
        try:
            assert client.wait_connected(timeout=2.0)
            # Several heartbeat windows pass with pongs flowing.
            time.sleep(client._HEARTBEAT_TIMEOUT_S * 4)
            assert factory.count == 1, "healthy connection was needlessly reconnected"
            assert client.is_live()
            assert client._deaf_reconnects == 0
        finally:
            client.stop()


class TestHeartbeatWatchdogUnit:
    """Direct unit coverage of the watchdog decision, no sockets involved."""

    def _client(self, tmp_path):
        return EventServerClient(
            server_url="http://localhost:9999",
            deployment_id="dep-1",
            api_key="key-1",
            cursor_path=tmp_path / "cursor.json",
        )

    def test_stale_pong_closes_socket(self, tmp_path):
        from unittest.mock import MagicMock
        c = self._client(tmp_path)
        ws = MagicMock()
        hb_stop = threading.Event()
        c._HEARTBEAT_INTERVAL_S = 0.01
        c._HEARTBEAT_TIMEOUT_S = 0.05
        # Pong last seen far in the past → the path is deaf.
        c._last_pong_at = time.monotonic() - 10.0
        c._heartbeat(ws, hb_stop)
        ws.close.assert_called_once()
        assert c._deaf_reconnects == 1

    def test_fresh_pong_keeps_socket_and_pings(self, tmp_path):
        from unittest.mock import MagicMock
        c = self._client(tmp_path)
        ws = MagicMock()
        hb_stop = threading.Event()
        c._HEARTBEAT_INTERVAL_S = 0.01
        c._HEARTBEAT_TIMEOUT_S = 5.0
        c._last_pong_at = time.monotonic()

        # Stop the loop right after the first ping so the test is bounded.
        def stop_after_ping(msg):
            assert json.loads(msg) == {"type": "ping"}
            hb_stop.set()
        ws.send.side_effect = stop_after_ping

        c._heartbeat(ws, hb_stop)
        ws.close.assert_not_called()
        ws.send.assert_called_once()
        assert c._deaf_reconnects == 0

    def test_pong_message_refreshes_liveness(self, tmp_path):
        c = self._client(tmp_path)
        c._connected.set()
        c._last_pong_at = None
        assert not c.is_live()
        c._handle_pong()
        assert c.is_live()
        assert c.seconds_since_pong() is not None
