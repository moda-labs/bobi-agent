"""`subagents cancel` end to end: real processes, the real CLI, a real registry.

Every test stops a tree it started itself on an isolated home. Nothing here can
reach a real manager or another session's processes.
"""

import json
import shutil
import sys
import time
from pathlib import Path

import pytest
import websocket
import yaml

from bobi.events.publish import bubble_context
from bobi.events.signing import signed_request
from bobi.reconcile import RECONCILE_GRACE
from bobi.sdk import get_registry
from tests.integration.conftest import requires_claude
from tests.process_tree import ROLES_UNDER_RUNNER, kill_if_running, pid_running

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
    """V20, the #1122 report through the commands an operator runs.

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


@requires_claude
@pytest.mark.timeout(300)
@pytest.mark.parametrize("instruction", [
    "Use the Bash tool once to run exactly this command, and wait for it to "
    "finish: {command}",
    "Use the Bash tool to run exactly this command with run_in_background set "
    "to true: {command} . Then use the Bash tool to run: sleep 170",
], ids=["foreground", "background-task"])
def test_cancel_stops_a_real_claude_tool_command(
        bobi_env, cli_run, clean_session, tmp_path, instruction):
    """V21: the real CLI's process layout is where the risk lives.

    The agent runs a command that writes its own pid. The test reads that pid
    file, not bobi.proctree, to judge what happened to the command.
    """
    name = "wf-adhoc-test-repo-cancel-claude"
    clean_session(name)
    script = tmp_path / "tick.py"
    shutil.copy(Path(__file__).parent.parent / "fixtures" / "tick.py", script)
    pid_file = tmp_path / "tick.pid"

    launched = cli_run(
        "subagents", "launch", "-w", "adhoc", "--role", "engineer",
        "--id", "cancel-claude",
        "--task", instruction.format(command=f"{sys.executable} {script}"),
        timeout=LAUNCH_TIMEOUT_S,
    )
    assert launched.returncode == 0, launched.stderr

    deadline = time.monotonic() + 180
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert pid_file.exists(), "the agent never started the tool command"
    command = int(pid_file.read_text())
    try:
        assert pid_running(command)

        cancelled = cli_run("subagents", "cancel", "cancel-claude",
                            timeout=CANCEL_TIMEOUT_S)

        assert cancelled.returncode == 0, cancelled.stderr
        assert not pid_running(command)
        ticks = tmp_path / "ticks"
        before = ticks.stat().st_size
        time.sleep(6)
        assert ticks.stat().st_size == before
    finally:
        kill_if_running(command, str(script))


@pytest.fixture
def no_external_topics(stub_bobi_env):
    """A manager needs inbox delivery here, not a GitHub subscription it has no
    grant for (shape: the fixture in test_manager_lifecycle.py)."""
    from bobi import paths

    config_path = paths.agent_yaml_path(stub_bobi_env.project_path)
    original = config_path.read_text()
    mode = config_path.stat().st_mode
    config = yaml.safe_load(original)
    for service in config.get("services", []):
        service["events"] = False
    config_path.chmod(0o644)
    config_path.write_text(yaml.safe_dump(config))
    yield
    config_path.chmod(0o644)
    config_path.write_text(original)
    config_path.chmod(mode)


def _wait_for_log_line(path: Path, line: str, start: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and line in path.read_text()[start:]:
            return
        time.sleep(0.2)
    tail = path.read_text()[start:][-2000:] if path.exists() else "(no log)"
    raise TimeoutError(f"{line!r} not logged in {timeout}s:\n{tail}")


def _start_manager(env, cli_run) -> None:
    """A real manager boot, up to the point its session is delivering events."""
    log_file = env.state_dir / "manager.log"
    start = log_file.stat().st_size if log_file.exists() else 0
    started = cli_run("start", timeout=45)
    assert started.returncode == 0, started.stderr
    _wait_for_log_line(log_file, "Drain loop active", start, timeout=90)


def _observe_session_failures(env):
    """Subscribe to the event the reconciler publishes, on the real server."""
    es_url, bubble_id, bubble_key = bubble_context(env.project_path)
    response = signed_request(
        es_url, "POST", "/deployments",
        {"name": "cancel-observer", "subscriptions": ["agent/session.failed"]},
        bubble_id, bubble_key, timeout=10,
    )
    assert response.status_code == 201, response.text
    deployment = response.json()
    socket = websocket.create_connection(
        f"ws://{es_url.removeprefix('http://')}"
        f"/deployments/{deployment['deployment_id']}/subscribe",
        header=[f"Authorization: Bearer {deployment['api_key']}"],
        timeout=10,
    )
    assert json.loads(socket.recv())["type"] == "connected"
    return socket


def _events_for(socket, run_key: str, *, seconds: float) -> list[dict]:
    """Every failure event for *run_key* seen in the next *seconds*."""
    events = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        socket.settimeout(max(0.1, min(1.0, deadline - time.monotonic())))
        try:
            message = json.loads(socket.recv())
        except websocket.WebSocketTimeoutException:
            continue
        if message.get("type") in ("event", "replay"):
            data = message["data"]
            if data.get("payload", {}).get("run_key") == run_key:
                events.append(data)
    return events


@pytest.mark.timeout(300)
def test_manager_restart_past_deadline(
        stub_bobi_env, stub_cli_run, stub_clean_session, process_tree,
        no_external_topics):
    """V24, the reconciler's deadline branch through real manager starts.

    Past its `timeout` but inside its step's budget (#1124), a restart leaves a
    live run and its whole tree alone. Past that budget, the tree is gone, the
    entry is `failed`, and the manager's own sweep publishes one
    `agent/session.failed` to the real event server.
    """
    env = stub_bobi_env
    name = "wf-adhoc-test-repo-past-deadline"
    stub_clean_session(name)
    tree = process_tree()

    # Start and stop once: publishing needs the bubble a first boot mints.
    _start_manager(env, stub_cli_run)
    stopped = stub_cli_run("stop", timeout=30)
    assert stopped.returncode == 0, stopped.stderr
    socket = _observe_session_failures(env)

    try:
        launched = stub_cli_run(
            "subagents", "launch", "-w", "adhoc", "--role", "engineer",
            "--id", "past-deadline",
            "--task", f"please __stub__:spawn:{tree.script} now",
            timeout=LAUNCH_TIMEOUT_S,
        )
        assert launched.returncode == 0, launched.stderr
        # Wait for the tree, not just the launch: the runner's own register
        # replaces the entry, and would undo a rewind made before it.
        tree.wait_for(ROLES_UNDER_RUNNER, timeout=LAUNCH_TIMEOUT_S)
        runner = _wait_for_runner_pid(env, name)
        timeout = _state(env, name)["timeout"]
        assert timeout
        get_registry().update(
            name, started_at=time.time() - (timeout + RECONCILE_GRACE + 60))

        _start_manager(env, stub_cli_run)

        state = _state(env, name)
        assert (state["status"], state["pid"]) == ("running", runner), state
        tree.assert_all_running(roles=ROLES_UNDER_RUNNER)
        assert _events_for(socket, "past-deadline", seconds=5) == []

        stopped = stub_cli_run("stop", timeout=30)
        assert stopped.returncode == 0, stopped.stderr
        get_registry().update(name, deadline_at=time.time() - (RECONCILE_GRACE + 60))

        _start_manager(env, stub_cli_run)

        state = _state(env, name)
        assert state["status"] == "failed", state
        assert not pid_running(runner)
        tree.assert_stopped()
        events = _events_for(socket, "past-deadline", seconds=5)
        assert len(events) == 1, events
        assert "past its deadline in phase task" in events[0]["payload"]["error"]
    finally:
        socket.close()
        stub_cli_run("stop", timeout=30)
