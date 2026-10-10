"""`install-service` under a real systemd user instance (MOD-288).

The unit tests stub every `systemctl` call, so they cannot show the thing
MOD-288 asks for: the OS bringing the agent back after it dies. This module
drives the host's own ``systemctl --user`` through the real lifecycle:

* the supervisor and manager run inside the unit's cgroup;
* systemd restarts the supervisor after a SIGKILL (``Restart=on-failure``);
* a manager started directly with ``start --foreground`` does not put the
  service into a restart loop when the service next starts (PR #1098 F1);
* ``uninstall-service`` leaves nothing running.

It runs only on Linux with a reachable systemd user instance, and skips
everywhere else. The agent lives in a temporary BOBI_HOME and runs the stub
brain. The unit name is fixed (``bobi.service``), so the module also skips
when the host already has one rather than replace a real service.

To run it from macOS, use any Linux host with systemd as PID 1 and a
lingering user, e.g. a privileged container booted with ``/sbin/init``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

AGENT = "svc-systemd-it"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "bobi.service"


def _user_systemd_ok() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        return False
    try:
        state = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return state in ("running", "degraded")


pytestmark = [
    pytest.mark.skipif(not _user_systemd_ok(),
                       reason="needs Linux with a systemd user instance"),
    pytest.mark.skipif(UNIT_PATH.exists(),
                       reason=f"{UNIT_PATH} exists; not replacing a real service"),
]


def _run(*args: str, env: dict | None = None, timeout: float = 120,
         check: bool = True):
    proc = subprocess.run(list(args), capture_output=True, text=True,
                          timeout=timeout, env=env)
    if check and proc.returncode != 0:
        pytest.fail(f"{' '.join(args)} failed ({proc.returncode}):\n"
                    f"{proc.stdout}\n{proc.stderr}")
    return proc


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cgroup(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return ""


def _in_unit(pid: int) -> bool:
    return "/bobi.service" in _cgroup(pid)


def _agent_pids() -> list[int]:
    """Live processes whose command line names this test's agent."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if AGENT.encode() in argv and int(entry.name) != os.getpid():
            found.append(int(entry.name))
    return found


