"""Acceptance + negative integration test for the sidecar supervision core.

Ported from the public repo's tests/test_watchdog_restart.py. Real processes,
no MagicMock: a ``bobi supervise``-style Supervisor drives a real stub-manager
child that serves the actual health endpoint. We assert the supervisor restarts
a wedged director and - the trap - does NOT restart a healthy idle one.

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

from bobi import manager_health, paths

from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.supervision import Supervisor

STUB = Path(__file__).parent / "fixtures" / "supervisor_stub_manager.py"
SIGNAL_HARNESS = Path(__file__).parent / "fixtures" / "supervisor_signal_harness.py"
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
    return SupervisorConfig(
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


def test_supervisor_restarts_wedged_director(tmp_path):
    root = tmp_path / "proj"
    (root / ".bobi" / "state").mkdir(parents=True)
    launch_log = tmp_path / "launches.log"

    sup = Supervisor([], _fast_config(), project_root=root,
                     spawn_fn=_spawn_fn(root, launch_log, "wedge-then-recover"))
    t = _run_supervisor_in_thread(sup)
    try:
        # (a)+(b): the supervisor detects the stall and restarts the stub - the
        # relaunch is the 2nd line in the launch log.
        assert _wait_until(lambda: _launch_count(launch_log) >= 2, timeout=20), \
            "supervisor never restarted the wedged director"

        # (c): the relaunched manager is addressable again - its health block
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
            "supervisor restart-looped a recovered (idle) director"
    finally:
        sup.request_stop()
        t.join(timeout=10)


def test_supervisor_restarts_dead_director(tmp_path):
    """#12: a dead director (status=error, health server still answering) must
    be restarted - the pre-fix supervisor left it stranded until a human
    SIGTERM because `error` is outside ACTIVE_STATES and the probe never
    fails."""
    root = tmp_path / "proj"
    (root / ".bobi" / "state").mkdir(parents=True)
    launch_log = tmp_path / "launches.log"

    sup = Supervisor([], _fast_config(), project_root=root,
                     spawn_fn=_spawn_fn(root, launch_log, "dead-then-recover"))
    t = _run_supervisor_in_thread(sup)
    try:
        # The supervisor restarts the dead stub - immediately, without waiting
        # out any stall threshold or confirm window.
        assert _wait_until(lambda: _launch_count(launch_log) >= 2, timeout=20), \
            "supervisor never restarted the dead (status=error) director"

        # The relaunched manager reports the recovered idle director and is
        # left alone (no restart loop on the healthy relaunch).
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

        count_after_recovery = _launch_count(launch_log)
        time.sleep(2.0)
        assert _launch_count(launch_log) == count_after_recovery, \
            "supervisor restart-looped a recovered (idle) director"
    finally:
        sup.request_stop()
        t.join(timeout=10)


def test_supervisor_does_not_restart_healthy_idle_director(tmp_path):
    """The trap: a frozen last_activity on an *idle* director must NOT restart."""
    root = tmp_path / "proj"
    (root / ".bobi" / "state").mkdir(parents=True)
    launch_log = tmp_path / "launches.log"

    sup = Supervisor([], _fast_config(), project_root=root,
                     spawn_fn=_spawn_fn(root, launch_log, "always-idle"))
    t = _run_supervisor_in_thread(sup)
    try:
        # Wait for the stub to come up (one launch).
        assert _wait_until(lambda: _launch_count(launch_log) >= 1, timeout=10)
        # Across several stall thresholds + confirm windows, the idle director
        # is never restarted - the active-state discriminator prevents the
        # false kill.
        time.sleep(4.0)
        assert _launch_count(launch_log) == 1, \
            "supervisor false-killed a healthy idle director"
    finally:
        sup.request_stop()
        t.join(timeout=10)


def test_supervisor_forwards_sigterm_to_child_and_exits_clean(tmp_path):
    """Production path: a supervisor on the MAIN thread (real signal handlers)
    must forward SIGTERM to the manager child and exit 0 - graceful container
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


def test_load_grace_defers_a_busy_wedge_then_reopens(tmp_path):
    """MOD-364 end to end: the supervisor's OWN load evidence - a real CPU-
    burning descendant walked from the real /proc - defers a confirmed wedge
    verdict on a pegged host, so the healthy-but-busy manager is NOT restarted
    and nothing is charged. The moment the load evidence drops, the gate
    reopens and the SAME wedge restarts exactly as before.

    This is the #903 trap in miniature: full-suite runs on a 2-vCPU instance
    charged the restart budget three ways and killed a productive manager.
    """
    if not Path("/proc").exists():
        pytest.skip("/proc required for the real descendant CPU walk")

    root = tmp_path / "proj"
    (root / ".bobi" / "state").mkdir(parents=True)
    launch_log = tmp_path / "launches.log"
    busy_pid_file = tmp_path / "busy.pid"

    host = {"load": (99.0, 2)}  # saturated: load1 99 over 2 cpus

    def spawn():
        return subprocess.Popen([
            sys.executable, str(STUB),
            "--project-root", str(root),
            "--session", SESSION,
            "--launch-log", str(launch_log),
            "--busy-pid-file", str(busy_pid_file),
            "--mode", "busy-wedge-then-recover",
        ])

    def load_fn(manager_pid, previous):
        from bobi.supervisor.load import load_evidence
        return load_evidence(manager_pid, previous, host_load=host["load"])

    sup = Supervisor([], _fast_config(), project_root=root,
                     spawn_fn=spawn, load_fn=load_fn)
    t = _run_supervisor_in_thread(sup)
    busy_pid = None
    try:
        # The stub forks its busy descendant and records its pid.
        assert _wait_until(lambda: busy_pid_file.exists(), timeout=10), \
            "busy descendant never spawned"
        busy_pid = int(busy_pid_file.read_text().strip())

        # Under pegged load the confirmed wedge verdict defers, across several
        # confirm windows: one launch only, no restart, nothing charged.
        time.sleep(4.0)
        assert _launch_count(launch_log) == 1, \
            "a busy wedge was restarted despite the load grace"

        # The load clears: the evidence goes inactive and the same wedge now
        # restarts - the gate reopens, the budget charges, the relaunch
        # recovers to idle.
        host["load"] = (0.1, 2)
        assert _wait_until(lambda: _launch_count(launch_log) >= 2, timeout=20), \
            "supervisor never restarted the wedge after the load cleared"
    finally:
        sup.request_stop()
        t.join(timeout=10)
        if busy_pid:
            try:
                os.kill(busy_pid, signal.SIGKILL)
            except OSError:
                pass  # already exited (its 60s deadline) or reparented away
