"""Run a real Supervisor on the MAIN thread so SIGTERM handling is exercised.

The acceptance tests drive ``Supervisor.run`` from a worker thread (where
``signal.signal`` is a no-op), so this harness exists to test the production
path: a supervisor that installs real signal handlers and must forward SIGTERM
to its child for graceful container shutdown. The test sends this process a
SIGTERM and asserts the child dies and the supervisor exits 0.

Usage: ``python watchdog_signal_harness.py <child-pidfile>``
"""

import subprocess
import sys

from modastack.watchdog import Supervisor, WatchdogConfig


def main() -> int:
    pidfile = sys.argv[1]

    def spawn():
        # A long-lived sleeper standing in for the manager child.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        with open(pidfile, "w") as fh:
            fh.write(str(proc.pid))
        return proc

    cfg = WatchdogConfig(
        poll_interval=0.2, stall_threshold=999, confirm_polls=2,
        max_restarts=3, restart_window=60.0, backoff=(0.1,),
        min_healthy_uptime=0.0, term_grace=3.0,
    )
    # Healthy idle forever — the watchdog must never restart; the only way out
    # is the SIGTERM the test sends.
    sup = Supervisor([], cfg, spawn_fn=spawn,
                     health_fn=lambda: {"manager": {"status": "idle",
                                                    "idle_seconds": 0}})
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