class Agent:
    def __init__(self, home: Path):
        self.home = home
        self.state = home / "agents" / AGENT / "run" / "state"
        self.env = {**os.environ, "BOBI_HOME": str(home), "BOBI_STUB_BRAIN": "1"}

    def bobi(self, *args: str, **kw):
        return _run(sys.executable, "-m", "bobi", *args, env=self.env, **kw)

    def systemctl(self, *args: str, check: bool = False) -> str:
        return _run("systemctl", "--user", *args, check=check).stdout.strip()

    def main_pid(self) -> int:
        return int(self.systemctl("show", "bobi", "-p", "MainPID", "--value") or 0)

    def manager_pid(self) -> int:
        try:
            return int((self.state / "manager.pid").read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def log(self) -> str:
        try:
            return (self.state / "manager.log").read_text()
        except OSError:
            return ""

    def wait(self, predicate, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(1)
        status = self.systemctl("status", "bobi", "--no-pager")
        tail = "\n".join(self.log().splitlines()[-40:])
        pytest.fail(f"timed out after {timeout}s waiting for {what}\n\n"
                    f"{status}\n\nmanager.log:\n{tail}")


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    home = tmp_path_factory.mktemp("bobi-home")
    pack = tmp_path_factory.mktemp("pack")
    (pack / "roles" / "manager").mkdir(parents=True)
    (pack / "agent.yaml").write_text(
        "version: 1.0.0\nagent: svc_test\nentry_point: manager\n"
        "brain: {kind: stub}\n"
        'event_server: {url: "http://localhost:18999"}\n'
    )
    (pack / "roles" / "manager" / "ROLE.md").write_text("# Manager\n\nTest manager.\n")

    a = Agent(home)
    a.bobi("agents", "install", str(pack), "--name", AGENT, "--non-interactive")
    # The unit carries BOBI_HOME and PATH only; the stub brain also needs
    # BOBI_STUB_BRAIN, which the user manager hands to every unit it starts.
    a.systemctl("set-environment", "BOBI_STUB_BRAIN=1", check=True)
    try:
        yield a
    finally:
        if UNIT_PATH.exists():
            a.bobi("agent", AGENT, "uninstall-service", check=False)
        a.systemctl("unset-environment", "BOBI_STUB_BRAIN")
        for pid in _agent_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.timeout(180)
def test_install_runs_the_manager_inside_the_unit(agent: Agent):
    out = agent.bobi("agent", AGENT, "install-service").stdout
    assert "Installed and started systemd service" in out
    assert "BOBI_OS_SERVICE=1" in UNIT_PATH.read_text()

    manager = agent.wait(
        lambda: (lambda p: p if _alive(p) else 0)(agent.manager_pid()),
        60, "the supervised manager to write manager.pid",
    )
    assert agent.systemctl("is-active", "bobi") == "active"
    assert _in_unit(manager), _cgroup(manager)
    assert _in_unit(agent.main_pid())


@pytest.mark.timeout(180)
def test_systemd_restarts_a_killed_supervisor(agent: Agent):
    supervisor, manager = agent.main_pid(), agent.manager_pid()
    assert _alive(supervisor) and _alive(manager)

    os.kill(supervisor, signal.SIGKILL)

    # RestartSec=30, then a fresh supervisor spawns a fresh manager.
    agent.wait(
        lambda: agent.systemctl("is-active", "bobi") == "active"
        and agent.main_pid() not in (0, supervisor),
        75, "systemd to restart the supervisor",
    )
    new_manager = agent.wait(
        lambda: (lambda p: p if p != manager and _alive(p) else 0)(
            agent.manager_pid()),
        60, "the restarted service's manager",
    )
    assert _in_unit(new_manager)
    # KillMode=control-group took the orphaned manager down with the old
    # supervisor; two managers on one state dir would be the MOD-305 shape.
    assert not _alive(manager)


@pytest.mark.timeout(240)
def test_service_start_replaces_a_direct_manager_without_looping(agent: Agent, tmp_path):
    """PR #1098 F1. `stop` leaves the unit enabled; a manager started directly
    must not make the next service start (login, reboot) crash-loop."""
    agent.bobi("agent", AGENT, "stop")
    assert agent.systemctl("is-active", "bobi") == "inactive"

    fg_log = tmp_path / "fg.log"
    with fg_log.open("w") as sink:
        direct = subprocess.Popen(
            [sys.executable, "-m", "bobi", "agent", AGENT, "start", "--foreground"],
            env=agent.env, stdout=sink, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    try:
        _replace_direct_manager(agent, direct, fg_log)
    finally:
        if direct.poll() is None:
            direct.kill()
            direct.wait()


def _replace_direct_manager(agent: Agent, direct: subprocess.Popen, fg_log: Path):
    agent.wait(lambda: agent.manager_pid() == direct.pid, 60,
               "the directly started manager")
    assert "systemd service is installed" in fg_log.read_text()

    (agent.state / "manager.log").write_text("")
    # Not `bobi agent start`: that sweeps on its own. A bare unit start is
    # what login or boot does, so only the supervisor's sweep is in play.
    agent.systemctl("start", "bobi", check=True)

    # poll(), not kill -0: the direct manager is our child, and until it is
    # reaped a dead one still answers signal 0.
    agent.wait(lambda: direct.poll() is not None, 60,
               "the service to stop the direct manager")
    supervised = agent.wait(
        lambda: (lambda p: p if _alive(p) and _in_unit(p) else 0)(
            agent.manager_pid()),
        60, "the service's own manager",
    )
    supervisor = agent.main_pid()

    # Without the sweep the child exits on AlreadyRunning within ~30s and the
    # supervisor logs a fast crash.
    time.sleep(45)
    log = agent.log()
    assert "fast_crash=True" not in log, log
    assert "Already running" not in log, log
    assert agent.main_pid() == supervisor
    assert agent.manager_pid() == supervised and _alive(supervised)


@pytest.mark.timeout(120)
def test_uninstall_leaves_nothing_running(agent: Agent):
    manager = agent.manager_pid()

    out = agent.bobi("agent", AGENT, "uninstall-service").stdout

    assert "Removed service" in out
    assert not UNIT_PATH.exists()
    assert agent.systemctl("is-active", "bobi") != "active"
    agent.wait(lambda: not _alive(manager), 30, "the manager to exit")
    assert _agent_pids() == []
