"""A tiny *real* manager stand-in for the #464 watchdog acceptance test.

It does what the real manager does that the watchdog cares about — registers an
entry-point session and serves the health endpoint — but nothing else, so the
acceptance test can drive real processes (the #454 "no MagicMock" lesson)
without a Claude session.

Modes:
- ``wedge-then-recover``: first launch registers a wedged director
  (``status=running`` with a frozen ``last_activity``); every relaunch
  registers a healthy idle director (``status=idle``). Lets the test prove the
  watchdog restarts the wedge and then stabilises on the recovered manager.
- ``always-idle``: always registers a healthy idle director with a frozen
  ``last_activity`` — the trap. The watchdog must NOT restart it (negative
  test).

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
    p.add_argument("--mode", required=True,
                   choices=["wedge-then-recover", "always-idle"])
    a = p.parse_args()

    root = Path(a.project_root)
    log = Path(a.launch_log)

    # Launch index = number of prior launches recorded.
    launch_index = (len(log.read_text().splitlines()) if log.exists() else 0) + 1
    with open(log, "a") as fh:
        fh.write(f"launch {launch_index} pid={os.getpid()}\n")

    from bobi.sdk import set_project_root, get_registry, SessionEntry
    set_project_root(root)

    frozen = time.time() - 100_000  # far past any test threshold
    if a.mode == "always-idle":
        status = "idle"
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

    # Behave like a live-but-quiet manager: stay up until the watchdog kills us.
    while True:
        time.sleep(0.2)


if __name__ == "__main__":
    main()
