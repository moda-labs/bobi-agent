"""Integration tests for agent launching — adhoc and multi-step workflows.

Uses a short 2-step test workflow instead of the full issue-lifecycle
so tests complete quickly. All session state goes into the isolated install.

Requires the `claude` CLI to be installed. Skipped in CI.
"""

import json
import time

import pytest

from bobi.sdk import _sessions_dir
from .conftest import requires_claude

pytestmark = pytest.mark.claude


@requires_claude
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


@requires_claude
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
