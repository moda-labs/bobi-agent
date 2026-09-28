"""Run the real load-grace supervisor smoke on its production main thread."""

import subprocess
import sys
from pathlib import Path

from bobi.fsutil import atomic_write_json
from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.load import default_load_evidence
from bobi.supervisor.supervision import Supervisor, SupervisorObserver


STUB = Path(__file__).parent / "supervisor_stub_manager.py"
SESSION = "moda-manager-proj"


def main() -> int:
    root, launch_log, busy_pid_file, state_file, wedge_trigger = map(
        Path, sys.argv[1:]
    )

    def spawn():
        return subprocess.Popen([
            sys.executable, str(STUB),
            "--project-root", str(root),
            "--session", SESSION,
            "--launch-log", str(launch_log),
            "--busy-pid-file", str(busy_pid_file),
            "--wedge-trigger-file", str(wedge_trigger),
            "--mode", "busy-wedge-then-recover",
        ])

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
    observed = {}

    def load(manager_pid, previous):
        evidence = default_load_evidence(
            manager_pid,
            previous,
            pegged_ratio=config.load_pegged_ratio,
            tree_cpu_ratio=config.load_tree_cpu_ratio,
        )
        observed["load_evidence"] = {
            key: value for key, value in evidence.items() if key != "sample"
        }
        return evidence

    class Observer(SupervisorObserver):
        def poll(self, state):
            observed["load_grace"] = state.load_grace
            atomic_write_json(state_file, observed)

    return Supervisor(
        [], config, project_root=root, spawn_fn=spawn, load_fn=load,
        observer=Observer(),
    ).run()


if __name__ == "__main__":
    sys.exit(main())
