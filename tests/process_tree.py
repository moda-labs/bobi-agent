"""Start and watch a real process tree from a test (#1122).

These helpers read the operating system directly and never import
``bobi.proctree``: the tests that use them judge that module, so they must not
share its idea of what is alive.

A pid here is only ever acted on while the process's command line still names
this tree's own script, so cleanup cannot signal a recycled pid. Cleanup
signals pids one at a time and never by group, because a group kill misses this
tree by design.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "cli_shaped_tree.py"
ROLES = ("runner", "cli", "shell", "command")
# What a tree started below the runner has: the stub brain's ``__stub__:spawn``,
# where the real runner is the leader.
ROLES_UNDER_RUNNER = ("cli", "shell", "command")

# Starts a tree whose root is not this process's child, so a dead root is really
# gone rather than this process's zombie. The launcher exits straight away and
# hands back the root's pid.
_LAUNCHER = r"""
import subprocess
import sys
root = subprocess.Popen(sys.argv[1:], start_new_session=True,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
print(root.pid)
"""


def _command_line(pid: int) -> str:
    """The process's command line, or "" once it is gone or a zombie."""
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except FileNotFoundError:
        if Path("/proc/self/cmdline").exists():
            return ""
        return _command_line_from_ps(pid)
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace")


def _command_line_from_ps(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "stat=,command="],
        capture_output=True, text=True,
    )
    state, _, command = result.stdout.strip().partition(" ")
    return "" if state.startswith("Z") else command


def pid_running(pid: int) -> bool:
    """Whether *pid* is a running process. A zombie has already stopped."""
    return bool(_command_line(pid))


def kill_if_running(pid: int, marker: str) -> None:
    """SIGKILL *pid* only while its command line still contains *marker*."""
    if marker in _command_line(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class ProcessTree:
    """One copy of the fixture in *directory*, and what it has written there."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.script = directory / FIXTURE.name
        shutil.copy(FIXTURE, self.script)
        self.root: subprocess.Popen | None = None

    def _argv(self, on_term: str) -> list[str]:
        return [sys.executable, str(self.script),
                "--role", "runner", "--on-term", on_term]

    def start(self, *, on_term: str = "die") -> int:
        """Start the tree as this process's child. Returns the root's pid."""
        self.root = subprocess.Popen(
            self._argv(on_term), start_new_session=True,
            stdin=subprocess.DEVNULL,
        )
        self.wait_for(ROLES)
        return self.root.pid

    def start_detached(self, *, on_term: str = "die") -> int:
        """Start the tree with its root reparented away from this process."""
        launched = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, *self._argv(on_term)],
            capture_output=True, text=True, check=True,
        )
        self.wait_for(ROLES)
        return int(launched.stdout)

    def pids(self) -> dict[str, int]:
        return {path.stem: int(path.read_text())
                for path in self.directory.glob("*.pid")}

    def wait_for(self, roles=ROLES, timeout: float = 30.0) -> dict[str, int]:
        """Block until every role has written its pid and the command ticks."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pids = self.pids()
            if all(role in pids for role in roles) and self.ticks() > 0:
                return pids
            time.sleep(0.02)
        raise TimeoutError(
            f"tree in {self.directory} wrote only {sorted(self.pids())} "
            f"and {self.ticks()} ticks in {timeout}s"
        )

    def _is_ours(self, pid: int) -> bool:
        return str(self.script) in _command_line(pid)

    def running(self) -> dict[str, int]:
        """The roles still running. A zombie and a recycled pid do not count."""
        return {role: pid for role, pid in self.pids().items()
                if self._is_ours(pid)}

    def ticks(self) -> int:
        try:
            return (self.directory / "ticks").stat().st_size
        except OSError:
            return 0

    def assert_stopped(self, *, quiet_for: float = 1.0) -> None:
        """Every process is gone, and nothing is still writing."""
        running = self.running()
        before = self.ticks()
        time.sleep(quiet_for)
        grew = self.ticks() - before
        assert not running and not grew, (
            f"still running: {running}, and the tick file grew by {grew} "
            f"bytes in {quiet_for}s"
        )

    def assert_stopped_soon(self, *, timeout: float = 10.0) -> None:
        """For an interrupt, which sends SIGKILL and propagates without waiting."""
        deadline = time.monotonic() + timeout
        while self.running() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assert_stopped(quiet_for=0.3)

    def assert_all_running(self, *, roles=ROLES) -> None:
        assert sorted(self.running()) == sorted(roles)

    def reap(self) -> None:
        """Kill whatever is left, by verified pid. Always runs in ``finally``."""
        for pid in self.pids().values():
            kill_if_running(pid, str(self.script))
        if self.root is not None:
            self.root.wait()


# Runs one command as this process's child and stays alive afterwards. It takes
# its own session, and SIGTERM kills that whole session, so a command that
# signals its own ancestor takes out only this harness and never the test run
# (shape: ``_RUNTIME_CALLER_HARNESS`` in test_manager_lifecycle.py). It holds the
# command back until ``go`` appears, so a test can first record this process's
# pid wherever the command will look for it.
_CALLER_HARNESS = r"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

directory = Path(sys.argv[1])
command = sys.argv[2:]
deadline = time.monotonic() + 120


def die_with_the_session(_signum, _frame):
    os.killpg(os.getpgrp(), signal.SIGKILL)


signal.signal(signal.SIGTERM, die_with_the_session)
(directory / "caller.pid").write_text(str(os.getpid()))
while not (directory / "go").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
result = subprocess.run(command, capture_output=True, text=True)
# A rename, so the test never reads a file that is still being written.
(directory / "result.tmp").write_text(json.dumps({
    "returncode": result.returncode,
    "stdout": result.stdout,
    "stderr": result.stderr,
}))
os.replace(directory / "result.tmp", directory / "result.json")
while time.monotonic() < deadline:
    time.sleep(0.2)
"""


class DisposableCaller:
    """A throwaway ancestor for a command that must not signal its own caller."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.process: subprocess.Popen | None = None

    def start(self, command: list[str]) -> int:
        """Start the harness with *command* held back. Returns the harness pid."""
        self.process = subprocess.Popen(
            [sys.executable, "-c", _CALLER_HARNESS, str(self.directory), *command],
            start_new_session=True, stdin=subprocess.DEVNULL,
        )
        pid_file = self.directory / "caller.pid"
        deadline = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.process.pid

    def run(self, timeout: float = 60.0) -> dict:
        """Release the command and return what it did."""
        (self.directory / "go").touch()
        result_file = self.directory / "result.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result_file.exists():
                return json.loads(result_file.read_text())
            assert self.process.poll() is None, (
                "the caller was killed: the command signalled its own ancestor"
            )
            time.sleep(0.02)
        raise TimeoutError(f"the command did not finish in {timeout}s")

    def assert_alive(self) -> None:
        assert self.process.poll() is None, "the caller was signalled"

    def reap(self) -> None:
        """Kill the harness's own session, which this class created."""
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        if self.process is not None:
            self.process.wait()
