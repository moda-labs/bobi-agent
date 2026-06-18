"""Integration tests for tool_poll script caching — real subprocess + real I/O.

Unlike the unit tests in test_tool_poll.py (which mock subprocess.run),
these tests exercise the full cache lifecycle with real shell commands,
real file I/O, and real scheduler reconciliation.  No Claude CLI needed.
"""

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from modastack.monitors.schema import Condition, Monitor
from modastack.monitors.tool_checks import (
    _run_command,
    _script_path,
    tool_poll,
)


@pytest.fixture()
def scripts_dir(tmp_path):
    """Redirect the script cache to a temp directory."""
    d = tmp_path / "scripts"
    d.mkdir()
    with patch("modastack.monitors.tool_checks._scripts_dir", return_value=d):
        yield d


class TestScriptCacheIntegration:
    """End-to-end script caching with real subprocess calls."""

    def test_first_run_caches_and_second_run_uses_cache(self, scripts_dir):
        """First run executes the command and caches it; second run uses the cached script."""
        cmd = ["echo", json.dumps([{"id": "item-1", "subject": "hello"}])]
        env = dict(os.environ)

        # First run — direct execution, should cache
        result1 = _run_command(cmd, env, 10, "cache-test", "id")
        assert result1 is not None
        assert len(result1) == 1
        assert result1[0].key == "item-1"

        # Verify the script was cached
        script = scripts_dir / "cache-test.sh"
        assert script.exists()
        assert script.stat().st_mode & stat.S_IEXEC
        content = script.read_text()
        assert "echo" in content
        assert "item-1" in content

        # Second run — should use cached script (same result)
        result2 = _run_command(cmd, env, 10, "cache-test", "id")
        assert result2 is not None
        assert len(result2) == 1
        assert result2[0].key == "item-1"

    def test_mutated_cache_returns_cached_data(self, scripts_dir):
        """If the cached script is mutated, the runner returns the mutated output."""
        cmd = ["echo", json.dumps([{"id": "original"}])]
        env = dict(os.environ)

        # First run — caches the script
        _run_command(cmd, env, 10, "mutate-test", "id")
        script = scripts_dir / "mutate-test.sh"
        assert script.exists()

        # Mutate the cached script to return different data
        script.write_text(
            '#!/usr/bin/env bash\nset -euo pipefail\n'
            'echo \'[{"id": "mutated"}]\'\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        # Second run — uses mutated cached script
        result = _run_command(cmd, env, 10, "mutate-test", "id")
        assert result is not None
        assert len(result) == 1
        assert result[0].key == "mutated"

    def test_broken_cache_falls_back_and_self_heals(self, scripts_dir):
        """A broken cached script triggers fallback to direct execution and re-caching."""
        cmd = ["echo", json.dumps([{"id": "healthy"}])]
        env = dict(os.environ)

        # First run — caches the script
        _run_command(cmd, env, 10, "heal-test", "id")
        script = scripts_dir / "heal-test.sh"
        assert script.exists()

        # Break the cached script
        script.write_text("#!/usr/bin/env bash\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        # Run again — cached script fails (exit 1), falls back to direct,
        # and re-caches the working command
        result = _run_command(cmd, env, 10, "heal-test", "id")
        assert result is not None
        assert len(result) == 1
        assert result[0].key == "healthy"

        # Verify the script was re-cached (self-healed)
        healed_content = script.read_text()
        assert "echo" in healed_content
        assert "healthy" in healed_content

    def test_tool_poll_caches_through_monitor_interface(self, scripts_dir):
        """tool_poll (the public check runner) caches scripts end-to-end."""
        payload = json.dumps([{"id": "msg-42", "from": "test@example.com"}])
        monitor = Monitor(
            name="integration-email",
            check="tool_poll",
            event="monitor/email.received",
            extra={"command": f"echo '{payload}'", "id_field": "id"},
        )

        result = tool_poll(monitor, [Path("/repo")])
        assert result is not None
        assert len(result) == 1
        assert result[0].key == "msg-42"
        assert result[0].data["from"] == "test@example.com"

        # Verify script was cached under the monitor name
        script = scripts_dir / "integration-email.sh"
        assert script.exists()

    def test_cache_invalidated_on_command_failure(self, scripts_dir):
        """When the direct command also fails, the stale cache is removed."""
        env = dict(os.environ)

        # Seed a cached script manually
        script = scripts_dir / "fail-test.sh"
        script.write_text("#!/usr/bin/env bash\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        # Run with a command that will also fail
        result = _run_command(["false"], env, 10, "fail-test", "id")
        assert result is None

        # Stale cache should be removed
        assert not script.exists()
