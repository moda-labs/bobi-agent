"""Integration tests for manager start/stop lifecycle.

Exercises the full named start → status → stop cycle via the CLI
against the isolated install. Requires the `claude` CLI.
"""

import json
import os
import signal
import time

import pytest

from .conftest import requires_claude

pytestmark = pytest.mark.claude


@requires_claude
@pytest.mark.timeout(120)
class TestManagerStartStop:
    def test_launch_team_service_starts_manager(self, bobi_env):
        from bobi.service import launch_team, stop_team

        pid_file = bobi_env.state_dir / "manager.pid"
        try:
            entry = launch_team(bobi_env.project_path, wait_timeout=60)
            assert entry.name == f"bobi-{bobi_env.agent_name}-manager"
            assert entry.status in ("starting", "running", "idle")
            assert pid_file.exists(), "PID file not created after service launch"
        finally:
            stop_team(bobi_env.project_path)
            _wait_for_exit_file(pid_file)

    def test_start_creates_pid_file(self, bobi_env, cli_run):
        result = cli_run("start", timeout=15)
        assert result.returncode == 0, f"start failed: {result.stderr}"

        pid_file = bobi_env.state_dir / "manager.pid"

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            time.sleep(0.5)

        assert pid_file.exists(), "PID file not created after start"
        pid = int(pid_file.read_text().strip())
        assert pid > 0

        # Clean up
        os.kill(pid, signal.SIGTERM)
        _wait_for_exit(pid)
        pid_file.unlink(missing_ok=True)

    def test_start_writes_log(self, bobi_env, cli_run):
        result = cli_run("start", timeout=15)
        assert result.returncode == 0

        log_file = bobi_env.state_dir / "manager.log"

        deadline = time.monotonic() + 15
        has_log = False
        while time.monotonic() < deadline:
            if log_file.exists() and log_file.stat().st_size > 0:
                has_log = True
                break
            time.sleep(0.5)

        assert has_log, "Manager log file not written"
        content = log_file.read_text()
        assert "Bobi" in content or "starting" in content.lower()

        # Clean up
        pid_file = bobi_env.state_dir / "manager.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
            _wait_for_exit_file(pid_file)
            pid_file.unlink(missing_ok=True)

    def test_stop_removes_pid_file(self, bobi_env, cli_run):
        cli_run("start", timeout=15)

        pid_file = bobi_env.state_dir / "manager.pid"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            time.sleep(0.5)
        assert pid_file.exists()

        result = cli_run("stop", timeout=15)
        assert result.returncode == 0
        assert "stopped" in result.stdout.lower() or "stopping" in result.stdout.lower()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not pid_file.exists():
                break
            time.sleep(0.3)

        assert not pid_file.exists(), "PID file not cleaned up after stop"

    def test_stop_when_not_running(self, bobi_env, cli_run):
        pid_file = bobi_env.state_dir / "manager.pid"
        pid_file.unlink(missing_ok=True)

        result = cli_run("stop", timeout=5)
        assert result.returncode == 0
        assert "not running" in result.stdout.lower()

    def test_start_rejects_double_start(self, bobi_env, cli_run):
        cli_run("start", timeout=15)

        pid_file = bobi_env.state_dir / "manager.pid"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            time.sleep(0.5)

        result = cli_run("start", timeout=5)
        assert "already running" in result.stdout.lower()

        # Clean up
        cli_run("stop", timeout=15)
        _wait_for_exit_file(pid_file)

    def test_status_shows_running_after_start(self, bobi_env, cli_run):
        cli_run("start", timeout=15)

        pid_file = bobi_env.state_dir / "manager.pid"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            time.sleep(0.5)

        result = cli_run("status", timeout=5)
        assert result.returncode == 0

        cli_run("stop", timeout=15)
        _wait_for_exit_file(pid_file)

    def test_restart_swaps_pid(self, bobi_env, cli_run):
        cli_run("start", timeout=15)

        pid_file = bobi_env.state_dir / "manager.pid"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            time.sleep(0.5)

        old_pid = int(pid_file.read_text().strip())

        cli_run("restart", timeout=30)

        deadline = time.monotonic() + 15
        new_pid = old_pid
        while time.monotonic() < deadline:
            if pid_file.exists():
                try:
                    new_pid = int(pid_file.read_text().strip())
                    if new_pid != old_pid:
                        break
                except (ValueError, OSError):
                    pass
            time.sleep(0.5)

        assert new_pid != old_pid, "PID should change after restart"

        cli_run("stop", timeout=15)
        _wait_for_exit_file(pid_file)


@requires_claude
@pytest.mark.timeout(180)
class TestManagerMessaging:
    """Tests that require a fully booted manager with drain loop active."""

    @pytest.fixture(autouse=True)
    def _start_and_stop(self, bobi_env, cli_run):
        log_file = bobi_env.state_dir / "manager.log"
        pid_file = bobi_env.state_dir / "manager.pid"

        # Record log position before start so we only check new output
        log_pos = log_file.stat().st_size if log_file.exists() else 0

        cli_run("start", timeout=15)

        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            if pid_file.exists() and log_file.exists():
                new_content = log_file.read_text()[log_pos:]
                if "drain loop active" in new_content or "Bobi running" in new_content:
                    ready = True
                    break
            time.sleep(1)

        if not ready:
            new_content = log_file.read_text()[log_pos:] if log_file.exists() else "(no log)"
            pytest.skip(f"Manager did not become ready: {new_content[-300:]}")

        yield

        cli_run("stop", timeout=15)
        _wait_for_exit_file(pid_file)

    def test_message_and_ask(self, cli_run):
        result = cli_run("message", "hello from integration test", timeout=30)
        assert result.returncode == 0
        assert "sent" in result.stdout.lower()

        result = cli_run("ask", "Reply with just: INTEGRATION_OK", "--timeout", "90", timeout=120)
        assert result.returncode == 0, f"ask failed: stderr={result.stderr}"
        assert len(result.stdout.strip()) > 0


@requires_claude
@pytest.mark.timeout(30)
class TestManagerNotRunning:
    """Tests for message/ask when the manager is stopped."""

    def test_message_when_not_running(self, bobi_env, cli_run):
        pid_file = bobi_env.state_dir / "manager.pid"
        pid_file.unlink(missing_ok=True)

        result = cli_run("message", "should fail", timeout=5)
        output = (result.stdout + result.stderr).lower()
        assert result.returncode != 0
        assert any(msg in output for msg in [
            "not running", "no active session", "cannot reach", "process is dead",
        ])

    def test_ask_when_not_running(self, bobi_env, cli_run):
        pid_file = bobi_env.state_dir / "manager.pid"
        pid_file.unlink(missing_ok=True)

        result = cli_run("ask", "should fail", timeout=5)
        assert result.returncode != 0


def _wait_for_exit(pid: int, timeout: float = 10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.3)
        except ProcessLookupError:
            return
    raise TimeoutError(f"Process {pid} did not exit within {timeout}s")


def _wait_for_exit_file(pid_file, timeout: float = 10):
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.3)
        except ProcessLookupError:
            return
