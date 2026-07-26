"""Tests for the web app daemon — token persistence, status, and a real
detached start/stop round-trip against a temp BOBI_HOME."""

import os
import stat
import subprocess
import sys
import time

import pytest

from bobi import service
from bobi.webapp import daemon


def _is_app(pid: int) -> bool:
    """What `stop()` asks before it signals: live AND wearing the app's argv."""
    return service.is_process(pid, daemon._is_app_argv)


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
        assert stopped.stopped is True
        assert stopped.pid == st.pid
        assert daemon.status().running is False

    def test_stop_when_not_running(self, home):
        st = daemon.stop()
        assert st.pid == 0
        assert (st.stopped, st.killed, st.still_running) == (False, False, False)


class TestStopIdentity:
    """A crash skips run_foreground's pidfile cleanup, so app.pid can outlive
    the daemon and the OS reuses the pid - stop() must prove the pid is the app
    before signalling it."""

    def test_stale_pid_reused_by_another_process_is_never_signalled(self, home):
        (home / "webapp").mkdir(parents=True)
        victim = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            (home / "webapp" / "app.pid").write_text(str(victim.pid))
            (home / "webapp" / "app.port").write_text("1")

            st = daemon.stop()

            assert st.stale is True
            assert victim.poll() is None, "stop() killed an unrelated process"
            # The stale state is cleared, so a later start() is unobstructed.
            assert not (home / "webapp" / "app.pid").exists()
            assert not (home / "webapp" / "app.port").exists()
        finally:
            victim.kill()
            victim.wait()

    def test_identifies_the_real_daemon_and_not_a_bystander(self, home):
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            st = daemon.start(open_browser=False)
            try:
                assert _is_app(st.pid) is True
                assert _is_app(bystander.pid) is False
            finally:
                daemon.stop()
        finally:
            bystander.kill()
            bystander.wait()

    def test_a_wedged_app_is_still_escalated_to_sigkill(self, home, tmp_path):
        """Identity must not make `bobi app stop` toothless: an app that
        ignores SIGTERM is still force-killed."""
        ready = tmp_path / "ready"
        fake = tmp_path / "bobi"
        fake.write_text('#!/bin/sh\ntrap "" TERM\ntouch "$READY"\nsleep 30\n')
        fake.chmod(0o755)
        (home / "webapp").mkdir(parents=True)
        wedged = subprocess.Popen([str(fake), "app", "run"],
                                  env={**os.environ, "READY": str(ready)})
        try:
            for _ in range(50):  # the trap is only armed once the script runs
                if ready.exists():
                    break
                time.sleep(0.1)
            assert ready.exists(), "fake daemon never started"
            (home / "webapp" / "app.pid").write_text(str(wedged.pid))
            assert _is_app(wedged.pid) is True

            st = daemon.stop()

            assert st.pid == wedged.pid
            assert st.killed is True
            assert wedged.wait(timeout=5) == -9
        finally:
            if wedged.poll() is None:
                wedged.kill()
                wedged.wait()

    def test_argv_lookup_falls_back_to_ps_without_proc(self, tmp_path,
                                                       monkeypatch):
        """macOS has no /proc, so identity comes from `ps` there - and the
        console-script spelling of the daemon is still recognised. Driven with a
        stub `ps` on PATH, since this image ships none."""
        stub = tmp_path / "ps"
        stub.write_text("#!/bin/sh\necho '/usr/local/bin/bobi app run'\n")
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(service, "_proc_argv", lambda pid: [])

        assert service.process_argv(4242) == ["/usr/local/bin/bobi", "app", "run"]
        assert daemon._is_app_argv(service.process_argv(4242)) is True

    def test_a_process_we_cannot_identify_is_never_signalled(self, monkeypatch):
        """No /proc and no ps: identity is unprovable, so the pid is off-limits
        - the failure mode of guessing is killing a stranger."""
        monkeypatch.setattr(service, "_proc_argv", lambda pid: [])
        monkeypatch.setattr(service, "_ps_argv", lambda pid: [])
        assert _is_app(os.getpid()) is False

    def test_unidentifiable_pid_keeps_its_state_files(self, tmp_path,
                                                      monkeypatch):
        """Failing closed on the signal must not fail OPEN on the bookkeeping.

        Where argv is unreadable (no /proc, no ps) a live daemon reads as
        unidentifiable. Deleting app.pid/app.port there orphans a process that
        is very likely still serving and throws away the only record of its
        port - nothing can find it, and the next start() can never bind.
        """
        monkeypatch.setattr(daemon.paths, "state_dir", lambda *a, **k: tmp_path)
        pid_path, port_path = daemon._pid_path(), daemon._port_path()
        pid_path.write_text(str(os.getpid()))   # a pid that IS alive
        port_path.write_text("8899")
        # Alive, but unidentifiable: neither argv source answers.
        monkeypatch.setattr(service, "_proc_argv", lambda pid: [])
        monkeypatch.setattr(service, "_ps_argv", lambda pid: [])

        st = daemon.stop()

        # Distinguishable from a real stop: the daemon is still running, and
        # `bobi app stop` has to say so rather than report it stopped.
        assert st.unidentified is True
        assert st.stopped is False and st.killed is False
        assert pid_path.exists(), "app.pid deleted for a live, unidentified pid"
        assert port_path.exists(), "app.port deleted - the daemon is unfindable"
