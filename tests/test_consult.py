"""Unit tests for the ask endpoint and CLI command."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from modastack.cli import main


class TestAskEndpoint:
    """Tests for POST /api/ask in the dashboard."""

    @pytest.fixture
    def client(self):
        from dashboard.app import app
        from fastapi.testclient import TestClient
        return TestClient(app)

    @patch("modastack.manager.session.is_alive", return_value=True)
    @patch("modastack.manager.session.inject_capture", return_value=(True, "Use regex."))
    @patch("modastack.manager.session.last_inject_error", return_value="")
    def test_returns_response(self, mock_err, mock_inject, mock_alive, client):
        resp = client.post("/api/ask", json={
            "question": "regex or string matching?",
            "timeout": 60,
            "source": "test",
            "correlation_id": "abc-123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "regex" in data["response"].lower()
        assert data["correlation_id"] == "abc-123"
        mock_inject.assert_called_once()
        injected = mock_inject.call_args[0][0]
        assert "[QUESTION]" in injected

    @patch("modastack.manager.session.is_alive", return_value=False)
    def test_error_when_not_running(self, mock_alive, client):
        resp = client.post("/api/ask", json={"question": "hello?"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "not running" in data["error"]

    def test_error_on_empty_question(self, client):
        resp = client.post("/api/ask", json={"question": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "empty" in data["error"]

    @patch("modastack.manager.session.is_alive", return_value=True)
    @patch("modastack.manager.session.inject_capture", return_value=(False, ""))
    @patch("modastack.manager.session.last_inject_error", return_value="manager busy")
    def test_error_on_inject_failure(self, mock_err, mock_inject, mock_alive, client):
        resp = client.post("/api/ask", json={"question": "hello?"})
        data = resp.json()
        assert data["ok"] is False
        assert "inject failed" in data["error"]


class TestAskCLI:
    """Tests for the modastack ask CLI command (now uses inbox.deliver)."""

    @patch("modastack.inbox.deliver", return_value=(True, "Use regex for this case."))
    @patch("modastack.cli._resolve_address", return_value="moda-mgr-test")
    def test_prints_response_to_stdout(self, mock_resolve, mock_deliver):
        runner = CliRunner()
        result = runner.invoke(main, ["ask", "regex or string?"])
        assert result.exit_code == 0
        assert "Use regex" in result.output

    @patch("modastack.inbox.deliver", return_value=(False, "session not ready"))
    @patch("modastack.cli._resolve_address", return_value="moda-mgr-test")
    def test_exits_1_on_failure(self, mock_resolve, mock_deliver):
        runner = CliRunner()
        result = runner.invoke(main, ["ask", "hello?"])
        assert result.exit_code == 1
        assert "failed" in result.output.lower()

    @patch("modastack.cli._resolve_address", return_value=None)
    def test_exits_1_when_no_manager_session(self, mock_resolve):
        runner = CliRunner()
        result = runner.invoke(main, ["ask", "hello?"])
        assert result.exit_code == 1
        assert "no active manager" in result.output.lower()
