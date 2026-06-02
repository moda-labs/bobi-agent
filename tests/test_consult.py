"""Unit tests for the consultation endpoint and CLI command."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from modastack.cli import main


class TestConsultEndpoint:
    """Tests for POST /api/consult in the dashboard."""

    @pytest.fixture
    def client(self):
        from dashboard.app import app
        from fastapi.testclient import TestClient
        return TestClient(app)

    @patch("modastack.manager.session.is_alive", return_value=True)
    @patch("modastack.manager.session.inject_capture", return_value=(True, "Use regex."))
    @patch("modastack.manager.session.last_inject_error", return_value="")
    def test_returns_response(self, mock_err, mock_inject, mock_alive, client):
        resp = client.post("/api/consult", json={
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
        assert "[CONSULTATION]" in injected

    @patch("modastack.manager.session.is_alive", return_value=False)
    def test_error_when_not_running(self, mock_alive, client):
        resp = client.post("/api/consult", json={"question": "hello?"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "not running" in data["error"]

    def test_error_on_empty_question(self, client):
        resp = client.post("/api/consult", json={"question": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "empty" in data["error"]

    @patch("modastack.manager.session.is_alive", return_value=True)
    @patch("modastack.manager.session.inject_capture", return_value=(False, ""))
    @patch("modastack.manager.session.last_inject_error", return_value="manager busy")
    def test_error_on_inject_failure(self, mock_err, mock_inject, mock_alive, client):
        resp = client.post("/api/consult", json={"question": "hello?"})
        data = resp.json()
        assert data["ok"] is False
        assert "inject failed" in data["error"]


class TestConsultCLI:
    """Tests for the modastack consult CLI command."""

    @patch("urllib.request.urlopen")
    def test_prints_response_to_stdout(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "response": "Use regex for this case.",
            "correlation_id": "test-123",
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        with patch("os.kill"):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value="12345"):
                    result = runner.invoke(main, ["consult", "regex or string?"])

        assert result.exit_code == 0
        assert "Use regex" in result.output

    @patch("urllib.request.urlopen")
    def test_exits_1_on_failure(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": False,
            "error": "manager not running",
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        with patch("os.kill"):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value="12345"):
                    result = runner.invoke(main, ["consult", "hello?"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower() or "failed" in (result.output + getattr(result, 'stderr', '')).lower()

    def test_exits_1_when_manager_not_running(self):
        runner = CliRunner()
        with patch("pathlib.Path.exists", return_value=False):
            result = runner.invoke(main, ["consult", "hello?"])
        assert result.exit_code == 1
        assert "not running" in result.output.lower()
