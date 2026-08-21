"""Tests for the web app daemon — token persistence, status, and a real
detached start/stop round-trip against a temp BOBI_HOME."""

import os
import stat

import pytest

from bobi.webapp import daemon


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    # Ephemeral port so parallel test runs never collide.
    monkeypatch.setenv("BOBI_APP_PORT", "0")
    return tmp_path / "home"


class TestToken:
    def test_minted_once_and_persisted(self, home):
        t1 = daemon.ensure_token()
        t2 = daemon.ensure_token()
        assert t1 == t2
        assert len(t1) > 20

    def test_token_file_is_private(self, home):
        daemon.ensure_token()
        mode = stat.S_IMODE(os.stat(home / "webapp" / "app.token").st_mode)
        assert mode == 0o600


class TestStatus:
    def test_not_running_when_no_state(self, home):
        st = daemon.status()
        assert st.running is False

    def test_not_running_with_stale_pid(self, home):
        (home / "webapp").mkdir(parents=True)
        (home / "webapp" / "app.pid").write_text("999999999")
        (home / "webapp" / "app.port").write_text("1")
        assert daemon.status().running is False


class TestLifecycle:
    def test_start_ping_stop_round_trip(self, home):
        st = daemon.start(open_browser=False)
        try:
            assert st.running is True
            assert st.port > 0
            assert st.url.startswith(f"http://127.0.0.1:{st.port}/?n=")
            # status() agrees while the daemon lives
            assert daemon.status().running is True
            # idempotent start reuses the running daemon
            again = daemon.start(open_browser=False)
            assert again.pid == st.pid
        finally:
            stopped = daemon.stop()
        assert stopped.running is False
        assert daemon.status().running is False

    def test_stop_when_not_running(self, home):
        st = daemon.stop()
        assert st.running is False


class TestStopIdentity:
    """D037 — a stale pidfile plus pid reuse made `bobi app stop` a weapon.

    run_foreground only cleans its pidfile on a graceful exit, so a crash
    leaves app.pid pointing at a dead pid. The OS reuses that pid for an
    unrelated process; stop() saw _pid_alive(pid) is True and went straight to
    SIGTERM then SIGKILL. status() has always guarded exactly this by
    requiring /api/ping before declaring the app running — stop() did not.
    """

    def _stale_state(self, home, pid: int):
        (home / "webapp").mkdir(parents=True, exist_ok=True)
        (home / "webapp" / "app.pid").write_text(str(pid))
        (home / "webapp" / "app.port").write_text("1")

    def test_an_unrelated_live_pid_is_never_signalled(self, home, monkeypatch):
        import subprocess
        import sys

        # A real innocent bystander: a sleeping python process that is
        # emphatically not the bobi app.
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self._stale_state(home, victim.pid)
            st = daemon.stop()

            assert st.running is False
            assert victim.poll() is None, "stop() killed an unrelated process"
            # The stale files are cleaned up, so the next start is unblocked.
            assert not (home / "webapp" / "app.pid").exists()
        finally:
            victim.kill()
            victim.wait(timeout=10)

    def test_force_signals_a_pid_that_cannot_be_verified(self, home, monkeypatch):
        """An app wedged past answering /api/ping must still be stoppable."""
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self._stale_state(home, proc.pid)
            daemon.stop(force=True)
            proc.wait(timeout=10)
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


EPERM_PID = 424242


def _refuse_signals_to(monkeypatch, pid: int) -> None:
    """Make `os.kill` raise EPERM for *pid* — a process owned by another uid."""
    real_kill = os.kill

    def fake_kill(target, sig):
        if target == pid:
            raise PermissionError(1, "Operation not permitted")
        return real_kill(target, sig)

    monkeypatch.setattr(os, "kill", fake_kill)


