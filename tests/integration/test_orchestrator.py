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
             "-w", "adhoc", "--repo", str(tmp_path), "--task", "say hello #99"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "wf-adhoc" in result.stdout and "99" in result.stdout
        assert elapsed < 5

    def test_workflow_returns_immediately(self, tmp_path):
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "issue-lifecycle",
             "--repo", str(REPO_ROOT)],
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
        assert "wf-adhoc" in result.stdout and "88" in result.stdout


class TestValidation:
    def test_workflow_required(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "--repo", "/tmp", "--task", "X"],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0

    def test_repo_required(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "adhoc", "--task", "X"],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0


@requires_claude
@pytest.mark.timeout(180)
class TestSessionDirectory:
    """Verify the session directory lifecycle: creation, handoffs, log, cleanup."""

    def test_session_dir_created_with_log(self, tmp_path):
        """Adhoc agent creates session dir with state.json and log.jsonl."""
        from modastack.subagent import launch_agent
        from modastack.sdk import SESSION_DIR, SessionRegistry
        import shutil

        # Clean previous run
        from modastack.workflow.orchestrator import make_session_name
        old_session = SESSION_DIR / make_session_name("adhoc", "tmp", "996")
        if old_session.exists():
            shutil.rmtree(old_session)

        name = launch_agent(
            task="Say 'hello' and exit. Issue #996",
            cwd=str(tmp_path), workflow_name="adhoc",
            interactive=False,
        )

        session_dir = SESSION_DIR / name

        # Wait for completion
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state_path = session_dir / "state.json"
            if state_path.exists():
                import json
                state = json.loads(state_path.read_text())
                if state.get("status") == "done":
                    break
            time.sleep(2)

        assert session_dir.exists(), f"Session dir not created: {session_dir}"
        assert (session_dir / "state.json").exists(), "state.json missing"
        assert (session_dir / "log.jsonl").exists(), "log.jsonl missing"

        # Log should have content
        log_content = (session_dir / "log.jsonl").read_text()
        assert len(log_content) > 0, "Log is empty"

        # State should be done
        import json
        state = json.loads((session_dir / "state.json").read_text())
        assert state["status"] == "done"
        assert state["pid"] == 0

    def test_multistep_handoff_files(self, tmp_path):
        """Multi-step workflow creates handoff-{step}.yaml for each step."""
        from modastack.workflow.schema import Workflow, StepDef, HandoffContract, load_workflow
        from modastack.workflow.orchestrator import run_workflow, make_session_name
        from modastack.sdk import SESSION_DIR
        import shutil

        # Clean previous run so we don't hit stale session resume
        session_name = make_session_name("test-handoff", "test", "995")
        old_session = SESSION_DIR / session_name
        if old_session.exists():
            shutil.rmtree(old_session)
        old_id = SESSION_DIR / f"{session_name}.id"
        old_id.unlink(missing_ok=True)

        wf_file = tmp_path / "test-handoff.yaml"
        wf_file.write_text(textwrap.dedent("""\
            name: test-handoff
            steps:
              - name: step1
                prompt: |
                  Write the number 42 to the handoff file.
                handoff:
                  required: [answer]
                timeout: 120

              - name: step2
                prompt: "Confirm the answer and exit."
                timeout: 60
        """))

        wf = load_workflow(wf_file)
        session_name = make_session_name("test-handoff", "test", "995")
        result = run_workflow(
            wf, task="test handoff", repo="test",
            cwd=str(tmp_path), issue_id="995", timeout=300,
            interactive=False,
        )

        session_dir = SESSION_DIR / session_name

        # Session dir should exist with handoff files
        assert session_dir.exists(), f"Session dir missing: {session_dir}"
        assert (session_dir / "state.json").exists()
        assert (session_dir / "log.jsonl").exists()

        # If step1 completed, handoff-step1.yaml should exist
        handoff1 = session_dir / "handoff-step1.yaml"
        if handoff1.exists():
            import yaml
            data = yaml.safe_load(handoff1.read_text())
            assert "answer" in data, f"handoff-step1.yaml missing 'answer': {data}"
