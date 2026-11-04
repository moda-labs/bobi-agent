"""A manager restart judges a live workflow run by its current drain (#1124).

The startup reconciler used to close every live run past ``started_at +
timeout + grace`` - a flat run-level deadline (default 3600s) that nothing in
the run enforces and that a healthy issue-lifecycle run routinely outlives.
Each restart therefore killed a healthy run, marked it ``failed`` and emitted
``agent/session.failed``.

This drives the REAL pieces end to end: a real manager (stub brain) restarted
through the CLI, a real detached workflow runner, the real registry, and the
real startup reconcile. The only thing the test writes directly is registry
DATA (backdated timestamps), so a run hours old can be staged in seconds.

Stub only: the mechanism is process lifecycle and the reconciler's predicate,
which no brain participates in.
"""

import os
import signal
import time

import pytest
import yaml

from bobi.reconcile import RECONCILE_GRACE
from bobi.sdk import TERMINAL_FAILED, get_registry, pid_alive
from bobi.workflow.schema import ADHOC_STEP_TIMEOUT

WORKFLOW = "restart-deadline"

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


# Generous on purpose: a cold or loaded machine can take tens of seconds to
# boot a manager, and these waits bound plumbing, not the behaviour under test.
WAIT_S = 120


def _wait(predicate, what, env=None):
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    log = env and env.state_dir / "manager.log"
    tail = log.read_text()[-2000:] if log and log.exists() else "(no manager.log)"
    pytest.fail(f"timed out waiting for {what}; manager.log tail:\n{tail}")


def _run_entry(run_key):
    return next((e for e in get_registry().list_all()
                 if e.run_key == run_key), None)


def _manager_boots(env) -> int:
    log = env.state_dir / "manager.log"
    return log.read_text().count("launching manager session") if log.exists() else 0


def _restart_and_wait_for_reconcile(env, cli_run):
    # The startup reconcile runs before the manager session launches, so the
    # next "launching manager session" line means the sweep has finished.
    boots = _manager_boots(env)
    result = cli_run("restart", timeout=45)
    assert result.returncode == 0, result.stderr
    _wait(lambda: _manager_boots(env) > boots, "manager to reboot", env)


def _age_run(name, seconds):
    # Stamps the run as if it had reached this point `seconds` ago: its start
    # and the deadline armed when its step began move back together.
    entry = get_registry().get(name)
    get_registry().update(name, started_at=entry.started_at - seconds,
                          deadline_at=entry.deadline_at - seconds)


@pytest.mark.timeout(600)
@pytest.mark.parametrize("workflow, task, step, healthy_age, overdue_age", [
    # The production shape: 1h48m into a run, inside its long step's budget.
    pytest.param(WORKFLOW, "restart deadline e2e", "long",
                 6480, 7200 + RECONCILE_GRACE + 60, id="declared-step-budget"),
    # An adhoc.yaml declaring no timeout, as every pack's does, at the age of
    # the longest completed run on record.
    pytest.param("adhoc", "__stub__:hang:600", "task",
                 8915, ADHOC_STEP_TIMEOUT + RECONCILE_GRACE + 60, id="adhoc"),
])
def test_restart_keeps_a_healthy_long_run_and_still_closes_an_overdue_one(
        env, stub_cli_run, workflow, task, step, healthy_age, overdue_age):
    cli_run = stub_cli_run
    run_key = f"1124-e2e-{step}"
    runner_pid = 0
    # The install outlives one test, so its log already holds earlier boots. A
    # boot line landing late would satisfy the restart's own wait below.
    earlier_boots = _manager_boots(env)
    try:
        start = cli_run("start", timeout=45)
        assert start.returncode == 0, start.stderr
        _wait(lambda: _manager_boots(env) > earlier_boots, "manager to boot", env)

        launch = cli_run(
            "subagents", "launch", "-w", workflow, "--role", "engineer",
            "--id", run_key, "--task", task,
            timeout=60,
        )
        assert launch.returncode == 0, launch.stderr
        entry = _wait(
            lambda: (e := _run_entry(run_key)) and e.phase == step
            and e.status == "running" and e,
            f"the run to enter its {step} step", env,
        )
        runner_pid = entry.pid
        assert pid_alive(runner_pid)

        # Leg 1 - the run is far past the run-level 3600s + grace, but inside
        # its step's own budget: a restart leaves it alone.
        _age_run(entry.name, healthy_age)
        _restart_and_wait_for_reconcile(env, cli_run)

        survived = get_registry().get(entry.name)
        assert survived.status == "running", survived.error
        assert survived.pid == runner_pid
        assert pid_alive(runner_pid), "restart killed a healthy run"

        # Leg 2 - the outer net still holds: once the drain itself is past
        # its budget plus grace, the next restart closes the run.
        _age_run(entry.name, overdue_age - healthy_age)
        _restart_and_wait_for_reconcile(env, cli_run)

        closed = get_registry().get(entry.name)
        assert closed.status == TERMINAL_FAILED
        assert f"in phase {step}" in closed.error
        _wait(lambda: not pid_alive(runner_pid), "the runner to exit", env)
    finally:
        try:
            cli_run("stop", timeout=30)
        finally:
            if runner_pid and pid_alive(runner_pid):
                os.kill(runner_pid, signal.SIGKILL)
