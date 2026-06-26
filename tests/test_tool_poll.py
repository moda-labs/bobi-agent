"""Tests for tool_poll and venn_poll native check runners (MDS-53 Part B).

tool_poll is the general-purpose check runner — runs any CLI command that
outputs JSON, normalizes the result to {id, ...} conditions, and returns
them for the scheduler's _reconcile path.  Pure subprocess — $0 LLM.

venn_poll is a convenience wrapper that builds the venn CLI command from
service/tool/query params.

Script caching: after a successful run the command is cached as a shell
script.  Subsequent runs try the cached script first, falling back to
direct execution (self-healing) when the cached script fails.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bobi.monitors.schema import Condition, Monitor


# ---------------------------------------------------------------------------
# tool_poll runner (general-purpose)
# ---------------------------------------------------------------------------

class TestToolPollRunner:
    """Unit tests for the general-purpose tool_poll check runner."""

    def _get_runner(self):
        from bobi.monitors.tool_checks import CHECKS
        return CHECKS["tool_poll"]

    def _monitor(self, command="echo '[]'", **extra):
        return Monitor(
            name="test-poll",
            check="tool_poll",
            event="monitor/test",
            extra={"command": command, "id_field": "id", **extra},
        )

    def test_runs_command_and_normalizes(self, tmp_path):
        """tool_poll runs the command and normalizes JSON output to conditions."""
        runner = self._get_runner()
        items = [{"id": "item-1", "value": "a"}, {"id": "item-2", "value": "b"}]
        with patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(items), stderr="",
            )
            result = runner(self._monitor(command="my-tool --list"), [Path("/repo")])

        assert len(result) == 2
        assert result[0].key == "item-1"
        assert result[1].key == "item-2"

    def test_empty_output_returns_empty_list(self, tmp_path):
        """No output = all clear."""
        runner = self._get_runner()
        with patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="", stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert result == []

    def test_custom_id_field(self):
        """id_field param selects which field becomes the condition key."""
        runner = self._get_runner()
        items = [{"uid": "abc", "text": "hi"}]
        with patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(items), stderr="",
            )
            result = runner(self._monitor(id_field="uid"), [Path("/repo")])

        assert len(result) == 1
        assert result[0].key == "abc"

    def test_command_failure_returns_none(self):
        """A failed command is indeterminate (None)."""
        runner = self._get_runner()
        with patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._script_path", return_value=Path("/nonexistent")):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="connection error",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert result is None

    def test_timeout_returns_none(self):
        """A timed-out command is indeterminate."""
        runner = self._get_runner()
        with patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("cmd", 60)
            result = runner(self._monitor(), [Path("/repo")])

        assert result is None

    def test_missing_command_returns_none(self):
        """A monitor missing the required 'command' extra returns None."""
        runner = self._get_runner()
        m = Monitor(name="bad", check="tool_poll", extra={})
        result = runner(m, [Path("/repo")])
        assert result is None

    def test_wrapped_result_object(self):
        """Handles {"result": [...]} wrapper format."""
        runner = self._get_runner()
        items = [{"id": "x1", "data": "val"}]
        with patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps({"result": items}), stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert len(result) == 1
        assert result[0].key == "x1"

    def test_missing_id_falls_back_to_hash(self):
        """Items without the id field get a hash-based key."""
        runner = self._get_runner()
        items = [{"text": "no id here"}]
        with patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(items), stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert len(result) == 1
        assert result[0].key  # has some key (hash-based)


# ---------------------------------------------------------------------------
# venn_poll runner (Venn convenience)
# ---------------------------------------------------------------------------

class TestVennPollRunner:
    """Unit tests for the venn_poll convenience check runner."""

    def _get_runner(self):
        from bobi.monitors.tool_checks import CHECKS
        return CHECKS["venn_poll"]

    def _monitor(self, **extra):
        return Monitor(
            name="email-watch",
            check="venn_poll",
            event="monitor/email",
            extra={
                "service": "work-gmail",
                "tool": "list_messages",
                "query": '{"maxResults": 5, "q": "is:unread"}',
                "id_field": "id",
                **extra,
            },
        )

    def test_normalizes_items_to_conditions(self):
        """venn_poll normalizes Venn CLI output to Condition(key=id, data=item)."""
        runner = self._get_runner()
        items = [
            {"id": "msg-1", "subject": "Hello", "from": "a@b.com"},
            {"id": "msg-2", "subject": "World", "from": "c@d.com"},
        ]
        venn_output = json.dumps({"result": items})
        with patch("bobi.monitors.tool_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout=venn_output, stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert len(result) == 2
        assert result[0].key == "msg-1"
        assert result[0].data["subject"] == "Hello"
        assert result[1].key == "msg-2"

    def test_builds_correct_venn_command(self):
        """venn_poll invokes `venn tools execute -s <service> -t <tool> -a <query>`."""
        runner = self._get_runner()
        with patch("bobi.monitors.tool_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"result": []}),
                stderr="",
            )
            runner(self._monitor(), [Path("/repo")])

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "venn" in cmd[0] or cmd[0].endswith("venn")
        assert "tools" in cmd
        assert "execute" in cmd
        idx_s = cmd.index("-s")
        assert cmd[idx_s + 1] == "work-gmail"
        idx_t = cmd.index("-t")
        assert cmd[idx_t + 1] == "list_messages"

    def test_missing_service_returns_none(self):
        """A monitor missing 'service' returns None."""
        runner = self._get_runner()
        m = Monitor(name="bad", check="venn_poll", extra={"tool": "x"})
        result = runner(m, [Path("/repo")])
        assert result is None

    def test_missing_tool_returns_none(self):
        """A monitor missing 'tool' returns None."""
        runner = self._get_runner()
        m = Monitor(name="bad", check="venn_poll", extra={"service": "x"})
        result = runner(m, [Path("/repo")])
        assert result is None

    def test_venn_not_installed_returns_none(self):
        """Returns None when venn CLI is not installed."""
        runner = self._get_runner()
        with patch("bobi.monitors.tool_checks._venn_binary", return_value=None):
            result = runner(self._monitor(), [Path("/repo")])
        assert result is None

    def test_injects_venn_api_key_in_env(self):
        """The VENN_API_KEY is passed through to the subprocess."""
        runner = self._get_runner()
        with patch("bobi.monitors.tool_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("bobi.monitors.tool_checks._run_cached_script", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch.dict("os.environ", {"VENN_API_KEY": "test-key-123"}), \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"result": []}),
                stderr="",
            )
            runner(self._monitor(), [Path("/repo")])

        env = mock_run.call_args[1].get("env", {})
        assert env.get("VENN_API_KEY") == "test-key-123"


# ---------------------------------------------------------------------------
# Script caching
# ---------------------------------------------------------------------------

class TestScriptCaching:
    """Cached scripts speed up repeat polls and self-heal on failure."""

    def test_caches_script_on_success(self, tmp_path):
        """After a successful direct run, the command is cached as a script."""
        from bobi.monitors.tool_checks import _run_command, _script_path

        with patch("bobi.monitors.tool_checks._scripts_dir", return_value=tmp_path), \
             patch("bobi.monitors.tool_checks._script_path",
                   side_effect=lambda name: tmp_path / f"{name}.sh"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{"id": "1"}]),
                stderr="",
            )
            result = _run_command(
                ["/usr/bin/my-tool", "--list"], {}, 60,
                "test-mon", "id", cache_scripts=True,
            )

        assert len(result) == 1
        script = tmp_path / "test-mon.sh"
        assert script.exists()
        content = script.read_text()
        assert "/usr/bin/my-tool" in content
        assert "--list" in content

    def test_uses_cached_script_on_subsequent_run(self, tmp_path):
        """When a cached script exists and succeeds, direct execution is skipped."""
        from bobi.monitors.tool_checks import _run_command

        cached_result = MagicMock(
            returncode=0,
            stdout=json.dumps([{"id": "cached-1"}]),
            stderr="",
        )
        with patch("bobi.monitors.tool_checks._run_cached_script",
                   return_value=cached_result), \
             patch("subprocess.run") as direct_run:
            result = _run_command(
                ["/usr/bin/my-tool"], {}, 60,
                "test-mon", "id", cache_scripts=True,
            )

        # Direct execution should NOT have been called
        direct_run.assert_not_called()
        assert len(result) == 1
        assert result[0].key == "cached-1"

    def test_falls_back_on_cached_script_failure(self, tmp_path):
        """When the cached script fails, falls back to direct execution."""
        from bobi.monitors.tool_checks import _run_command

        # Cached script returns None (timeout/error)
        with patch("bobi.monitors.tool_checks._run_cached_script",
                   return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script"):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{"id": "direct-1"}]),
                stderr="",
            )
            result = _run_command(
                ["/usr/bin/my-tool"], {}, 60,
                "test-mon", "id", cache_scripts=True,
            )

        # Direct execution was used as fallback
        mock_run.assert_called_once()
        assert result[0].key == "direct-1"

    def test_invalidates_cache_on_direct_failure(self, tmp_path):
        """When direct execution also fails, the cached script is removed."""
        from bobi.monitors.tool_checks import _run_command

        script_file = tmp_path / "test-mon.sh"
        script_file.write_text("#!/bin/bash\necho 'old'")

        with patch("bobi.monitors.tool_checks._run_cached_script",
                   return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._script_path",
                   return_value=script_file):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="error",
            )
            result = _run_command(
                ["/usr/bin/my-tool"], {}, 60,
                "test-mon", "id", cache_scripts=True,
            )

        assert result is None
        assert not script_file.exists()

    def test_cache_disabled_skips_script(self):
        """cache_scripts=False bypasses all caching logic."""
        from bobi.monitors.tool_checks import _run_command

        with patch("bobi.monitors.tool_checks._run_cached_script") as cached, \
             patch("subprocess.run") as mock_run, \
             patch("bobi.monitors.tool_checks._cache_script") as cache_fn:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{"id": "1"}]),
                stderr="",
            )
            _run_command(
                ["/usr/bin/my-tool"], {}, 60,
                "test-mon", "id", cache_scripts=False,
            )

        cached.assert_not_called()
        cache_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Reconcile integration (unchanged — validates the plumbing still works)
# ---------------------------------------------------------------------------

class TestToolPollReconcile:
    """tool_poll conditions flow through _reconcile: new IDs fire, same IDs
    dedup, disappeared IDs drop, reappeared IDs refire."""

    def _scheduler(self, tmp_path, monitors):
        from bobi.monitors.scheduler import MonitorScheduler
        published = []

        def _record(event, data):
            published.append({"event": event, "data": data})
            return True

        class FakeRegistry:
            def effective_monitors(self):
                return monitors

            def projects_for(self, m):
                return [Path("/repo")]

        sched = MonitorScheduler(
            publish=_record,
            state_path=tmp_path / "state.json",
            now=lambda: datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            registry_loader=lambda: FakeRegistry(),
            spawn_check=lambda mon, cwd, on_verdict: None,
        )
        return sched, published

    def test_new_ids_fire_same_ids_dedup(self, tmp_path):
        m = Monitor(name="email", check="tool_poll", event="monitor/email",
                    extra={"command": "echo '[]'"})
        sched, published = self._scheduler(tmp_path, [m])

        tick1 = [Condition(key="msg-1", data={"s": "a"}),
                 Condition(key="msg-2", data={"s": "b"})]
        sched._reconcile(m, tick1)
        assert len(published) == 2

        sched._reconcile(m, tick1)
        assert len(published) == 2  # unchanged

    def test_disappeared_ids_drop_reappeared_refire(self, tmp_path):
        m = Monitor(name="email", check="tool_poll", event="monitor/email",
                    extra={"command": "echo '[]'"})
        sched, published = self._scheduler(tmp_path, [m])

        sched._reconcile(m, [Condition(key="msg-1", data={})])
        assert len(published) == 1

        sched._reconcile(m, [])
        assert len(published) == 1

        sched._reconcile(m, [Condition(key="msg-1", data={})])
        assert len(published) == 2


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    """Both tool_poll and venn_poll are registered as framework-level checks."""

    def test_tool_poll_registered(self):
        from bobi.monitors.tool_checks import CHECKS
        assert "tool_poll" in CHECKS
        assert callable(CHECKS["tool_poll"])

    def test_venn_poll_registered(self):
        from bobi.monitors.tool_checks import CHECKS
        assert "venn_poll" in CHECKS
        assert callable(CHECKS["venn_poll"])

    def test_scheduler_loads_framework_checks(self, tmp_path):
        """The scheduler can find and load both tool_poll and venn_poll."""
        from bobi.monitors.scheduler import MonitorScheduler

        sched = MonitorScheduler(
            state_path=tmp_path / "state.json",
            project_path=tmp_path,
        )
        assert "tool_poll" in sched._checks
        assert "venn_poll" in sched._checks
