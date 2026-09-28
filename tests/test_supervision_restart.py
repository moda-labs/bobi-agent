"""Acceptance + negative integration test for the sidecar supervision core.

Ported from the public repo's tests/test_watchdog_restart.py. Real processes,
no MagicMock: a ``bobi supervise``-style Supervisor drives a real stub-manager
child that serves the actual health endpoint. We assert the supervisor restarts
a wedged director and - the trap - does NOT restart a healthy idle one.

The Supervisor's process management, health polling (real HTTP) and restart
state machine are all exercised end to end; only the child *program* is the
stub (a real manager would need a Claude session).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from bobi import manager_health, paths

from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.supervision import Supervisor

STUB = Path(__file__).parent / "fixtures" / "supervisor_stub_manager.py"
SIGNAL_HARNESS = Path(__file__).parent / "fixtures" / "supervisor_signal_harness.py"
LOAD_HARNESS = Path(__file__).parent / "fixtures" / "supervisor_load_harness.py"
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


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


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


def test_load_grace_smoke_defers_real_busy_wedge_then_reopens(tmp_path):
    """Load grace smoke (#903/MOD-382): a real supervisor drives a real manager
    process and CPU-burning descendant through the platform-native evidence
    reader. The busy wedge is not restarted; killing the burner drops the
    evidence and reopens the same restart path.

    This is the #903 trap in miniature: full-suite runs on a 2-vCPU instance
    charged the restart budget three ways and killed a productive manager.
    """
    root = tmp_path / "proj"
    (root / ".bobi" / "state").mkdir(parents=True)
    launch_log = tmp_path / "launches.log"
    busy_pid_file = tmp_path / "busy.pid"

    state_file = tmp_path / "supervisor-state.json"
    wedge_trigger = tmp_path / "wedge.trigger"
    proc = subprocess.Popen([
        sys.executable, str(LOAD_HARNESS), str(root), str(launch_log),
        str(busy_pid_file), str(state_file), str(wedge_trigger),
    ])
    busy_pid = None
    try:
        # The stub forks its busy descendant and records its pid.
        assert _wait_until(lambda: busy_pid_file.exists(), timeout=10), \
            "busy descendant never spawned"
        busy_pid = int(busy_pid_file.read_text().strip())

        # Establish the native two-sample CPU delta before presenting an
        # ambiguous liveness verdict. Starting already wedged races the restart
        # confirmation against the first usable process-tree sample.
        def observed():
            return _read_json(state_file)

        assert _wait_until(
            lambda: bool(
                observed().get("load_evidence", {}).get("active")
            ),
            timeout=20,
        ), f"real load evidence never observed the busy descendant: {observed()}"
        assert _launch_count(launch_log) == 1

        # Transition the same live manager to wedged only after the production
        # reader has established that its real descendant is consuming CPU.
        wedge_trigger.touch()
        assert _wait_until(
            lambda: bool(observed().get("load_grace")), timeout=20
        ), \
            f"real load evidence never activated the supervisor gate: {observed()}"
        grace = observed()["load_grace"]
        assert grace is not None
        assert grace["load1"] is not None
        assert grace["busy_descendants"] >= 1
        assert grace["tree_cpu_cores"] > 0
        assert _launch_count(launch_log) == 1, \
            "a busy wedge was restarted despite the load grace"

        # Stop the real CPU work. The tree delta falls to zero and the same
        # wedge now restarts through the production decision path.
        os.kill(busy_pid, signal.SIGKILL)
        busy_pid = None
        assert _wait_until(lambda: _launch_count(launch_log) >= 2, timeout=20), \
            "supervisor never restarted the wedge after the load cleared"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if busy_pid:
            try:
                os.kill(busy_pid, signal.SIGKILL)
            except OSError:
                pass  # already exited (its 60s deadline) or reparented away
