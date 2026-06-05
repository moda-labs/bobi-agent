"""Tests for ManagerSession.inject() — routes through inbox.deliver()."""

from pathlib import Path
from unittest.mock import patch

import pytest

from modastack.manager.session import ManagerSession


class TestInject:

    def setup_method(self):
        self.session = ManagerSession(repo_path=Path("/tmp/test-repo"))

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    def test_success_returns_true(self, mock_deliver):
        result = self.session.inject("hello manager")
        assert result is True
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args[0][0] == self.session.session_name
        assert mock_deliver.call_args[0][1] == "hello manager"

    @patch("modastack.inbox.deliver", return_value=(False, "session not found"))
    def test_failure_returns_false(self, mock_deliver):
        result = self.session.inject("hello")
        assert result is False

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    def test_uses_non_blocking_mode(self, mock_deliver):
        self.session.inject("test")
        assert mock_deliver.call_args[1]["wait"] is False

    @patch("modastack.inbox.deliver", return_value=(True, "the answer"))
    def test_inject_capture_returns_response(self, mock_deliver):
        ok, response = self.session.inject_capture("question?")
        assert ok is True
        assert response == "the answer"
        assert mock_deliver.call_args[1]["wait"] is True

    @patch("modastack.inbox.deliver", return_value=(False, "timeout"))
    def test_inject_capture_failure(self, mock_deliver):
        ok, response = self.session.inject_capture("question?")
        assert ok is False

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    def test_custom_timeout(self, mock_deliver):
        self.session.inject_capture("test", timeout=60)
        assert mock_deliver.call_args[1]["timeout"] == 60

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    def test_wait_for_ready_extends_timeout(self, mock_deliver):
        self.session.inject_capture("test", timeout=60, wait_for_ready=120)
        assert mock_deliver.call_args[1]["timeout"] == 120

    def test_detect_state_stopped_when_no_session(self):
        assert self.session.detect_state() == "stopped"

    def test_is_alive_false_when_no_session(self):
        assert self.session.is_alive() is False

    def test_last_inject_error_always_empty(self):
        assert self.session.last_inject_error() == ""