class TestForeignUidPid:
    """D051 — a pid we may not signal is alive, not dead.

    `daemon` kept its own `_pid_alive` that read EPERM from `os.kill(pid, 0)`
    as "no such process", while `bobi.sdk.pid_alive` — the copy the rest of
    the codebase delegates to — reads it as alive. Under a BOBI_HOME reachable
    by another uid (an app once started with sudo, a home mounted into a
    container) that divergence made `status()` report not-running for a live
    app, so the next `bobi app start` spawned a second daemon that could not
    bind the port and overwrote app.pid, orphaning the first.
    """

    def _state(self, home, pid: int) -> None:
        (home / "webapp").mkdir(parents=True, exist_ok=True)
        (home / "webapp" / "app.pid").write_text(str(pid))
        (home / "webapp" / "app.port").write_text("1")

    def test_status_reports_running_for_a_pid_it_may_not_signal(
            self, home, monkeypatch):
        self._state(home, EPERM_PID)
        _refuse_signals_to(monkeypatch, EPERM_PID)
        monkeypatch.setattr(daemon, "_ping", lambda port, token, timeout=1.0: True)

        st = daemon.status()

        assert st.running is True
        assert st.pid == EPERM_PID

    def test_stop_reports_the_refusal_and_keeps_the_state_files(
            self, home, monkeypatch):
        self._state(home, EPERM_PID)
        _refuse_signals_to(monkeypatch, EPERM_PID)
        monkeypatch.setattr(daemon, "_ping", lambda port, token, timeout=1.0: True)

        st = daemon.stop()

        assert st.not_permitted is True
        assert st.pid == EPERM_PID
        # The app is still running, so its state is not stale — clearing it
        # would let the next `start` spawn a second daemon against a live one.
        assert (home / "webapp" / "app.pid").exists()
        assert (home / "webapp" / "app.port").exists()


class TestPing:
    """Q123 — /api/ping goes through the pooled client, not raw urllib."""

    def test_ping_uses_the_shared_http_client(self, home, monkeypatch):
        seen = {}

        class Resp:
            def json(self):
                return {"ok": True}

        def fake_get(url, *, headers=None, timeout=None):
            seen.update(url=url, headers=headers, timeout=timeout)
            return Resp()

        monkeypatch.setattr(daemon.http, "get", fake_get)

        assert daemon._ping(1234, "tok") is True
        assert seen["url"] == "http://127.0.0.1:1234/api/ping"
        assert seen["headers"] == {"x-bobi-webui-token": "tok"}
        assert seen["timeout"] == 1.0

    def test_a_refused_connection_is_not_alive(self, home, monkeypatch):
        def boom(url, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr(daemon.http, "get", boom)
        assert daemon._ping(1234, "tok") is False

    def test_a_non_ok_body_is_not_alive(self, home, monkeypatch):
        """httpx returns a 401 rather than raising, where urlopen raised.

        The liveness answer has to come from the body either way, or a
        token-rejecting server would read as a healthy app.
        """
        class Denied:
            def json(self):
                return {"error": "bad or missing token"}

        monkeypatch.setattr(daemon.http, "get", lambda url, **kw: Denied())
        assert daemon._ping(1234, "tok") is False

    def test_a_body_that_is_not_json_is_not_alive(self, home, monkeypatch):
        class Garbage:
            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(daemon.http, "get", lambda url, **kw: Garbage())
        assert daemon._ping(1234, "tok") is False


class TestSharedLaunchContract:
    """D097/Q024 — run_foreground binds its own socket because the port is
    fixed and the pid/port files must land between bind and serve. Everything
    either side of that is the shared launcher contract, not a third copy."""

    def test_it_serves_through_the_shared_run_server(self, home, monkeypatch):
        from bobi.webui_common import launcher

        seen = {}

        def fake_run_server(app, sock):
            seen["app"] = app
            seen["sockname"] = sock.getsockname()
            # `start` polls for these in the parent while the child serves,
            # so they must already be on disk by the time serving begins.
            seen["port_file"] = (home / "webapp" / "app.port").read_text()
            seen["pid_file"] = (home / "webapp" / "app.pid").read_text()

        monkeypatch.setattr(launcher, "run_server", fake_run_server)

        assert daemon.run_foreground() == 0
        assert seen["sockname"][0] == "127.0.0.1"
        assert int(seen["port_file"]) == seen["sockname"][1]
        assert int(seen["pid_file"]) == os.getpid()
        # And the state is cleaned up when serving ends.
        assert not (home / "webapp" / "app.port").exists()
        assert not (home / "webapp" / "app.pid").exists()

    def test_it_binds_through_the_shared_socket_helper(self, home, monkeypatch):
        from bobi.webui_common import launcher

        def refuse(host, port):
            raise OSError("address already in use")

        monkeypatch.setattr(launcher, "serve_socket", refuse)

        # A taken port is an operator message and a non-zero exit, not a
        # traceback out of the daemon child.
        assert daemon.run_foreground() == 1

    def test_the_browser_opens_through_the_shared_helper(self, home, monkeypatch):
        from bobi.webui_common import launcher

        seen = {}
        monkeypatch.setattr(
            launcher, "open_browser_soon",
            lambda url, *, delay=0.5: seen.update(url=url, delay=delay))

        daemon._open_browser("http://127.0.0.1:1/?n=t")

        assert seen["url"] == "http://127.0.0.1:1/?n=t"
        assert seen["delay"] == 0.3
