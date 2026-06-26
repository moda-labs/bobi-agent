"""Tests for the concurrency semaphore — simultaneous agent cap."""

import time
from unittest.mock import MagicMock, patch

import pytest

from bobi.sdk import SessionEntry
from bobi.concurrency_semaphore import (
    DEFAULT_CAP,
    _EXCLUDED_ROLES,
    _POLL_INTERVAL,
    check_concurrency,
    count_active_agents,
    emit_concurrency_cap_alert,
    wait_for_slot,
)


def _mock_registry(entries):
    registry = MagicMock()
    registry.list_active = MagicMock(
        return_value=[e for e in entries
                      if e.status in ("starting", "running", "idle")])
    return registry


def _make_entry(name, role="engineer", status="running"):
    return SessionEntry(name=name, role=role, status=status, pid=0)


class TestCountActiveAgents:
    def test_empty_registry(self):
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry([])):
            assert count_active_agents() == 0

    def test_counts_engineer_agents(self):
        entries = [
            _make_entry("agent-1", role="engineer"),
            _make_entry("agent-2", role="engineer"),
        ]
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry(entries)):
            assert count_active_agents() == 2

    def test_excludes_managers(self):
        entries = [
            _make_entry("mgr-1", role="manager"),
            _make_entry("agent-1", role="engineer"),
        ]
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry(entries)):
            assert count_active_agents() == 1

    def test_excludes_monitors(self):
        entries = [
            _make_entry("check-1", role="monitor"),
            _make_entry("agent-1", role="engineer"),
        ]
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry(entries)):
            assert count_active_agents() == 1

    def test_counts_roles_without_explicit_role(self):
        """Agents with empty or non-excluded roles count toward the cap."""
        entries = [
            _make_entry("agent-1", role=""),
            _make_entry("agent-2", role="project_lead"),
        ]
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry(entries)):
            assert count_active_agents() == 2


class TestCheckConcurrency:
    def test_allowed_under_cap(self):
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry([_make_entry("a1")])):
            allowed, count = check_concurrency(cap=2)
            assert allowed is True
            assert count == 1

    def test_blocked_at_cap(self):
        entries = [_make_entry(f"a{i}") for i in range(3)]
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry(entries)):
            allowed, count = check_concurrency(cap=3)
            assert allowed is False
            assert count == 3

    def test_blocked_over_cap(self):
        entries = [_make_entry(f"a{i}") for i in range(5)]
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry(entries)):
            allowed, count = check_concurrency(cap=2)
            assert allowed is False
            assert count == 5

    def test_allowed_when_empty(self):
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry([])):
            allowed, count = check_concurrency(cap=1)
            assert allowed is True
            assert count == 0


class TestWaitForSlot:
    @patch("bobi.concurrency_semaphore.time.sleep")
    def test_returns_immediately_when_under_cap(self, mock_sleep):
        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry([])):
            result = wait_for_slot(cap=2, timeout=10)
            assert result is True
            mock_sleep.assert_not_called()

    @patch("bobi.concurrency_semaphore.time.sleep")
    def test_waits_then_succeeds_when_slot_opens(self, mock_sleep):
        """Simulates an agent finishing between poll iterations."""
        entries_full = [_make_entry(f"a{i}") for i in range(2)]
        entries_open = [_make_entry("a0")]

        call_count = [0]

        def mock_list_active():
            call_count[0] += 1
            if call_count[0] <= 1:
                return entries_full
            return entries_open

        registry = MagicMock()
        registry.list_active = mock_list_active

        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=registry):
            result = wait_for_slot(cap=2, timeout=30)
            assert result is True
            assert mock_sleep.call_count >= 1

    @patch("bobi.concurrency_semaphore.time.sleep")
    def test_times_out_when_always_full(self, mock_sleep):
        entries = [_make_entry(f"a{i}") for i in range(2)]

        # Make time advance past the deadline after a few polls
        real_time = time.time
        call_count = [0]

        def advancing_time():
            call_count[0] += 1
            if call_count[0] > 3:
                return real_time() + 1000  # jump past deadline
            return real_time()

        with patch("bobi.concurrency_semaphore.get_registry",
                   return_value=_mock_registry(entries)):
            with patch("bobi.concurrency_semaphore.time.time",
                       side_effect=advancing_time):
                result = wait_for_slot(cap=2, timeout=0.01)
                assert result is False


