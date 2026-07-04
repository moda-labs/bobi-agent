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
