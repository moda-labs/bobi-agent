"""Tests for the venn_poll native check runner (MDS-53 Part B).

venn_poll is a framework-level check that runs the Venn CLI to pull items
from a configured service+tool, normalizes the result to {id, ...} conditions,
and returns them for the scheduler's _reconcile path. Pure subprocess — $0 LLM.

The unit tests mock the Venn CLI subprocess and run anywhere with no
credentials. Integration tests (requiring a configured Venn CLI) are gated.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.monitors.schema import Condition, Monitor


# ---------------------------------------------------------------------------
# venn_poll runner
# ---------------------------------------------------------------------------

class TestVennPollRunner:
    """Unit tests for the venn_poll native check runner."""

    def _get_runner(self):
        from modastack.monitors.venn_checks import CHECKS
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
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=venn_output, stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert len(result) == 2
        assert result[0].key == "msg-1"
        assert result[0].data["subject"] == "Hello"
        assert result[1].key == "msg-2"

    def test_empty_result_returns_empty_list(self):
        """No items from Venn = all clear (empty list, not None)."""
        runner = self._get_runner()
        venn_output = json.dumps({"result": []})
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=venn_output, stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert result == []

    def test_custom_id_field(self):
        """id_field param selects which field becomes the condition key."""
        runner = self._get_runner()
        items = [{"message_id": "abc", "text": "hi"}]
        venn_output = json.dumps({"result": items})
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=venn_output, stderr="",
            )
            result = runner(self._monitor(id_field="message_id"), [Path("/repo")])

        assert len(result) == 1
        assert result[0].key == "abc"

    def test_missing_id_field_falls_back_to_hash(self):
        """Items without the id field get a hash-based key."""
        runner = self._get_runner()
        items = [{"text": "no id here"}]
        venn_output = json.dumps({"result": items})
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=venn_output, stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert len(result) == 1
        assert result[0].key  # has some key (hash-based)
        assert result[0].data["text"] == "no id here"

    def test_venn_cli_failure_returns_none(self):
        """A failed Venn CLI call is indeterminate (None), not all-clear."""
        runner = self._get_runner()
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="connection error",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert result is None

    def test_venn_cli_timeout_returns_none(self):
        """A timed-out Venn CLI call is indeterminate."""
        runner = self._get_runner()
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("venn", 60)
            result = runner(self._monitor(), [Path("/repo")])

        assert result is None

    def test_unparseable_output_returns_none(self):
        """Garbage output from Venn CLI is indeterminate."""
        runner = self._get_runner()
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="not json at all", stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert result is None

    def test_builds_correct_venn_command(self):
        """venn_poll invokes `venn tools execute -s <service> -t <tool> -a <query>`."""
        runner = self._get_runner()
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
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
        assert "-s" in cmd
        idx_s = cmd.index("-s")
        assert cmd[idx_s + 1] == "work-gmail"
        assert "-t" in cmd
        idx_t = cmd.index("-t")
        assert cmd[idx_t + 1] == "list_messages"

    def test_injects_venn_api_key_in_env(self):
        """The VENN_API_KEY is injected into the subprocess environment."""
        runner = self._get_runner()
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run, \
             patch.dict("os.environ", {"VENN_API_KEY": "test-key-123"}):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"result": []}),
                stderr="",
            )
            runner(self._monitor(), [Path("/repo")])

        env = mock_run.call_args[1].get("env", {})
        assert env.get("VENN_API_KEY") == "test-key-123"

    def test_result_is_list_not_wrapped(self):
        """When Venn returns a plain list (not wrapped in {result: ...}),
        venn_poll handles it."""
        runner = self._get_runner()
        items = [{"id": "x1", "data": "val"}]
        with patch("modastack.monitors.venn_checks._venn_binary", return_value="/usr/bin/venn"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(items), stderr="",
            )
            result = runner(self._monitor(), [Path("/repo")])

        assert len(result) == 1
        assert result[0].key == "x1"

    def test_missing_service_param_returns_none(self):
        """A monitor missing the required 'service' extra returns None."""
        runner = self._get_runner()
        m = Monitor(name="bad", check="venn_poll", extra={"tool": "x"})
        result = runner(m, [Path("/repo")])
        assert result is None

    def test_missing_tool_param_returns_none(self):
        """A monitor missing the required 'tool' extra returns None."""
        runner = self._get_runner()
        m = Monitor(name="bad", check="venn_poll", extra={"service": "x"})
        result = runner(m, [Path("/repo")])
        assert result is None


# ---------------------------------------------------------------------------
# venn_poll + scheduler reconcile integration
# ---------------------------------------------------------------------------

class TestVennPollReconcile:
    """venn_poll conditions flow through _reconcile: new IDs fire, same IDs
    dedup, disappeared IDs drop, reappeared IDs refire."""

    def _scheduler(self, tmp_path, monitors):
        from modastack.monitors.scheduler import MonitorScheduler
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
        m = Monitor(name="email", check="venn_poll", event="monitor/email",
                    extra={"service": "s", "tool": "t"})
        sched, published = self._scheduler(tmp_path, [m])

        # First tick: two new items
        tick1 = [Condition(key="msg-1", data={"s": "a"}),
                 Condition(key="msg-2", data={"s": "b"})]
        sched._reconcile(m, tick1)
        assert len(published) == 2

        # Second tick: same items -> no new events
        sched._reconcile(m, tick1)
        assert len(published) == 2  # unchanged

    def test_disappeared_ids_drop_reappeared_refire(self, tmp_path):
        m = Monitor(name="email", check="venn_poll", event="monitor/email",
                    extra={"service": "s", "tool": "t"})
        sched, published = self._scheduler(tmp_path, [m])

        sched._reconcile(m, [Condition(key="msg-1", data={})])
        assert len(published) == 1

        # Disappears (read/deleted)
        sched._reconcile(m, [])
        assert len(published) == 1

        # Reappears (new email with same id? unlikely but tests the path)
        sched._reconcile(m, [Condition(key="msg-1", data={})])
        assert len(published) == 2


# ---------------------------------------------------------------------------
# venn_poll registered in CHECKS
# ---------------------------------------------------------------------------

class TestVennPollRegistration:
    """venn_poll is registered as a framework-level check."""

    def test_registered_in_checks(self):
        from modastack.monitors.venn_checks import CHECKS
        assert "venn_poll" in CHECKS
        assert callable(CHECKS["venn_poll"])

    def test_scheduler_loads_framework_checks(self, tmp_path):
        """The scheduler's _check_conditions path can find and run venn_poll."""
        from modastack.monitors.scheduler import MonitorScheduler

        sched = MonitorScheduler(
            state_path=tmp_path / "state.json",
            project_path=tmp_path,
        )
        # Framework checks should be loaded even without pack checks
        assert "venn_poll" in sched._checks
