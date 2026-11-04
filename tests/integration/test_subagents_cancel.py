"""`subagents cancel` end to end: real processes, the real CLI, a real registry.

Every test stops a tree it started itself on an isolated home. Nothing here can
reach a real manager or another session's processes.
"""

import json
import time

import pytest

from tests.process_tree import ROLES_UNDER_RUNNER, pid_running

# Cancel can block for 15s, over the CLI runner's 10s default.
CANCEL_TIMEOUT_S = 30
LAUNCH_TIMEOUT_S = 60


def _state(env, name: str) -> dict:
    return json.loads((env.sessions_dir / name / "state.json").read_text())


def _wait_for_runner_pid(env, name: str, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _state(env, name).get("pid")
        if pid:
            return pid
        time.sleep(0.05)
    raise TimeoutError(f"{name} recorded no pid in {timeout}s")


@pytest.mark.timeout(180)
def test_cancel_stops_cli_shaped_tree(
        stub_bobi_env, stub_cli_run, stub_clean_session, process_tree):
    """The #1122 report, through the commands an operator runs.

    A detached run whose stub turn starts a tree shaped like the claude CLI's.
    When `subagents cancel` returns, the runner and the whole tree are dead and
    nothing is still writing. The tree's command sits in its own process group,
    so a process-group kill fails this test too.
    """
    name = "wf-adhoc-test-repo-cancel-tree"
    stub_clean_session(name)
    tree = process_tree()

    launched = stub_cli_run(
        "subagents", "launch", "-w", "adhoc", "--role", "engineer",
        "--id", "cancel-tree",
        "--task", f"please __stub__:spawn:{tree.script} now",
        timeout=LAUNCH_TIMEOUT_S,
    )
    assert launched.returncode == 0, launched.stderr
    tree.wait_for(ROLES_UNDER_RUNNER, timeout=LAUNCH_TIMEOUT_S)
    runner = _wait_for_runner_pid(stub_bobi_env, name)
    tree.assert_all_running(roles=ROLES_UNDER_RUNNER)
    assert pid_running(runner)

    cancelled = stub_cli_run("subagents", "cancel", "cancel-tree",
                             timeout=CANCEL_TIMEOUT_S)

    assert cancelled.returncode == 0, cancelled.stderr
    assert "Cancelled cancel-tree" in cancelled.stdout
    assert not pid_running(runner)
    tree.assert_stopped()
    state = _state(stub_bobi_env, name)
    assert (state["status"], state["pid"]) == ("cancelled", 0)
