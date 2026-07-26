"""Tests for the web app daemon — token persistence, status, and a real
detached start/stop round-trip against a temp BOBI_HOME."""

import os
import stat
import subprocess
import threading
import time

import pytest

from bobi import service
from bobi.webapp import daemon


def _is_app(pid: int) -> bool:
    """What `stop()` asks before it signals: live AND wearing the app's argv."""
    return service.is_process(pid, daemon._is_app_argv)


# 96 characters - an install path of unremarkable length, and 16 past the 80
# columns BSD `ps` falls back to when its output is a pipe. What gets cut is
# the tail, which is the only part any `is_*_argv` predicate looks at.
_LONG_DAEMON_ARGV = (
    "/usr/local/bin/node "
    "/Users/somebody/dev/moda/bobi/agents/eng-team/run/event-server/dist/local.js"
)


def _stub_bsd_ps(tmp_path, monkeypatch) -> str:
    """Put a `ps` on PATH that clips like BSD's unless asked for full width.

    This image ships no `ps` at all, and the Linux one does not clip the way
    macOS's does, so the platform behaviour under test has to be supplied.
    Returns the untruncated command line the stub reports.
    """
    stub = tmp_path / "ps"
    stub.write_text(
        "#!/bin/sh\n"
        f"full='{_LONG_DAEMON_ARGV}'\n"
        'case " $* " in\n'
        '  *" -ww "*) printf "%s\\n" "$full" ;;\n'
        '  *) printf "%s\\n" "$full" | cut -c1-80 ;;\n'
        "esac\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return _LONG_DAEMON_ARGV


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

    def test_stale_pid_reused_by_another_process_is_never_signalled(
            self, home, sacrificial_process):
        (home / "webapp").mkdir(parents=True)
        victim = sacrificial_process()
        (home / "webapp" / "app.pid").write_text(str(victim.pid))
        (home / "webapp" / "app.port").write_text("1")

        st = daemon.stop()

        assert st.stale is True
        assert victim.poll() is None, "stop() killed an unrelated process"
        # The stale state is cleared, so a later start() is unobstructed.
        assert not (home / "webapp" / "app.pid").exists()
        assert not (home / "webapp" / "app.port").exists()

    def test_identifies_the_real_daemon_and_not_a_bystander(
            self, home, sacrificial_process):
        bystander = sacrificial_process()
        st = daemon.start(open_browser=False)
        try:
            assert _is_app(st.pid) is True
            assert _is_app(bystander.pid) is False
        finally:
            daemon.stop()

    def test_a_wedged_app_is_still_escalated_to_sigkill(self, home, tmp_path):
        """Identity must not make `bobi app stop` toothless: an app that
        ignores SIGTERM is still force-killed."""
        ready = tmp_path / "ready"
        fake = tmp_path / "bobi"
        # `read` blocks on a pipe this test holds open, so the stand-in cannot
        # run out a clock while a loaded box delays the assertion (see
        # tests/test_pid_identity_victims.py). The ignored TERM does not
        # interrupt it, which is exactly the wedged behaviour under test.
        fake.write_text('#!/bin/sh\ntrap "" TERM\ntouch "$READY"\nread _\n')
        fake.chmod(0o755)
        (home / "webapp").mkdir(parents=True)
        wedged = subprocess.Popen([str(fake), "app", "run"],
                                  env={**os.environ, "READY": str(ready)},
                                  stdin=subprocess.PIPE)
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

    def test_ps_argv_is_not_truncated_at_the_default_width(self, tmp_path,
                                                           monkeypatch):
        """BSD `ps` clips the command column, so identity must ask for it all.

        `ps -o command=` is width-limited, and when stdout is a pipe - always,
        under `subprocess.run(capture_output=True)` - macOS falls back to 80
        columns rather than a terminal size. A real install path blows past
        that: `node <root>/event-server/dist/local.js` is 95 characters here,
        so the tail is cut off. The tail is the whole identity: every
        `is_*_argv` predicate matches on the END of argv.

        Only `-ww` lifts the limit. The stub reproduces both behaviours so the
        contract is pinned by outcome, not by asserting on the flags passed.
        """
        monkeypatch.setattr(service, "_proc_argv", lambda pid: [])
        full = _stub_bsd_ps(tmp_path, monkeypatch)

        assert service._ps_argv(4242) == full.split(), (
            "ps output was clipped at the default width - the tail of argv, "
            "which is the only part identity looks at, never arrived"
        )

    def test_a_long_argv_daemon_is_stopped_not_declared_stale(
            self, tmp_path, monkeypatch, sacrificial_process):
        """The damage the clipping does, through the real stop policy.

        A truncated argv does not read as "unidentifiable" - it reads as a
        DIFFERENT process. `stop_pidfile` treats that as a recycled pid: it
        deletes the pid file and reports `stale`. So on macOS, with an install
        path of ordinary length, `bobi ... event-server stop` left the server
        running, threw away the only record of it, and told the operator it
        had already exited. Nothing could find it afterwards and the next
        start could not bind.
        """
        monkeypatch.setattr(service, "_proc_argv", lambda pid: [])
        full = _stub_bsd_ps(tmp_path, monkeypatch)
        from bobi.events.server import is_event_server_argv

        assert is_event_server_argv(full.split()) is True, "stub argv is not the daemon"

        victim = sacrificial_process()
        pid_path = tmp_path / "event-server.pid"
        pid_path.write_text(str(victim.pid))
        # Reap on exit, so the pid stops answering `kill(pid, 0)` once the
        # SIGTERM lands. An unreaped child stays a signalable zombie, which
        # would keep the grace loop spinning for a reason unrelated to the bug.
        reaper = threading.Thread(target=victim.wait, daemon=True)
        reaper.start()

        result = service.stop_pidfile(
            pid_path, identity=is_event_server_argv, kind="the event server")

        assert result.stale is False, (
            "a live daemon was reported stale because ps clipped its argv"
        )
        assert result.stopped is True
        reaper.join(timeout=5)
        assert victim.poll() is not None, "the daemon was never signalled"

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
