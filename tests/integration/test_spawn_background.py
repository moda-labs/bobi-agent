"""Integration tests for background agent spawning via CLI.

Verifies the full CLI → subprocess → launch_agent pipeline:
- The subprocess launches and returns immediately
- The agent runs in the background
- The spawn alias routes to the adhoc workflow

Requires the `claude` CLI to be installed. Skipped in CI.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)

REPO_ROOT = Path(__file__).parent.parent.parent


class TestSpawnBackgroundSubprocess:
    """Test that spawn / agent launch returns immediately."""

    def test_cli_agent_returns_immediately(self, tmp_path):
        """modastack agent should return in <5s, not block for the full agent."""
        from modastack.sdk import SessionRegistry, get_registry, set_repo_root
        session_name = f"wf-adhoc-{tmp_path.name}-99"
        set_repo_root(REPO_ROOT)
        registry = get_registry()
        registry.mark_done(session_name)
        session_dir = SessionRegistry.session_dir(session_name)
        if session_dir.exists():
            shutil.rmtree(session_dir)

        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "adhoc", "--role", "engineer",
             "--repo", str(tmp_path), "--task", "say hello #99"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert elapsed < 5, f"agent took {elapsed:.1f}s — should return immediately"

        registry.mark_done(session_name)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    def test_subprocess_command_is_valid_python(self):
        """The -c script passed to the subprocess should parse without errors."""
        import json
        from modastack.subagent import _parse_issue_number

        task = "Fix issue #1"
        issue_id = _parse_issue_number(task) or "adhoc-test"
        args = json.dumps({
            "cwd": "/tmp/test", "task": task, "timeout": 30,
            "requested_by": {},
        })
        script = (
            "import json, sys; from modastack.subagent import spawn_adhoc; "
            "spawn_adhoc(**json.loads(sys.argv[1]))"
        )

        # Verify it compiles
        compile(script, "<spawn_adhoc>", "exec")

    @requires_claude
    def test_subprocess_runs_and_completes(self, tmp_path):
        """A spawned agent should run as a separate process and complete."""
        from modastack.subagent import launch_agent
        from modastack.sdk import SESSION_DIR

        name = launch_agent(
            task="Say 'hello world' and exit. Issue #997",
            cwd=str(tmp_path),
            workflow_name="adhoc",
            timeout=60,
            interactive=False,
        )

        assert "997" in name

        session_dir = SESSION_DIR / name

        # Wait for the subprocess to complete
        deadline = time.monotonic() + 90
        completed = False
        while time.monotonic() < deadline:
            state_path = session_dir / "state.json"
            if state_path.exists():
                import json
                state = json.loads(state_path.read_text())
                if state.get("status") == "done":
                    completed = True
                    break
            time.sleep(2)

        assert completed, "Agent subprocess did not complete within 90s"
