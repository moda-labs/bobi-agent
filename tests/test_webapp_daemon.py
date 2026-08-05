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
