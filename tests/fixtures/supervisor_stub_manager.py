"""A tiny *real* manager stand-in for the sidecar acceptance test.

Ported from the public repo's tests/fixtures/watchdog_stub_manager.py. It does
what the real manager does that the supervisor cares about - registers an
entry-point session and serves the health endpoint - but nothing else, so the
acceptance test can drive real processes (the "no MagicMock" lesson) without a
Claude session.

Modes:
- ``wedge-then-recover``: first launch registers a wedged director
  (``status=running`` with a frozen ``last_activity``); every relaunch
  registers a healthy idle director (``status=idle``). Lets the test prove the
  supervisor restarts the wedge and then stabilises on the recovered manager.
- ``always-idle``: always registers a healthy idle director with a frozen
  ``last_activity`` - the trap. The supervisor must NOT restart it (negative
  test).
- ``dead-then-recover``: first launch registers a *dead* director
  (``status=error``) whose health server keeps answering - the exact #12
  stranding shape. Every relaunch registers a healthy idle director.
- ``busy-wedge-then-recover`` (MOD-364): first launch registers a wedged
  director AND forks a CPU-burning descendant, writing the busy child's pid to
  ``--busy-pid-file`` - the load-grace shape: a sanctioned heavy worker on a
  saturated host. Every relaunch registers a healthy idle director. The busy
  child self-exits after 60s (and the test SIGKILLs it in cleanup), so a
  failed assertion cannot leak a burn loop past the test.

Each launch appends a line to ``--launch-log`` so the test can count restarts.
"""

import argparse
import os
import time
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project-root", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--launch-log", required=True)
    p.add_argument("--busy-pid-file", default=None)
    p.add_argument("--mode", required=True,
                   choices=["wedge-then-recover", "always-idle",
                            "dead-then-recover", "busy-wedge-then-recover"])
    a = p.parse_args()

    root = Path(a.project_root)
    log = Path(a.launch_log)

    # Launch index = number of prior launches recorded.
    launch_index = (len(log.read_text().splitlines()) if log.exists() else 0) + 1
    with open(log, "a") as fh:
        fh.write(f"launch {launch_index} pid={os.getpid()}\n")

    busy_child = None
    if a.mode == "busy-wedge-then-recover" and launch_index == 1:
        # A real descendant burning CPU: fork a tight loop that outlives this
        # manager (reparented to init when the supervisor kills us), so the
        # supervisor's /proc walk sees a busy process in the manager's tree.
        busy_child = os.fork()
        if busy_child == 0:
            deadline = time.time() + 60
            while time.time() < deadline:
                pass
            os._exit(0)
        Path(a.busy_pid_file).write_text(str(busy_child))

    from bobi.sdk import set_project_root, get_registry, SessionEntry
    set_project_root(root)

    frozen = time.time() - 100_000  # far past any test threshold
    if a.mode == "always-idle":
        status = "idle"
    elif a.mode == "dead-then-recover":
        status = "error" if launch_index == 1 else "idle"
    elif a.mode == "busy-wedge-then-recover":
        status = "running" if launch_index == 1 else "idle"
    else:  # wedge-then-recover
        status = "running" if launch_index == 1 else "idle"

    get_registry().register(SessionEntry(
        name=a.session, role="manager", status=status,
        pid=os.getpid(), last_activity=frozen,
    ))

    from bobi import manager_health
    from bobi import paths
    manager_health.start(paths.state_dir(root), root.name,
                         manager_session=a.session)

    # Behave like a live-but-quiet manager: stay up until the supervisor kills us.
    while True:
        time.sleep(0.2)


if __name__ == "__main__":
    main()
