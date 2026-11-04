"""A real process tree shaped like a tool call under the claude CLI (#1122).

The topology, measured on a live run:

    runner (python)   pgid=R  sid=R   session and group leader
    └─ claude CLI     pgid=R  sid=R   same group as the runner
       └─ bash (tool) pgid=B  sid=B   its own session
          └─ tool cmd pgid=T  sid=B   its own process group, and the one writing

A signal to the runner alone leaves the other three running, and a signal to
its process group leaves the last two. That is the bug this tree reproduces.

Everything lands beside this file, so a test copies it into a temp directory
and the path is the only thing a caller needs to pass:

    <dir>/<role>.pid    one per process
    <dir>/ticks         grows for as long as the command runs

With no arguments the tree starts at ``cli``, for a caller that is itself the
runner (the stub brain's ``__stub__:spawn`` directive). ``--role runner`` adds
the leader, for a test with no runner of its own.

``--on-term`` changes how the command takes SIGTERM: ``die`` (the default),
``ignore``, ``linger`` (exit a second later), or ``fork`` (start a ``late``
child, then linger). Every process exits by itself after ``LIFETIME_SECONDS``
(precedent: ``supervisor_stub_manager.py``), so a failed test cannot leak a
writer for long.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIFETIME_SECONDS = 120.0
TICK_SECONDS = 0.05
LINGER_SECONDS = 1.0
NEXT_ROLE = {"runner": "cli", "cli": "shell", "shell": "command"}


def _write_pid(role: str, pid: int) -> None:
    # A rename, so a reader never sees an empty file.
    temp = HERE / f"{role}.pid.tmp"
    temp.write_text(str(pid))
    os.replace(temp, HERE / f"{role}.pid")


def _spawn(role: str, on_term: str) -> subprocess.Popen:
    kwargs = {}
    if role == "shell":
        kwargs["start_new_session"] = True
    elif role == "command":
        kwargs["process_group"] = 0
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()),
         "--role", role, "--on-term", on_term],
        stdin=subprocess.DEVNULL, **kwargs,
    )


def _trap_term(on_term: str) -> None:
    def linger(_signum, _frame):
        time.sleep(LINGER_SECONDS)
        os._exit(0)

    def fork_then_linger(_signum, _frame):
        late = _spawn("late", "die")
        _write_pid("late", late.pid)
        linger(_signum, _frame)

    if on_term == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif on_term == "linger":
        signal.signal(signal.SIGTERM, linger)
    elif on_term == "fork":
        signal.signal(signal.SIGTERM, fork_then_linger)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", default="cli")
    parser.add_argument("--on-term", default="die")
    args = parser.parse_args()

    _write_pid(args.role, os.getpid())
    if args.role == "command":
        _trap_term(args.on_term)
    next_role = NEXT_ROLE.get(args.role)
    if next_role:
        _spawn(next_role, args.on_term)

    writes = args.role in ("command", "late")
    deadline = time.monotonic() + LIFETIME_SECONDS
    while time.monotonic() < deadline:
        if writes:
            with open(HERE / "ticks", "a") as ticks:
                ticks.write("t\n")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
