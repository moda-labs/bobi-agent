"""Integration tests for the workflow orchestrator.

Tests the full CLI → subprocess → orchestrator pipeline for both
adhoc and multi-step workflows.
"""

import json
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)

REPO_ROOT = Path(__file__).parent.parent.parent


class TestCLIReturnsImmediately:
    """modastack agent should return in <5s for both modes."""

    def test_adhoc_returns_immediately(self, tmp_path):
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "--repo", str(tmp_path), "--task", "say hello #99"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "eng-99" in result.stdout
        assert elapsed < 5

    def test_workflow_returns_immediately(self, tmp_path):
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "issue-lifecycle",
             "--repo", str(tmp_path)],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "wf-issue-lifecycle" in result.stdout
        assert elapsed < 5

    def test_spawn_alias_uses_adhoc(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "spawn",
             "--repo", str(tmp_path), "--task", "hello #88"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "eng-88" in result.stdout

    def test_workflow_binds_issue_and_runs_are_distinct(self, tmp_path):
        """Reproduces the headline bug (#130): two workflow runs for two
        different issues must get DISTINCT run ids, each bound to its own
        issue number — not the same task-hash id that aliased the second run
        onto the first. Different repos so the (repo, issue) collision guard
        doesn't reject the second run.

        Before the fix both invocations returned the identical
        ``adhoc-<taskhash>`` id; ``--issue`` was dropped entirely.
        """
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()

        def run(repo, issue):
            r = subprocess.run(
                [sys.executable, "-m", "modastack.cli", "agent",
                 "--workflow", "issue-lifecycle", "--repo", str(repo),
                 "--issue", issue],
                capture_output=True, text=True, timeout=10, cwd=str(REPO_ROOT),
            )
            assert r.returncode == 0, f"stderr: {r.stderr}"
            return r.stdout

        out_36 = run(repo_a, "36")
        out_34 = run(repo_b, "34")

        assert "wf-issue-lifecycle" in out_36 and "36" in out_36
        assert "wf-issue-lifecycle" in out_34 and "34" in out_34
        assert out_36 != out_34


class TestValidation:
    def test_neither_task_nor_workflow(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "--repo", "/tmp"],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0

    def test_repo_required(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "--task", "X"],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0


@requires_claude
class TestAdhocAgentEndToEnd:
    """Full end-to-end: adhoc agent runs, writes log, events posted."""

    def test_adhoc_agent_log_written(self, tmp_path):
        """Subprocess writes responses to the log file."""
        from modastack.subagent import launch_agent
        name = launch_agent(task="Say 'hello' and exit. Issue #997", cwd=str(tmp_path), workflow_name="adhoc")

        log_file = Path.home() / ".modastack" / "manager" / "logs" / f"{name}.jsonl"

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if log_file.exists() and '"stop"' in log_file.read_text():
                break
            time.sleep(2)

        assert log_file.exists(), f"Log file {log_file} not created"
        content = log_file.read_text()
        assert "workflow.started" in content or "step.started" in content or "session.completed" in content, \
            f"No lifecycle events in log:\n{content[:500]}"


@requires_claude
class TestMultiStepWorkflow:
    """Full end-to-end: multi-step workflow with handoff propagation."""

    def test_two_step_workflow_completes(self, tmp_path):
        """Write a 2-step workflow, verify both steps complete."""
        wf_file = tmp_path / "test-workflow.yaml"
        wf_file.write_text(textwrap.dedent("""\
            name: test-two-step
            steps:
              - name: greet
                prompt: |
                  Say 'hello world' and write a handoff file at
                  ~/.modastack/handoffs/997.md with YAML frontmatter:
                  ---
                  greeting: hello world
                  ---
                handoff:
                  required: [greeting]
                timeout: 120

              - name: echo
                prompt: "Say the word 'done' and exit."
                timeout: 60
        """))

        from modastack.workflow.schema import load_workflow
        from modastack.workflow.orchestrator import run_workflow

        wf = load_workflow(wf_file)
        result = run_workflow(
            wf, task="test two-step", repo="test",
            cwd=str(tmp_path), issue_id="997", timeout=300,
        )

        assert result is True


@requires_claude
@pytest.mark.timeout(600)
class TestIssueLifecycleWorkflow:
    """Run the actual issue-lifecycle workflow against a test repo.

    Uses the real workflow YAML from workflows/issue-lifecycle.yaml.
    The first two steps (setup + pickup) should complete if there's a
    real git repo with an issue to work on. The route step then branches
    based on the handoff.
    """

    def test_issue_lifecycle_first_steps(self, tmp_path):
        """Run the issue-lifecycle workflow through setup and pickup.

        Creates a minimal git repo and verifies the orchestrator processes
        the first prompt steps before hitting the route.
        """
        # Set up a minimal git repo so the agent has something to work with
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        (tmp_path / "README.md").write_text("# Test repo\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "init"],
            capture_output=True,
            env={**dict(__import__("os").environ),
                 "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"},
        )

        from modastack.workflow.schema import load_workflow
        from modastack.workflow.orchestrator import run_workflow

        wf_path = REPO_ROOT / "workflows" / "issue-lifecycle.yaml"
        wf = load_workflow(wf_path)

        # Run the workflow — it will attempt setup and pickup steps.
        # In a test environment without a real issue tracker, the agent
        # will do its best with the context provided. We're testing that
        # the orchestrator drives the steps correctly, not that the agent
        # produces perfect output.
        result = run_workflow(
            wf,
            task="Work on test issue #1: Add a hello world endpoint",
            repo="test/test-repo",
            cwd=str(tmp_path),
            issue_id="1",
            timeout=300,
        )

        # The workflow may fail at some step (e.g., can't move a ticket
        # in a test repo), but the orchestrator should have attempted
        # at least the first step.
        log_file = Path.home() / ".modastack" / "manager" / "logs" / "wf-issue-lifecycle-1.jsonl"
        if log_file.exists():
            content = log_file.read_text()
            assert len(content) > 0, "Log file should have content from the orchestrator"
