"""Tests for manager session management."""

from pathlib import Path
from unittest.mock import patch, MagicMock

from manager.session import (
    _session_exists,
    _get_saved_session_id,
    _save_session_id,
    detect_state,
    capture,
    is_alive,
    SESSION_NAME,
)


class TestSessionExists:

    @patch("manager.session.subprocess.run")
    def test_returns_true_when_session_exists(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert _session_exists() is True

    @patch("manager.session.subprocess.run")
    def test_returns_false_when_no_session(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert _session_exists() is False


class TestSessionId:

    def test_save_and_load(self, tmp_path, monkeypatch):
        id_path = tmp_path / "session_id"
        monkeypatch.setattr("manager.session.SESSION_ID_PATH", id_path)

        _save_session_id("ses_abc123")
        assert _get_saved_session_id() == "ses_abc123"

    def test_load_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("manager.session.SESSION_ID_PATH", tmp_path / "nonexistent")
        assert _get_saved_session_id() == ""


class TestDetectState:

    @patch("manager.session._session_exists", return_value=False)
    def test_exited_when_no_session(self, _):
        assert detect_state() == "exited"

    @patch("manager.session.capture")
    @patch("manager.session._session_exists", return_value=True)
    def test_waiting_input(self, _, mock_capture):
        mock_capture.return_value = "\n".join([
            "Some output",
            "⏵⏵ bypass permissions",
            "❯ ",
        ])
        assert detect_state() == "waiting_input"

    @patch("manager.session.capture")
    @patch("manager.session._session_exists", return_value=True)
    def test_working_state(self, _, mock_capture):
        mock_capture.return_value = "\n".join([
            "Running analysis...",
            "Processing events...",
        ])
        assert detect_state() == "working"

    @patch("manager.session.capture")
    @patch("manager.session._session_exists", return_value=True)
    def test_unknown_on_empty(self, _, mock_capture):
        mock_capture.return_value = ""
        assert detect_state() == "unknown"

    @patch("manager.session.capture")
    @patch("manager.session._session_exists", return_value=True)
    def test_unknown_on_whitespace_only(self, _, mock_capture):
        mock_capture.return_value = "   \n  \n  "
        assert detect_state() == "unknown"


class TestCapture:

    @patch("manager.session.subprocess.run")
    def test_captures_pane_content(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="line1\nline2\n")
        result = capture(lines=10)
        assert result == "line1\nline2\n"
        cmd = mock_run.call_args[0][0]
        assert "-10" in cmd

    @patch("manager.session.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="can't find pane")
        result = capture(lines=10)
        assert result == ""


class TestIsAlive:

    @patch("manager.session._session_exists", return_value=True)
    def test_alive(self, _):
        assert is_alive() is True

    @patch("manager.session._session_exists", return_value=False)
    def test_not_alive(self, _):
        assert is_alive() is False
