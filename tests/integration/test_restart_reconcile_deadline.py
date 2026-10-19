"""A manager restart judges a live workflow run by its current drain (#1124).

The startup reconciler used to close every live run past ``started_at +
timeout + grace`` - a flat run-level deadline (default 3600s) that nothing in
the run enforces and that a healthy issue-lifecycle run routinely outlives.
Each restart therefore killed a healthy run, marked it ``failed`` and emitted
``agent/session.failed``.

This drives the REAL pieces end to end: a real manager (stub brain) restarted
through the CLI, a real detached workflow runner, the real registry, and the
real startup reconcile. The only thing the test writes directly is registry
DATA (backdated timestamps), so a 65-minute-old run can be staged in seconds.

Stub only: the mechanism is process lifecycle and the reconciler's predicate,
which no brain participates in.
"""

import os
import signal
import time

import pytest
import yaml

from bobi.sdk import TERMINAL_FAILED, get_registry, pid_alive

WORKFLOW = "restart-deadline"
RUN_KEY = "1124-e2e"

WORKFLOW_YAML = f"""\
name: {WORKFLOW}
trigger: >
  For the #1124 restart-reconcile integration test.
steps:
  - name: quick
    prompt: "__stub__:reply:ok"
    timeout: 60
  - name: long
    prompt: "__stub__:hang:600"
    timeout: 7200
"""


@pytest.fixture
def env(stub_bobi_env):
    """The stub install with its workflow staged and webhook topics off.

    Saved-identity repair refuses the ungranted GitHub topic on restart (see
    test_manager_lifecycle), and this test restarts twice.
    """
    from bobi import paths
    from bobi.runtime_guard import with_mutable_runtime_package

    config_path = paths.agent_yaml_path(stub_bobi_env.project_path)
    original = config_path.read_text()
    original_mode = config_path.stat().st_mode
    config = yaml.safe_load(original)
    for service in config.get("services", []):
        service["events"] = False
    workflow_path = stub_bobi_env.workflows_dir / f"{WORKFLOW}.yaml"
    try:
        config_path.chmod(0o644)
        config_path.write_text(yaml.safe_dump(config))
        with with_mutable_runtime_package(stub_bobi_env.project_path):
            workflow_path.write_text(WORKFLOW_YAML)
        yield stub_bobi_env
    finally:
        with with_mutable_runtime_package(stub_bobi_env.project_path):
            workflow_path.unlink(missing_ok=True)
        config_path.chmod(0o644)
        config_path.write_text(original)
        config_path.chmod(original_mode)


def _wait(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    pytest.fail(f"timed out waiting for {what}")


def _run_entry():
    return next((e for e in get_registry().list_all()
                 if e.run_key == RUN_KEY), None)


def _manager_boots(env) -> int:
    log = env.state_dir / "manager.log"
    return log.read_text().count("launching manager session") if log.exists() else 0


def _restart_and_wait_for_reconcile(env, cli_run):
    # The startup reconcile runs before the manager session launches, so the
    # next "launching manager session" line means the sweep has finished.
    boots = _manager_boots(env)
    result = cli_run("restart", timeout=45)
    assert result.returncode == 0, result.stderr
    _wait(lambda: _manager_boots(env) > boots, 45, "manager to reboot")


@pytest.mark.timeout(240)
def test_restart_keeps_a_healthy_long_run_and_still_closes_an_overdue_one(
        env, stub_cli_run, stub_clean_session):
    cli_run = stub_cli_run
    runner_pid = 0
    try:
        start = cli_run("start", timeout=45)
        assert start.returncode == 0, start.stderr
        _wait(lambda: _manager_boots(env), 45, "manager to boot")

        launch = cli_run(
            "subagents", "launch", "-w", WORKFLOW, "--role", "engineer",
            "--id", RUN_KEY, "--task", "restart deadline e2e",
            timeout=60,
        )
        assert launch.returncode == 0, launch.stderr
        entry = _wait(
            lambda: (e := _run_entry()) and e.phase == "long"
            and e.status == "running" and e,
            60, "the run to enter its long step",
        )
        runner_pid = entry.pid
        assert pid_alive(runner_pid)

        # Leg 1 - the production shape: the run is 1h48m old, far past the
        # run-level 3600s + grace, but inside its long step's own budget.
        get_registry().update(entry.name, started_at=time.time() - 6480)
        _restart_and_wait_for_reconcile(env, cli_run)

        survived = get_registry().get(entry.name)
        assert survived.status == "running", survived.error
        assert survived.pid == runner_pid
        assert pid_alive(runner_pid), "restart killed a healthy run"

        # Leg 2 - the outer net still holds: once the drain itself is past
        # its budget plus grace, the next restart closes the run.
        get_registry().update(entry.name, deadline_at=time.time() - 400)
        _restart_and_wait_for_reconcile(env, cli_run)

        closed = get_registry().get(entry.name)
        assert closed.status == TERMINAL_FAILED
        assert "long" in closed.error
        _wait(lambda: not pid_alive(runner_pid), 15, "the runner to exit")
    finally:
        cli_run("stop", timeout=30)
        if runner_pid and pid_alive(runner_pid):
            os.kill(runner_pid, signal.SIGKILL)
