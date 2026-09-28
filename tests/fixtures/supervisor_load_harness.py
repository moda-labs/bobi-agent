"""Run the real load-grace supervisor smoke on its production main thread."""

import subprocess
import sys
from pathlib import Path

from bobi.fsutil import atomic_write_json
from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.supervision import Supervisor, SupervisorObserver


STUB = Path(__file__).parent / "supervisor_stub_manager.py"
SESSION = "moda-manager-proj"


def main() -> int:
    root, launch_log, busy_pid_file, state_file = map(Path, sys.argv[1:])

    def spawn():
        return subprocess.Popen([
            sys.executable, str(STUB),
            "--project-root", str(root),
            "--session", SESSION,
            "--launch-log", str(launch_log),
            "--busy-pid-file", str(busy_pid_file),
            "--mode", "busy-wedge-then-recover",
        ])

    class Observer(SupervisorObserver):
        def poll(self, state):
            atomic_write_json(state_file, {"load_grace": state.load_grace})

    config = SupervisorConfig(
        poll_interval=0.25,
        stall_threshold=1.0,
        confirm_polls=2,
        max_restarts=3,
        restart_window=60.0,
        backoff=(0.2, 0.2, 0.2),
        min_healthy_uptime=0.3,
        term_grace=3.0,
        load_pegged_ratio=0.0,
        load_tree_cpu_ratio=0.02,
    )
    return Supervisor(
        [], config, project_root=root, spawn_fn=spawn, observer=Observer()
    ).run()


if __name__ == "__main__":
    sys.exit(main())
