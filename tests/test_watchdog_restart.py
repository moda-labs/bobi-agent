"""Acceptance + negative integration test for the #464 watchdog.

Real processes, no MagicMock (the #454 lesson, reinforced by Zach's R5): a
`modastack supervise`-style Supervisor drives a real stub-manager child that
serves the actual health endpoint. We assert the watchdog restarts a wedged
director and — the trap — does NOT restart a healthy idle one.

The Supervisor's process management, health polling (real HTTP) and restart
state machine are all exercised end to end; only the child *program* is the
stub (a real manager would need a Claude session).
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from modastack import manager_health, paths
from modastack.watchdog import Supervisor, WatchdogConfig

STUB = Path(__file__).parent / "fixtures" / "watchdog_stub_manager.py"
SIGNAL_HARNESS = Path(__file__).parent / "fixtures" / "watchdog_signal_harness.py"
SESSION = "moda-manager-proj"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def _fast_config():
    # Small thresholds so the acceptance test runs in seconds, not minutes.
    return WatchdogConfig(
        poll_interval=0.25,
        stall_threshold=1.0,
        confirm_polls=2,
        max_restarts=3,
        restart_window=60.0,
        backoff=(0.2, 0.2, 0.2),
        min_healthy_uptime=0.3,
        term_grace=3.0,
    )


def _spawn_fn(root: Path, launch_log: Path, mode: str):
    def spawn():
        return subprocess.Popen([
            sys.executable, str(STUB),
            "--project-root", str(root),
            "--session", SESSION,
            "--launch-log", str(launch_log),
            "--mode", mode,
        ])
    return spawn


def _launch_count(launch_log: Path) -> int:
    return len(launch_log.read_text().splitlines()) if launch_log.exists() else 0


def _wait_until(predicate, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _run_supervisor_in_thread(sup: Supervisor):
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    return t


def test_watchdog_restarts_wedged_director(tmp_path):
    root = tmp_path / "proj"
    (root / ".modastack" / "state").mkdir(parents=True)
    launch_log = tmp_path / "launches.log"

    sup = Supervisor([], _fast_config(), project_root=root,
                     spawn_fn=_spawn_fn(root, launch_log, "wedge-then-recover"))
    t = _run_supervisor_in_thread(sup)
    try:
        # (a)+(b): the watchdog detects the stall and restarts the stub — the
        # relaunch is the 2nd line in the launch log.
        assert _wait_until(lambda: _launch_count(launch_log) >= 2, timeout=20), \
            "watchdog never restarted the wedged director"

        # (c): the relaunched manager is addressable again — its health block
        # reports the recovered (idle) director, and it stays stable (no
        # restart loop on the healthy relaunch).
        def recovered():
            port_file = paths.state_path(root) / "manager-health.port"
            try:
                port = int(port_file.read_text().strip())
            except (OSError, ValueError):
                return False
            data = manager_health.health(f"http://127.0.0.1:{port}")
            return bool(data) and data.get("manager", {}).get("status") == "idle"

        assert _wait_until(recovered, timeout=10), \
            "relaunched director never returned to addressable/idle"

        # Stability: no runaway restarting once recovered.
        count_after_recovery = _launch_count(launch_log)
        time.sleep(2.0)
        assert _launch_count(launch_log) == count_after_recovery, \
            "watchdog restart-looped a recovered (idle) director"
    finally:
        sup.request_stop()
        t.join(timeout=10)


def test_watchdog_does_not_restart_healthy_idle_director(tmp_path):
    """The trap: a frozen last_activity on an *idle* director must NOT restart."""
    root = tmp_path / "proj"
    (root / ".modastack" / "state").mkdir(parents=True)
    launch_log = tmp_path / "launches.log"

    sup = Supervisor([], _fast_config(), project_root=root,
                     spawn_fn=_spawn_fn(root, launch_log, "always-idle"))
    t = _run_supervisor_in_thread(sup)
    try:
        # Wait for the stub to come up (one launch).
        assert _wait_until(lambda: _launch_count(launch_log) >= 1, timeout=10)
        # Across several stall thresholds + confirm windows, the idle director
        # is never restarted — the active-state discriminator prevents the
        # false kill.
        time.sleep(4.0)
        assert _launch_count(launch_log) == 1, \
            "watchdog false-killed a healthy idle director"
    finally:
        sup.request_stop()
        t.join(timeout=10)


def test_supervisor_forwards_sigterm_to_child_and_exits_clean(tmp_path):
    """Production path: a supervisor on the MAIN thread (real signal handlers)
    must forward SIGTERM to the manager child and exit 0 — graceful container
    shutdown. The acceptance tests run on a worker thread where signals are a
    no-op, so this is the only coverage of the signal path."""
    pidfile = tmp_path / "child.pid"
    proc = subprocess.Popen([sys.executable, str(SIGNAL_HARNESS), str(pidfile)])
    child_pid = None
    try:
        assert _wait_until(lambda: pidfile.exists(), timeout=10), \
            "supervisor harness never spawned its child"
        child_pid = int(pidfile.read_text().strip())
        assert _pid_alive(child_pid)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        assert proc.returncode == 0, "supervisor did not exit cleanly on SIGTERM"
        assert _wait_until(lambda: not _pid_alive(child_pid), timeout=5), \
            "SIGTERM was not forwarded to the manager child"
    finally:
        if proc.poll() is None:
            proc.kill()
        if child_pid and _pid_alive(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
