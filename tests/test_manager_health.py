"""Unit tests for bobi.manager_health — the manager health endpoint."""

import json
import os
import socket
import urllib.error
import urllib.request

import pytest

from bobi import manager_health
from bobi.inbox import Inbox, Message


@pytest.fixture(autouse=True)
def _reset_server():
    """Ensure the health server is stopped and module state is clean."""
    manager_health.stop()
    manager_health._server = None
    manager_health._thread = None
    manager_health._port_file = None
    manager_health._token_file = None
    yield
    manager_health.stop()


class TestHealthServer:
    @pytest.mark.parametrize("watermark", ["invalid", {
        "pending_batches": 1, "oldest_pending_at": "invalid"}])
    def test_malformed_watermark_does_not_hide_session_health(self,
                                                           tmp_path,
                                                           monkeypatch,
                                                           watermark):
        from bobi import sdk

        registry = sdk.SessionRegistry(root=tmp_path)
        registry.register(sdk.SessionEntry(
            name="director", pid=os.getpid(), status="idle",
            ack_watermark=watermark))
        monkeypatch.setattr(sdk, "get_registry", lambda: registry)
        assert manager_health._session_status_from_registry()[0]["name"] == "director"

    def test_health_reads_watermark_from_persisted_session_state(self,
                                                               tmp_path,
                                                               monkeypatch):
        from bobi import sdk

        registry = sdk.SessionRegistry(root=tmp_path)
        registry.register(sdk.SessionEntry(
            name="director", pid=os.getpid(), status="running",
            last_activity=10.0))
        state_path = registry._state_path("director")
        state = json.loads(state_path.read_text())
        state["ack_watermark"] = {
            "pinned_seq": 223, "pending_batches": 98,
            "oldest_pending_at": 100.0,
            "oldest_event_type": "agent/session.completed"}
        state_path.write_text(json.dumps(state))
        monkeypatch.setattr(sdk, "get_registry", lambda: registry)
        monkeypatch.setattr(manager_health.time, "time", lambda: 450.0)
        registry.update_inbox_stats("director", depth=3, oldest_age=25.0)
        port = manager_health.start(tmp_path / "state", "test-project")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=2) as response:
            data = json.loads(response.read())
        assert data["sessions"][0]["ack_watermark"] == {
            "pinned_seq": 223, "pending_batches": 98,
            "oldest_event_type": "agent/session.completed",
            "oldest_age_seconds": 350.0}
        assert data["sessions"][0]["inbox"] == {
            "depth": 3, "oldest_age_seconds": 25.0}
        assert registry.get("director").last_activity == 10.0

    def test_start_does_not_wait_for_reverse_dns(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        def fail_reverse_dns(_host):
            raise AssertionError("health startup must not resolve its bind host")

        monkeypatch.setattr(socket, "getfqdn", fail_reverse_dns)

        port = manager_health.start(state_dir, "test-project")

        assert port > 0
        assert manager_health._server.server_name == "127.0.0.1"

    def test_start_returns_port_and_writes_port_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project")
        assert isinstance(port, int)
        assert port > 0

        port_file = state_dir / "manager-health.port"
        assert port_file.exists()
        assert int(port_file.read_text().strip()) == port

    def test_start_uses_configured_bind_and_port(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        configured_port = sock.getsockname()[1]
        sock.close()

        monkeypatch.setenv("BOBI_HEALTH_BIND", "0.0.0.0")
        monkeypatch.setenv("BOBI_HEALTH_PORT", str(configured_port))
        port = manager_health.start(state_dir, "test-project")

        assert manager_health._server.server_address[0] == "0.0.0.0"
        assert port == configured_port
        assert int((state_dir / "manager-health.port").read_text()) == port
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=2) as resp:
            data = json.loads(resp.read())
        assert data["status"] == "ok"

    def test_fixed_port_can_restart_after_stop(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        configured_port = sock.getsockname()[1]
        sock.close()

        monkeypatch.setenv("BOBI_HEALTH_PORT", str(configured_port))
        assert manager_health.start(state_dir, "test-project") == configured_port
        manager_health.stop()
        assert manager_health.start(state_dir, "test-project") == configured_port

    @pytest.mark.parametrize("value", ["not-a-port", "-1", "65536"])
    def test_invalid_configured_port_fails_loudly(self, tmp_path, monkeypatch,
                                                  value):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setenv("BOBI_HEALTH_PORT", value)

        with pytest.raises(ValueError, match="BOBI_HEALTH_PORT"):
            manager_health.start(state_dir, "test-project")

    def test_health_endpoint_returns_ok(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project",
                                    session_status_fn=lambda: [])

        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())

        assert data["status"] == "ok"
        assert data["project"] == "test-project"
        assert data["pid"] > 0
        assert isinstance(data["sessions"], list)

    def test_health_endpoint_includes_session_status(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        fake_sessions = [
            {"name": "moda-mgr-repo", "role": "manager", "status": "running"},
            {"name": "eng-42", "role": "engineer", "status": "idle"},
        ]
        port = manager_health.start(state_dir, "my-project",
                                    session_status_fn=lambda: fake_sessions)

        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())

        assert len(data["sessions"]) == 2
        assert data["sessions"][0]["name"] == "moda-mgr-repo"
        assert data["sessions"][1]["role"] == "engineer"

    def test_non_health_path_returns_404(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project",
                                    session_status_fn=lambda: [])

        url = f"http://127.0.0.1:{port}/nonexistent"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=2)
        assert exc_info.value.code == 404

    def test_stop_cleans_up_port_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        manager_health.start(state_dir, "test-project",
                             session_status_fn=lambda: [])
        port_file = state_dir / "manager-health.port"
        assert port_file.exists()

        manager_health.stop()
        assert not port_file.exists()

    def test_start_is_idempotent(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port1 = manager_health.start(state_dir, "test-project",
                                     session_status_fn=lambda: [])
        port2 = manager_health.start(state_dir, "test-project",
                                     session_status_fn=lambda: [])
        assert port1 == port2

    def test_health_probe_function(self, tmp_path):
        """Test the health() client function against a running server."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project",
                                    session_status_fn=lambda: [])

        data = manager_health.health(f"http://127.0.0.1:{port}")
        assert data is not None
        assert data["status"] == "ok"

    def test_health_probe_returns_none_on_bad_port(self):
        """health() returns None when nothing is listening."""
        data = manager_health.health("http://127.0.0.1:1", timeout=0.5)
        assert data is None

    def test_a_stalled_client_does_not_block_the_next_probe(self, tmp_path):
        """D045 — one half-open connection must not wedge the endpoint.

        With BOBI_HEALTH_BIND=0.0.0.0 for orchestrator probes, anything that
        connects and never completes a request (port scanner, stalled proxy)
        parks the handler in rfile.readline(). On a serial HTTPServer every
        later probe queues behind it, so the supervisor times out and
        restarts a perfectly healthy manager.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project",
                                    session_status_fn=lambda: [])

        stalled = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            # Connected, and deliberately silent — no request line ever sent.
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                data = json.loads(resp.read())
            assert data["status"] == "ok"
        finally:
            stalled.close()

    def test_the_handler_gives_up_on_a_silent_connection(self, tmp_path):
        """Concurrency alone would leak a thread per stalled connection."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        manager_health.start(state_dir, "test-project",
                             session_status_fn=lambda: [])

        timeout = manager_health._server.RequestHandlerClass.timeout
        assert timeout is not None and 0 < timeout <= 30

    def test_ready_returns_503_until_manager_running_or_idle(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        manager = {"session": "moda-mgr-p", "status": "starting",
                   "last_activity": None, "idle_seconds": 0.0}

        port = manager_health.start(
            state_dir, "test-project", session_status_fn=lambda: [],
            manager_status_fn=lambda: manager)

        url = f"http://127.0.0.1:{port}/ready"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=2)
        assert exc_info.value.code == 503

        manager["status"] = "running"
        with urllib.request.urlopen(url, timeout=2) as resp:
            running = json.loads(resp.read())
        assert resp.status == 200
        assert running["status"] == "ready"

        manager["status"] = "idle"
        with urllib.request.urlopen(url, timeout=2) as resp:
            idle = json.loads(resp.read())
        assert resp.status == 200
        assert idle["manager"]["status"] == "idle"

    def test_ready_returns_503_without_manager_signal(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project",
                                    session_status_fn=lambda: [])

        url = f"http://127.0.0.1:{port}/ready"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=2)
        assert exc_info.value.code == 503


class TestManagerBlock:
    """The #464 `manager` block: the director's progress signal."""

    def _get(self, port):
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read())

    def test_no_manager_block_when_session_not_wired(self, tmp_path):
        """Backward compatible: omit the block entirely (old shape)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        port = manager_health.start(state_dir, "p",
                                    session_status_fn=lambda: [])
        data = self._get(port)
        assert "manager" not in data
        assert isinstance(data["sessions"], list)  # unchanged

    def test_manager_block_present_with_derived_idle_seconds(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        block = {"session": "moda-mgr-p", "status": "running",
                 "last_activity": 1000.0, "idle_seconds": 742.0}
        port = manager_health.start(
            state_dir, "p", session_status_fn=lambda: [],
            manager_status_fn=lambda: block)
        data = self._get(port)
        assert data["manager"]["session"] == "moda-mgr-p"
        assert data["manager"]["status"] == "running"
        assert data["manager"]["idle_seconds"] == 742.0

    def test_missing_entry_guard_reports_starting(self):
        """Pre-spawn window: a missing registry entry fails open to
        status=starting / idle_seconds=0 so the supervisor never restarts a
        booting manager."""
        block = manager_health._manager_block_from_registry("no-such-session")
        # No registry/root bound here -> the lookup returns None internally and
        # the block degrades to the booting guard or None; either way it is
        # never an active wedge signal.
        if block is not None:
            assert block["status"] == "starting"
            assert block["idle_seconds"] == 0.0

    def test_idle_seconds_derived_from_registry_entry(self, tmp_path, monkeypatch):
        """The block derives idle_seconds = now - last_activity server-side."""
        from bobi import sdk

        class _Entry:
            name = "moda-mgr-p"
            status = "running"
            last_activity = 100.0

        class _Reg:
            def get(self, name):
                return _Entry()

        monkeypatch.setattr(sdk, "get_registry", lambda: _Reg())
        monkeypatch.setattr(manager_health.time, "time", lambda: 700.0)
        block = manager_health._manager_block_from_registry("moda-mgr-p")
        assert block["status"] == "running"
        assert block["last_activity"] == 100.0
        assert block["idle_seconds"] == 600.0

    def test_manager_block_reports_live_inbox_depth_and_age(self, monkeypatch):
        from bobi import sdk

        class _Entry:
            name = "moda-mgr-p"
            status = "idle"
            last_activity = 100.0
            inbox_depth = 0
            inbox_oldest_age_seconds = 0.0
            inbox_oldest_enqueued_at = 0.0

        class _Reg:
            def get(self, name):
                return _Entry()

        inbox = Inbox("moda-mgr-p")
        inbox.start()
        msg = Message(id="one", sender="event-bus", text="queued")
        inbox.push(msg)
        monkeypatch.setattr("bobi.inbox.time.monotonic",
                            lambda: msg.enqueued_at + 12.4)
        monkeypatch.setattr(sdk, "get_registry", lambda: _Reg())
        monkeypatch.setattr(manager_health.time, "time", lambda: 700.0)
        try:
            block = manager_health._manager_block_from_registry("moda-mgr-p")
        finally:
            inbox.close()

        assert block["inbox"] == {
            "depth": 1,
            "oldest_age_seconds": 12.4,
        }

    def test_manager_block_surfaces_persisted_auth_failure(self, monkeypatch):
        from bobi import sdk

        class _Entry:
            name = "moda-mgr-p"
            status = "error"
            last_activity = 100.0
            error = "brain authentication failed"
            terminal_at = 120.0

        class _Reg:
            def get(self, name):
                return _Entry()

        monkeypatch.setattr(sdk, "get_registry", lambda: _Reg())
        monkeypatch.setattr(manager_health.time, "time", lambda: 700.0)

        block = manager_health._manager_block_from_registry("moda-mgr-p")

        assert block["status"] == "error"
        assert block["error"] == "brain authentication failed"
        assert block["terminal_at"] == 120.0


class TestSubscriptionRoutes:
    """`POST /subscriptions/reload` and `GET /subscriptions` (#952).

    Both are token-gated. `BOBI_HEALTH_BIND=0.0.0.0` ships in the reference
    image, so an unauthenticated mutating route would let anything on the
    network trigger credential-bearing authorize POSTs and state-changing
    subscription writes. `/health` and `/ready` stay open so Kubernetes and Fly
    probes are unaffected.
    """

    @staticmethod
    def _start(tmp_path, session="mgr"):
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        port = manager_health.start(state, "test-project",
                                    session_status_fn=lambda: [],
                                    manager_session=session)
        token = (state / "manager-health.token").read_text().strip()
        return port, token, state

    @staticmethod
    def _request(port, path, *, token=None, method="GET"):
        url = f"http://127.0.0.1:{port}{path}"
        req = urllib.request.Request(url, method=method)
        if token is not None:
            req.add_header(manager_health.RELOAD_TOKEN_HEADER, token)
        if method == "POST":
            req.data = b""
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    class _Controller:
        def __init__(self):
            self.calls = []

        def reload(self, *, authorize):
            self.calls.append(authorize)
            return {"status": "applied", "subscriptions": ["inbox/mgr"],
                    "added_topics": [], "removed_topics": [],
                    "at": "2026-10-10T18:22:04+00:00"}

        def accepted_record(self):
            return {"session": "mgr", "subscriptions": ["inbox/mgr"],
                    "at": "2026-10-10T18:22:04+00:00"}

    def test_reload_without_the_token_is_401_and_triggers_no_put(self, tmp_path,
                                                                 monkeypatch):
        from bobi import subagent

        controller = self._Controller()
        monkeypatch.setattr(subagent, "get_live_subscription",
                            lambda s: controller)
        port, token, _ = self._start(tmp_path)

        assert self._request(port, "/subscriptions/reload",
                             method="POST")[0] == 401
        assert self._request(port, "/subscriptions/reload", token="wrong",
                             method="POST")[0] == 401
        assert self._request(port, "/subscriptions")[0] == 401
        # The gate runs BEFORE the lookup, so nothing was applied.
        assert controller.calls == []

        status, body = self._request(port, "/subscriptions/reload",
                                     token=token, method="POST")
        assert status == 200
        assert body["status"] == "applied"
        # The operator-triggered path authorizes, so a newly declared global
        # topic has its grant written before the PUT.
        assert controller.calls == [True]

        status, body = self._request(port, "/subscriptions", token=token)
        assert status == 200
        assert body == controller.accepted_record()

    def test_health_and_ready_stay_unauthenticated(self, tmp_path):
        port, _token, _ = self._start(tmp_path)
        assert self._request(port, "/health")[0] == 200
        assert self._request(port, "/ready")[0] in (200, 503)

    def test_no_live_subscription_is_409_on_both_routes(self, tmp_path,
                                                        monkeypatch):
        from bobi import subagent

        monkeypatch.setattr(subagent, "get_live_subscription", lambda s: None)
        port, token, _ = self._start(tmp_path)
        status, body = self._request(port, "/subscriptions/reload",
                                     token=token, method="POST")
        assert status == 409
        assert body == {"status": "no_live_subscription", "session": "mgr"}
        assert self._request(port, "/subscriptions", token=token)[0] == 409

    def test_the_token_file_is_0600_and_unlinked_by_stop(self, tmp_path):
        import stat

        _port, token, state = self._start(tmp_path)
        token_file = state / "manager-health.token"
        assert token
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        manager_health.stop()
        assert not token_file.exists()
        assert not (state / "manager-health.port").exists()

    def test_an_unknown_post_path_is_404(self, tmp_path):
        port, token, _ = self._start(tmp_path)
        assert self._request(port, "/nope", token=token, method="POST")[0] == 404

    def test_the_token_is_never_observable_at_a_wider_mode(self, tmp_path):
        """Codex review F2.

        `write_secret` writes the file and chmods it AFTERWARDS, so on a 022
        umask the secret is readable at 0644 in between and permanently so if
        the process dies there. Here the token IS the boundary for a server
        that may bind 0.0.0.0, so it is created at 0600.
        """
        import stat

        state = tmp_path / "state"
        state.mkdir(parents=True)
        path = state / "manager-health.token"

        observed = []
        real_write = os.write

        def _watch(fd, data):
            # Mode at the instant the bytes land, not after the call returns.
            try:
                observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
            except OSError:
                pass
            return real_write(fd, data)

        prior = os.umask(0o022)
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(os, "write", _watch)
                token = manager_health._mint_token(path)
        finally:
            os.umask(prior)

        assert token
        assert observed == [0o600]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_minting_replaces_a_leftover_world_readable_token(self, tmp_path):
        """O_CREAT does not apply its mode to an EXISTING inode, so a file left
        at 0644 by an older version would keep that mode."""
        import stat

        state = tmp_path / "state"
        state.mkdir(parents=True)
        path = state / "manager-health.token"
        path.write_text("stale")
        os.chmod(path, 0o644)

        token = manager_health._mint_token(path)
        assert token != "stale"
        assert path.read_text() == token
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
