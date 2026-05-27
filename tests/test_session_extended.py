"""Extended tests for modastack/session.py — thin wrappers over tmux.py.

Real behavior tests (state detection, send routing, paste verification)
live in test_tmux.py. These test the session-layer wiring.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

from modastack.session import (
    _session_name,
    session_exists,
    load_skill,
    inject_skill,
    list_sessions,
    detect_state,
    capture,
    inject,
    kill_session,
)


class TestSessionName:

    def test_lowercase(self):
        assert _session_name("BET-42") == "moda-bet-42"

    def test_already_lowercase(self):
        assert _session_name("bet-42") == "moda-bet-42"


class TestSessionExists:

    @patch("modastack.session.has_session", return_value=True)
    def test_exists(self, mock_has):
        assert session_exists("BET-1") is True
        mock_has.assert_called_once_with("moda-bet-1")

    @patch("modastack.session.has_session", return_value=False)
    def test_not_exists(self, mock_has):
        assert session_exists("BET-1") is False


class TestLoadSkill:

    def test_loads_existing_skill(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "process"
        skill_dir = skills_dir / "pickup"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Pickup Skill\nDo the pickup.")
        monkeypatch.setattr("modastack.session.SKILLS_DIR", skills_dir)

        content = load_skill("pickup")
        assert "Pickup Skill" in content

    def test_returns_empty_for_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.session.SKILLS_DIR", tmp_path)
        assert load_skill("nonexistent") == ""


class TestInjectSkill:

    @patch("modastack.session.inject")
    def test_invokes_skill(self, mock_inject):
        inject_skill("BET-1", "pickup")
        mock_inject.assert_called_once_with("BET-1", "/pickup")

    @patch("modastack.session.inject")
    def test_invokes_skill_with_context(self, mock_inject):
        inject_skill("BET-1", "implement", context="spec at specs/bet-1.md")
        mock_inject.assert_called_once_with("BET-1", "/implement spec at specs/bet-1.md")


class TestListSessions:

    @patch("modastack.session.subprocess.run")
    def test_lists_moda_sessions(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="moda-bet-1\nmoda-bet-2\nmoda-manager\nother-session\n",
        )
        result = list_sessions()
        assert "BET-1" in result
        assert "BET-2" in result
        assert "MANAGER" in result

    @patch("modastack.session.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert list_sessions() == []


class TestDetectState:
    """detect_state collects data and delegates to determine_agent_state.
    The pure function is tested thoroughly in test_tmux.py.
    """

    @patch("modastack.session.has_session", return_value=False)
    def test_no_session_returns_exited(self, _):
        assert detect_state("BET-1") == {"state": "exited"}

    @patch("modastack.session.has_child_processes", return_value=True)
    @patch("modastack.session.get_pane_pid", return_value="1234")
    @patch("modastack.session.capture_pane", return_value="❯ \n  ⏵⏵ bypass permissions\n")
    @patch("modastack.session.has_session", return_value=True)
    def test_delegates_to_determine_agent_state(self, *_):
        result = detect_state("BET-1")
        assert result["state"] == "waiting_input"


class TestCapture:

    @patch("modastack.session.capture_pane", return_value="hello\nworld\n")
    def test_captures_lines(self, mock_cap):
        result = capture("BET-1", lines=20)
        assert result == "hello\nworld\n"
        mock_cap.assert_called_once_with("moda-bet-1", lines=20)


class TestInject:

    @patch("modastack.session.send_text", return_value=True)
    def test_delegates_to_send_text(self, mock_send):
        inject("BET-1", "hello world")
        mock_send.assert_called_once_with("moda-bet-1", "hello world")


class TestKillSession:

    @patch("modastack.session._tmux_kill")
    def test_kills_by_name(self, mock_kill):
        kill_session("BET-1")
        mock_kill.assert_called_once_with("moda-bet-1")
