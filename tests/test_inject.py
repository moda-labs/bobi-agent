"""Tests for session inject() — state checks, async scheduling, timeout."""

import asyncio
import threading
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from modastack.manager import session


class TestInject:

    def setup_method(self):
        self._orig_client = session._client
        self._orig_loop = session._loop
        self._orig_state = session._state

    def teardown_method(self):
        session._client = self._orig_client
        session._loop = self._orig_loop
        session._state = self._orig_state

    def test_returns_false_when_no_client(self):
        session._client = None
        session._loop = MagicMock()
        session._state = "waiting_input"
        assert session.inject("test") is False

    def test_returns_false_when_no_loop(self):
        session._client = MagicMock()
        session._loop = None
        session._state = "waiting_input"
        assert session.inject("test") is False

    def test_returns_false_when_not_waiting_input(self):
        session._client = MagicMock()
        session._loop = MagicMock()
        session._state = "working"
        assert session.inject("test") is False

    def test_returns_false_when_stopped(self):
        session._client = MagicMock()
        session._loop = MagicMock()
        session._state = "stopped"
        assert session.inject("test") is False

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_success_returns_true(self, mock_schedule, mock_log):
        session._client = MagicMock()
        loop = asyncio.new_event_loop()
        session._loop = loop
        session._state = "waiting_input"

        future = MagicMock()
        future.result.return_value = None
        mock_schedule.return_value = future

        result = session.inject("hello manager")
        assert result is True
        mock_schedule.assert_called_once()
        future.result.assert_called_once_with(timeout=300)
        loop.close()

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_timeout_returns_false(self, mock_schedule, mock_log):
        session._client = MagicMock()
        loop = asyncio.new_event_loop()
        session._loop = loop
        session._state = "waiting_input"

        future = MagicMock()
        future.result.side_effect = TimeoutError("timed out")
        mock_schedule.return_value = future

        result = session.inject("hello", timeout=5)
        assert result is False
        loop.close()

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_custom_timeout(self, mock_schedule, mock_log):
        session._client = MagicMock()
        loop = asyncio.new_event_loop()
        session._loop = loop
        session._state = "waiting_input"

        future = MagicMock()
        future.result.return_value = None
        mock_schedule.return_value = future

        session.inject("test", timeout=60)
        future.result.assert_called_once_with(timeout=60)
        loop.close()

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_exception_returns_false(self, mock_schedule, mock_log):
        session._client = MagicMock()
        loop = asyncio.new_event_loop()
        session._loop = loop
        session._state = "waiting_input"

        future = MagicMock()
        future.result.side_effect = RuntimeError("connection lost")
        mock_schedule.return_value = future

        result = session.inject("test")
        assert result is False
        loop.close()
