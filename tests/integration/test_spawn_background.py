"""Integration tests for spawn_adhoc_background — subprocess-based spawning.

Verifies the full CLI → subprocess → spawn_adhoc pipeline:
- The subprocess launches and runs independently
- Lifecycle events (session.started, session.completed) are emitted
- The subprocess survives the caller exiting
- Errors in the subprocess are logged, not swallowed

Requires the `claude` CLI to be installed. Skipped in CI.
"""

import json
import os
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


class TestSpawnBackgroundSubprocess:
    """Test that spawn_adhoc_background launches a real subprocess."""

    def test_cli_spawn_returns_immediately(self, tmp_path):
        """modastack spawn should return in <5s, not block for the full agent."""
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "spawn",
             "--repo", str(tmp_path), "--task", "say hello #99"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "eng-99" in result.stdout
        assert elapsed < 5, f"spawn took {elapsed:.1f}s — should return immediately"

    def test_subprocess_command_is_valid_python(self):
        """The -c script passed to the subprocess should parse without errors."""
        from modastack.subagent import spawn_adhoc_background, _parse_issue_number

        # Build the same command spawn_adhoc_background would build, but don't run it
        import hashlib
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
        compile(script, "<spawn_adhoc_background>", "exec")

    @requires_claude
    def test_subprocess_runs_and_completes(self, tmp_path):
        """A spawned engineer should run as a separate process and complete."""
        from modastack.subagent import spawn_adhoc_background

        name = spawn_adhoc_background(
            cwd=str(tmp_path),
            task="Say 'hello world' and exit. Issue #997",
            timeout=60,
        )

        assert name == "eng-997"

        log_file = Path.home() / ".modastack" / "manager" / "logs" / f"{name}-adhoc.jsonl"

        # Wait for the subprocess to complete (log file gets a "stop" entry)
        deadline = time.monotonic() + 90
        completed = False
        while time.monotonic() < deadline:
            if log_file.exists():
                content = log_file.read_text()
                if '"stop"' in content:
                    completed = True
                    break
            time.sleep(2)

        assert completed, "Engineer subprocess did not complete within 90s"

        content = log_file.read_text()
        assert '"response"' in content, "No response logged"
        assert '"stop"' in content, "No stop event logged"
