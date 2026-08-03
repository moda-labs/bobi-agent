"""Integration tests for agent launching — adhoc and multi-step workflows.

Uses a short 2-step test workflow instead of the full issue-lifecycle so tests
complete quickly. All session state goes into the isolated install. Runs on BOTH
brains (``dual_brain_env``): the public stub (fast lane, always) and real Claude
(gated). These assert launch + completion plumbing (the subagent/workflow
completes and writes its session state/logs), so the stub proves them in CI while
the Claude leg exercises a real subagent locally - the same stub the private
sidecar e2e uses.
"""

import json
import os
import time

import pytest

from bobi.sdk import _sessions_dir


# Bind this file's ``bobi_env`` / ``cli_run`` to the dual-brain (stub + claude)
# variants (see test_manager_lifecycle for the pattern) so subagents launch once
# per brain while the test bodies stay untouched.
@pytest.fixture
def bobi_env(dual_brain_env):
    return dual_brain_env


@pytest.fixture
def cli_run(dual_brain_cli_run):
    return dual_brain_cli_run


# These tests assert launch ADMISSION, never latency, so the subprocess budget
# is generous on purpose: a cold `bobi` import is seconds on a loaded box, and
# the sibling tests that do assert latency own that concern.
LAUNCH_TIMEOUT_S = 60


