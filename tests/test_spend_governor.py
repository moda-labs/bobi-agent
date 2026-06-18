"""Tests for the spend governor — rolling-hour invocation cap."""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from modastack.spend_governor import (
    DEFAULT_CAP,
    WINDOW_SECONDS,
    _load_state,
    _prune,
    _save_state,
    _state_path,
    check_spend_cap,
    emit_spend_cap_alert,
    record_invocation,
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A minimal project directory with a .modastack/state/ tree."""
    modastack_dir = tmp_path / ".modastack"
    modastack_dir.mkdir()
    (modastack_dir / "agent.yaml").write_text("entry_point: x\n")
    monkeypatch.setattr("modastack.paths._root", tmp_path)
    return tmp_path


class TestStateIO:
    def test_load_empty(self, tmp_path):
        assert _load_state(tmp_path / "nonexistent.json") == []

    def test_round_trip(self, tmp_path):
        path = tmp_path / "state.json"
        timestamps = [1000.0, 2000.0, 3000.0]
        _save_state(path, timestamps)
        assert _load_state(path) == timestamps

    def test_load_corrupt_json(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("not json")
        assert _load_state(path) == []

    def test_load_wrong_shape(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"invocations": ["not", "numbers"]}))
        assert _load_state(path) == []

    def test_save_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "state.json"
        _save_state(path, [1.0])
        assert _load_state(path) == [1.0]


class TestPrune:
    def test_removes_old_entries(self):
        now = time.time()
        old = now - WINDOW_SECONDS - 1
        recent = now - 100
        result = _prune([old, recent], now)
        assert result == [recent]

    def test_keeps_all_within_window(self):
        now = time.time()
        timestamps = [now - 100, now - 50, now - 10]
        assert _prune(timestamps, now) == timestamps

    def test_empty_list(self):
        assert _prune([], time.time()) == []


class TestCheckSpendCap:
    def test_allowed_under_cap(self, project):
        allowed, count = check_spend_cap(project, cap=5)
        assert allowed is True
        assert count == 0

    def test_blocked_at_cap(self, project):
        # Seed state with exactly cap invocations
        state_file = _state_path(project)
        now = time.time()
        timestamps = [now - i for i in range(5)]
        _save_state(state_file, timestamps)

        allowed, count = check_spend_cap(project, cap=5)
        assert allowed is False
        assert count == 5

    def test_blocked_over_cap(self, project):
        state_file = _state_path(project)
        now = time.time()
        timestamps = [now - i for i in range(10)]
        _save_state(state_file, timestamps)

        allowed, count = check_spend_cap(project, cap=5)
        assert allowed is False
        assert count == 10

    def test_old_entries_pruned_before_check(self, project):
        state_file = _state_path(project)
        now = time.time()
        # 5 old entries (outside window) + 2 recent
        old = [now - WINDOW_SECONDS - i - 1 for i in range(5)]
        recent = [now - 10, now - 5]
        _save_state(state_file, old + recent)

        allowed, count = check_spend_cap(project, cap=5)
        assert allowed is True
        assert count == 2


class TestRecordInvocation:
    def test_records_timestamp(self, project):
        record_invocation(project)
        state_file = _state_path(project)
        timestamps = _load_state(state_file)
        assert len(timestamps) == 1
        assert abs(timestamps[0] - time.time()) < 2

    def test_appends_to_existing(self, project):
        record_invocation(project)
        record_invocation(project)
        state_file = _state_path(project)
        timestamps = _load_state(state_file)
        assert len(timestamps) == 2

    def test_prunes_on_record(self, project):
        state_file = _state_path(project)
        now = time.time()
        old = [now - WINDOW_SECONDS - 100]
        _save_state(state_file, old)

        record_invocation(project)
        timestamps = _load_state(state_file)
        # Old entry pruned, only the new one remains
        assert len(timestamps) == 1
        assert timestamps[0] > now - 2


class TestEmitAlert:
    @patch("modastack.events.publish.post_event")
    def test_emits_event(self, mock_post, project):
        emit_spend_cap_alert(project, count=50, cap=50)
        mock_post.assert_called_once()
        args = mock_post.call_args
        assert args[0][0] == "system/spend.cap.breached"
        payload = args[0][1]
        assert payload["count"] == 50
        assert payload["cap"] == 50
        assert "blocked" in payload["text"]

    @patch("modastack.events.publish.post_event", side_effect=Exception("boom"))
    def test_alert_failure_does_not_raise(self, mock_post, project):
        # Must not raise — alert is best-effort
        emit_spend_cap_alert(project, count=50, cap=50)


class TestConfigIntegration:
    def test_spend_cap_parsed_from_yaml(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text("entry_point: x\nspend_cap: 25\n")
        from modastack.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.spend_cap == 25

    def test_spend_cap_defaults_to_zero(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text("entry_point: x\n")
        from modastack.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.spend_cap == 0


class TestLaunchAgentGovernor:
    """Test that launch_agent checks the spend governor."""

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.paths._root", tmp_path)
        (tmp_path / ".modastack").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".modastack" / "agent.yaml").write_text("entry_point: x\n")

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_blocks_when_cap_exceeded(self, mock_launch, mock_reg, mock_check, tmp_path):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        # Seed governor state at the cap
        state_file = _state_path(tmp_path)
        now = time.time()
        _save_state(state_file, [now - i for i in range(DEFAULT_CAP)])

        from modastack.subagent import launch_agent
        with pytest.raises(RuntimeError, match="Spend governor"):
            launch_agent(task="Fix #1", cwd=str(tmp_path), workflow_name="adhoc")
        mock_launch.assert_not_called()

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_allows_under_cap(self, mock_launch, mock_reg, mock_check, tmp_path):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from modastack.subagent import launch_agent
        name = launch_agent(task="Fix #1", cwd=str(tmp_path), workflow_name="adhoc")
        assert name
        mock_launch.assert_called_once()

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_records_invocation_after_launch(self, mock_launch, mock_reg,
                                             mock_check, tmp_path):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from modastack.subagent import launch_agent
        launch_agent(task="Fix #1", cwd=str(tmp_path), workflow_name="adhoc")
        state_file = _state_path(tmp_path)
        timestamps = _load_state(state_file)
        assert len(timestamps) == 1

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_respects_custom_cap(self, mock_launch, mock_reg, mock_check, tmp_path):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        # Set a low custom cap
        (tmp_path / ".modastack" / "agent.yaml").write_text(
            "entry_point: x\nspend_cap: 2\n")
        # Seed 2 invocations
        state_file = _state_path(tmp_path)
        now = time.time()
        _save_state(state_file, [now - 10, now - 5])

        from modastack.subagent import launch_agent
        with pytest.raises(RuntimeError, match="Spend governor"):
            launch_agent(task="Fix #1", cwd=str(tmp_path), workflow_name="adhoc")
        mock_launch.assert_not_called()
