"""Extended tests for modastack/session.py — functions not covered by test_session.py."""

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

    @patch("modastack.session.subprocess.run")
    def test_exists(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert session_exists("BET-1") is True
        assert "moda-bet-1" in str(mock_run.call_args)

    @patch("modastack.session.subprocess.run")
    def test_not_exists(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
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

    @patch("modastack.session.subprocess.run")
    def test_no_sessions(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="other-session\n")
        assert list_sessions() == []


class TestDetectStateExtended:

    @patch("modastack.session.subprocess.run")
    def test_waiting_input_with_bypass(self, mock_run):
        """Both ❯ and bypass permissions present → waiting_input."""
        pane = "\n".join([
            "Output done",
            "⏵⏵ bypass permissions",
            "❯ ",
        ])

        def side_effect(cmd, **kw):
            cmd_str = " ".join(cmd)
            if "has-session" in cmd_str:
                return MagicMock(returncode=0)
            if "capture-pane" in cmd_str:
                return MagicMock(stdout=pane)
            return MagicMock(returncode=0, stdout="")
        mock_run.side_effect = side_effect

        result = detect_state("BET-1")
        assert result["state"] == "waiting_input"

    @patch("modastack.session.subprocess.run")
    def test_no_session_returns_exited(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        result = detect_state("BET-1")
        assert result == {"state": "exited"}


class TestCapture:

    @patch("modastack.session.subprocess.run")
    def test_captures_lines(self, mock_run):
        mock_run.return_value = MagicMock(stdout="hello\nworld\n")
        result = capture("BET-1", lines=20)
        assert result == "hello\nworld\n"


class TestInject:

    @patch("modastack.session.time.sleep")
    @patch("modastack.session.subprocess.run")
    def test_collapses_multiline(self, mock_run, mock_sleep):
        inject("BET-1", "line one\nline two\nline three")
        # First call is send-keys with text, should be collapsed
        text_call = mock_run.call_args_list[0]
        assert "line one line two line three" in str(text_call)

    @patch("modastack.session.time.sleep")
    @patch("modastack.session.subprocess.run")
    def test_sends_enter_keys(self, mock_run, mock_sleep):
        inject("BET-1", "test")
        enter_calls = [c for c in mock_run.call_args_list if "Enter" in str(c)]
        assert len(enter_calls) == 2  # Two Enter presses


class TestKillSession:

    @patch("modastack.session.subprocess.run")
    def test_kills_by_name(self, mock_run):
        kill_session("BET-1")
        cmd = mock_run.call_args[0][0]
        assert "kill-session" in cmd
        assert "moda-bet-1" in cmd
