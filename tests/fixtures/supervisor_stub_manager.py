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
- ``busy-wedge-then-recover`` (#903): first launch spawns a CPU-burning
  descendant, then becomes wedged when ``--wedge-trigger-file`` appears. This
  lets the platform-native reader establish a real two-sample CPU delta before
  the ambiguous liveness verdict. Every relaunch registers a healthy idle
  director. The busy child self-exits after 60s (and the test SIGKILLs it in
  cleanup), so a failed assertion cannot leak a burn loop past the test.

Each launch appends a line to ``--launch-log`` so the test can count restarts.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project-root", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--launch-log", required=True)
    p.add_argument("--busy-pid-file", default=None)
    p.add_argument("--wedge-trigger-file", default=None)
    p.add_argument("--mode", required=True,
                   choices=["wedge-then-recover", "always-idle",
                            "dead-then-recover", "busy-wedge-then-recover"])
    a = p.parse_args()
    if a.mode == "busy-wedge-then-recover" and not a.wedge_trigger_file:
        p.error("--wedge-trigger-file is required for busy-wedge-then-recover")

    root = Path(a.project_root)
    log = Path(a.launch_log)

    # Launch index = number of prior launches recorded.
    launch_index = (len(log.read_text().splitlines()) if log.exists() else 0) + 1
    with open(log, "a") as fh:
        fh.write(f"launch {launch_index} pid={os.getpid()}\n")

    frozen = time.time() - 100_000  # far past any test threshold
    if a.mode == "always-idle":
        status = "idle"
    elif a.mode == "dead-then-recover":
        status = "error" if launch_index == 1 else "idle"
    elif a.mode == "busy-wedge-then-recover":
        status = "running" if launch_index == 1 else "idle"
    else:  # wedge-then-recover
        status = "running" if launch_index == 1 else "idle"

    from bobi import manager_health
    from bobi import paths
    if a.mode == "busy-wedge-then-recover":
        wedge_trigger = Path(a.wedge_trigger_file)

        def busy_manager_status():
            wedged = launch_index == 1 and wedge_trigger.exists()
            last_activity = frozen if wedged else time.time()
            return {
                "session": a.session,
                "status": "running" if launch_index == 1 else "idle",
                "last_activity": last_activity,
                "idle_seconds": max(0.0, time.time() - last_activity),
            }

        manager_health.start(
            paths.state_dir(root), root.name,
            session_status_fn=lambda: [],
            manager_status_fn=busy_manager_status,
        )
        if launch_index == 1:
            # Production starts manager health before sessions can launch
            # heavy descendants. Preserve that ordering so CPU evidence never
            # races ahead of the liveness signal the supervisor must judge.
            busy_child = subprocess.Popen([
                sys.executable,
                "-c",
                "import time; end=time.time()+60\n"
                "while time.time()<end: pass",
            ])
            Path(a.busy_pid_file).write_text(str(busy_child.pid))
    else:
        from bobi.sdk import set_project_root, get_registry, SessionEntry
        set_project_root(root)
        get_registry().register(SessionEntry(
            name=a.session, role="manager", status=status,
            pid=os.getpid(), last_activity=frozen,
        ))
        manager_health.start(paths.state_dir(root), root.name,
                             manager_session=a.session)

    # Behave like a live-but-quiet manager: stay up until the supervisor kills us.
    while True:
        time.sleep(0.2)


if __name__ == "__main__":
    main()
