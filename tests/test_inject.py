"""Tests for ManagerSession.inject() — state checks, async scheduling, timeout."""

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from modastack.manager.session import ManagerSession


class TestInject:

    def setup_method(self):
        self.session = ManagerSession(repo_path=Path("/tmp/test-repo"))

    def test_returns_false_when_no_client(self):
        self.session._client = None
        self.session._loop = MagicMock()
        self.session._state = "waiting_input"
        assert self.session.inject("test") is False

    def test_returns_false_when_no_loop(self):
        self.session._client = MagicMock()
        self.session._loop = None
        self.session._state = "waiting_input"
        assert self.session.inject("test") is False

    def test_returns_false_when_not_waiting_input(self):
        self.session._client = MagicMock()
        self.session._loop = MagicMock()
        self.session._state = "working"
        assert self.session.inject("test") is False

    def test_returns_false_when_stopped(self):
        self.session._client = MagicMock()
        self.session._loop = MagicMock()
        self.session._state = "stopped"
        assert self.session.inject("test") is False

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_success_returns_true(self, mock_schedule, mock_log):
        self.session._client = MagicMock()
        loop = asyncio.new_event_loop()
        self.session._loop = loop
        self.session._state = "waiting_input"

        future = MagicMock()
        future.result.return_value = None
        mock_schedule.return_value = future

        result = self.session.inject("hello manager")
        assert result is True
        mock_schedule.assert_called_once()
        future.result.assert_called_once_with(timeout=300)
        loop.close()

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_timeout_returns_false(self, mock_schedule, mock_log):
        self.session._client = MagicMock()
        loop = asyncio.new_event_loop()
        self.session._loop = loop
        self.session._state = "waiting_input"

        future = MagicMock()
        future.result.side_effect = TimeoutError("timed out")
        mock_schedule.return_value = future

        result = self.session.inject("hello", timeout=5)
        assert result is False
        loop.close()

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_custom_timeout(self, mock_schedule, mock_log):
        self.session._client = MagicMock()
        loop = asyncio.new_event_loop()
        self.session._loop = loop
        self.session._state = "waiting_input"

        future = MagicMock()
        future.result.return_value = None
        mock_schedule.return_value = future

        self.session.inject("test", timeout=60)
        future.result.assert_called_once_with(timeout=60)
        loop.close()

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_exception_returns_false(self, mock_schedule, mock_log):
        self.session._client = MagicMock()
        loop = asyncio.new_event_loop()
        self.session._loop = loop
        self.session._state = "waiting_input"

        future = MagicMock()
        future.result.side_effect = RuntimeError("connection lost")
        mock_schedule.return_value = future

        result = self.session.inject("test")
        assert result is False
        loop.close()

    def test_busy_drops_immediately_by_default(self):
        self.session._client = MagicMock()
        self.session._loop = MagicMock()
        self.session._state = "working"
        assert self.session.inject("test") is False
        assert "busy" in self.session.last_inject_error()

    @patch("modastack.manager.session.time.sleep")
    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_waits_for_busy_manager_then_injects(self, mock_schedule, mock_log, mock_sleep):
        self.session._client = MagicMock()
        loop = asyncio.new_event_loop()
        self.session._loop = loop
        self.session._state = "working"

        future = MagicMock()
        future.result.return_value = None
        mock_schedule.return_value = future

        def _flip(_seconds):
            self.session._state = "waiting_input"
        mock_sleep.side_effect = _flip

        result = self.session.inject("hello", wait_for_ready=5)
        assert result is True
        mock_sleep.assert_called()
        mock_schedule.assert_called_once()
        loop.close()

    def test_stopped_short_circuits_wait(self):
        self.session._client = MagicMock()
        self.session._loop = MagicMock()
        self.session._state = "stopped"
        assert self.session.inject("test", wait_for_ready=10) is False
        assert "stopped" in self.session.last_inject_error()

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_success_clears_last_error(self, mock_schedule, mock_log):
        self.session._client = MagicMock()
        loop = asyncio.new_event_loop()
        self.session._loop = loop
        self.session._state = "waiting_input"

        future = MagicMock()
        future.result.return_value = None
        mock_schedule.return_value = future

        assert self.session.inject("hi") is True
        assert self.session.last_inject_error() == ""
        loop.close()