class TestEmitAlert:
    @patch("bobi.events.publish.post_event")
    def test_emits_event(self, mock_post):
        emit_concurrency_cap_alert(count=2, cap=2)
        mock_post.assert_called_once()
        args = mock_post.call_args
        assert args[0][0] == "system/concurrency.cap.queued"
        payload = args[0][1]
        assert payload["count"] == 2
        assert payload["cap"] == 2

    @patch("bobi.events.publish.post_event",
           side_effect=Exception("boom"))
    def test_alert_failure_does_not_raise(self, mock_post):
        # Must not raise — alert is best-effort
        emit_concurrency_cap_alert(count=2, cap=2)


class TestConfigIntegration:
    def test_max_concurrent_parsed_from_yaml(self, tmp_path):
        config_dir = tmp_path / ".bobi"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nmax_concurrent_agents: 4\n")
        from bobi.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.max_concurrent_agents == 4

    def test_max_concurrent_defaults_to_zero(self, tmp_path):
        config_dir = tmp_path / ".bobi"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text("entry_point: x\n")
        from bobi.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.max_concurrent_agents == 0


class TestLaunchAgentConcurrency:
    """Test that launch_agent enforces the concurrency semaphore."""

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path)
        (tmp_path / ".bobi").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".bobi" / "agent.yaml").write_text("entry_point: x\n")

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    @patch("bobi.concurrency_semaphore.count_active_agents", return_value=5)
    @patch("bobi.concurrency_semaphore.wait_for_slot", return_value=False)
    def test_blocks_when_cap_exceeded_and_timeout(
        self, mock_wait, mock_count, mock_launch, mock_reg, mock_check, tmp_path
    ):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        with pytest.raises(RuntimeError, match="Concurrency semaphore"):
            launch_agent(task="Fix #1", cwd=str(tmp_path),
                         workflow_name="adhoc")
        mock_launch.assert_not_called()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    @patch("bobi.concurrency_semaphore.count_active_agents", return_value=0)
    def test_allows_under_cap(
        self, mock_count, mock_launch, mock_reg, mock_check, tmp_path
    ):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        name = launch_agent(task="Fix #1", cwd=str(tmp_path),
                            workflow_name="adhoc")
        assert name
        mock_launch.assert_called_once()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    @patch("bobi.concurrency_semaphore.count_active_agents", return_value=3)
    @patch("bobi.concurrency_semaphore.wait_for_slot", return_value=True)
    def test_queues_then_proceeds_when_slot_opens(
        self, mock_wait, mock_count, mock_launch, mock_reg, mock_check, tmp_path
    ):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        name = launch_agent(task="Fix #1", cwd=str(tmp_path),
                            workflow_name="adhoc")
        assert name
        mock_launch.assert_called_once()
        mock_wait.assert_called_once()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    @patch("bobi.concurrency_semaphore.count_active_agents", return_value=3)
    @patch("bobi.concurrency_semaphore.wait_for_slot", return_value=False)
    def test_respects_custom_cap(
        self, mock_wait, mock_count, mock_launch, mock_reg, mock_check, tmp_path
    ):
        """A custom cap from agent.yaml should be used."""
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        (tmp_path / ".bobi" / "agent.yaml").write_text(
            "entry_point: x\nmax_concurrent_agents: 5\n")
        from bobi.subagent import launch_agent
        # cap=5, count=3 → should be allowed (check_concurrency returns True)
        # But we've mocked count_active_agents to return 3 at the module level
        # and wait_for_slot to return False. The _check_concurrency_semaphore
        # calls check_concurrency which calls count_active_agents → 3 < 5 → allowed.
        # So it should NOT call wait_for_slot.
        name = launch_agent(task="Fix #1", cwd=str(tmp_path),
                            workflow_name="adhoc")
        assert name
        mock_launch.assert_called_once()
        mock_wait.assert_not_called()  # 3 < 5, no waiting needed