@pytest.mark.timeout(600)
class TestUnkeyedLaunchDedup:
    """The #850 incident, end to end through the real CLI.

    A role file documented `subagents launch` without `--id`. Every launch got a
    random run key, so the "already active" guard never fired and the chain ran
    50 deep to the spend cap. With a derived key the chain terminates at N=2.
    """

    TASK = "Say 'hello dedup' and exit. Issue #850"
    ROLE = "engineer"

    def _derived_session_name(self):
        """The name the CLI below will actually register under.

        Every dial the launch passes has to be passed here too - `--role` most
        of all, since these launches carry one. A helper that derived under a
        different role would compute a name production never produces, and both
        tests would then assert against a session that does not exist.
        """
        from bobi.subagent import derive_run_key
        key = derive_run_key("adhoc", self.TASK, role=self.ROLE)
        return f"wf-adhoc-test-repo-{key}"

    def test_an_unkeyed_launch_registers_under_the_derived_name(
        self, bobi_env, cli_run, clean_session
    ):
        """Half of the guard: both launches have to agree on a name at all.

        This is what was broken. Every un-keyed launch minted a random key, so
        no two ever landed on the same session and the admission check below
        could not fire no matter how it was written.
        """
        name = self._derived_session_name()
        clean_session(name)

        result = cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--role", self.ROLE, "--task", self.TASK,
            timeout=LAUNCH_TIMEOUT_S,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert f"Agent started: {name}" in result.stdout, result.stdout
        # The derivation is announced, so an un-keyed launch is not silent.
        assert "derived" in result.stderr, result.stderr

    def test_identical_unkeyed_launch_is_refused_while_the_first_runs(
        self, bobi_env, cli_run, clean_session
    ):
        """The other half: the run in flight refuses its own twin.

        The first run is pinned active rather than raced against. A real stub
        agent can finish inside the seconds the second CLI spends starting up,
        and then admitting the second launch is *correct* - so racing it would
        make this test pass or fail on timing rather than on the guard.

        Pinning only holds if nothing else is still writing the entry, so the
        first run is allowed to SETTLE first. Otherwise the agent's own
        terminal write lands after the pin and clobbers it - the same race in
        the other direction.
        """
        from bobi.sdk import get_registry
        name = self._derived_session_name()
        clean_session(name)

        first = cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--role", self.ROLE, "--task", self.TASK,
            timeout=LAUNCH_TIMEOUT_S,
        )
        assert first.returncode == 0, f"stderr: {first.stderr}"

        registry = get_registry()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            entry = registry.get(name)
            if entry and entry.status not in ("starting", "running", "idle"):
                break
            time.sleep(1)
        else:
            pytest.fail(f"first run never settled: {registry.get(name)}")

        # Now that no one else writes this entry, hold it active under a pid
        # that is certainly alive: this process, so admission's crash-close
        # (reconcile.close_dead_run) must NOT clear it.
        registry.update(name, status="running", pid=os.getpid())

        second = cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--role", self.ROLE, "--task", self.TASK,
            timeout=LAUNCH_TIMEOUT_S,
        )
        assert second.returncode != 0, (
            "a byte-identical un-keyed relaunch started a second run - "
            f"stdout: {second.stdout}"
        )
        output = second.stdout + second.stderr
        assert "already active" in output, output
        assert "--id-random" in output, output
        assert "subagents cancel" in output, output
        assert "Traceback" not in output, output

    def test_id_random_opts_back_into_parallel_fan_out(
        self, bobi_env, cli_run, clean_session
    ):
        started = []
        for _ in range(2):
            result = cli_run(
                "subagents", "launch",
                "-w", "adhoc", "--role", self.ROLE, "--id-random",
                "--task", self.TASK,
                timeout=LAUNCH_TIMEOUT_S,
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            name = result.stdout.split("Agent started:")[-1].strip()
            clean_session(name)
            started.append(name)

        assert len(set(started)) == 2, started

    # "A different task still launches alongside" is deliberately NOT here.
    # Proving it end to end needs two live agents at once, so it measures the
    # concurrency semaphore rather than dedup and fails on a loaded box for a
    # reason unrelated to what it claims. It is pinned deterministically at
    # tests/test_subagent.py::TestLaunchAgentUnkeyedDedup instead.


@pytest.mark.timeout(120)
class TestAdhocAgentLaunch:

    def test_adhoc_cli_returns_immediately(self, bobi_env, cli_run, clean_session):
        clean_session("wf-adhoc-test-repo-101")

        start = time.monotonic()
        result = cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--role", "engineer",

            "--task", "Say hello #101",
            timeout=10,
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert elapsed < 5, f"adhoc took {elapsed:.1f}s — should return immediately"

    def test_adhoc_agent_completes(self, bobi_env, cli_run, clean_session):
        """Launch via CLI (subprocess finds repo from cwd) and poll for completion."""
        clean_session("wf-adhoc-test-repo-102")

        result = cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--role", "engineer", "--id", "102",
            "--task", "Say 'hello world' and exit. Issue #102",
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        session_dir = _sessions_dir() / "wf-adhoc-test-repo-102"

        deadline = time.monotonic() + 90
        completed = False
        while time.monotonic() < deadline:
            state_path = session_dir / "state.json"
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if state.get("status") == "completed":
                    completed = True
                    break
            time.sleep(2)

        assert completed, "Agent did not complete within 90s"
        assert (session_dir / "state.json").exists()
        assert (session_dir / "log.jsonl").exists()

    def test_adhoc_session_state_fields(self, bobi_env, cli_run, clean_session):
        """Verify the session state file has the expected fields after completion."""
        clean_session("wf-adhoc-test-repo-103")

        cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--role", "engineer", "--id", "103",
            "--task", "Reply with DONE. Issue #103",
            timeout=10,
        )

        session_dir = _sessions_dir() / "wf-adhoc-test-repo-103"

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state_path = session_dir / "state.json"
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if state.get("status") == "completed":
                    break
            time.sleep(2)

        state = json.loads((session_dir / "state.json").read_text())
        assert state["status"] == "completed"
        assert state["pid"] == 0
        assert state["role"] == "engineer"

        log_content = (session_dir / "log.jsonl").read_text()
        assert len(log_content) > 0


@pytest.mark.timeout(180)
class TestMultiStepWorkflowLaunch:

    def test_two_step_cli_returns_immediately(self, bobi_env, cli_run, clean_session):
        clean_session("wf-two-step-test-repo-201")

        start = time.monotonic()
        result = cli_run(
            "subagents", "launch",
            "-w", "two-step", "--role", "engineer",

            "--task", "Run test workflow #201",
            timeout=10,
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert elapsed < 5

    def test_two_step_workflow_runs_both_steps(self, bobi_env, clean_session):
        from bobi.workflow.schema import load_workflow
        from bobi.workflow.orchestrator import run_workflow, make_session_name

        session_name = make_session_name("two-step", "test-repo", "202")
        clean_session(session_name)

        wf_file = bobi_env.workflows_dir / "two-step.yaml"
        wf = load_workflow(wf_file)

        result = run_workflow(
            wf, task="Run two-step test #202", repo="test-repo",
            cwd=str(bobi_env.project_path), run_key="202",
            timeout=120, interactive=False,
        )

        session_dir = _sessions_dir() / session_name
        assert session_dir.exists(), f"Session dir missing: {session_dir}"
        assert (session_dir / "state.json").exists()
        assert (session_dir / "log.jsonl").exists()
