"""Unit tests for the doctor bubble auth check (_check_bubble_auth)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bobi.doctor import _check_bubble_auth


@pytest.fixture
def tmp_project(tmp_path):
    """Set up a minimal project structure for doctor checks."""
    bobi_dir = tmp_path / ".bobi"
    bobi_dir.mkdir()
    state_dir = bobi_dir / "state"
    state_dir.mkdir()
    # Minimal agent.yaml so Config.load works
    (bobi_dir / "agent.yaml").write_text("agent: test\n")
    return tmp_path


def _write_bubble(project: Path, bubble_id: str = "", bubble_key: str = ""):
    state_file = project / ".bobi" / "state" / "bubble.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if bubble_id:
        data["bubble_id"] = bubble_id
    if bubble_key:
        data["bubble_key"] = bubble_key
    state_file.write_text(json.dumps(data))


class TestCheckBubbleAuth:
    def test_no_project_returns_ok(self):
        with patch("bobi.doctor.bound_root", return_value=None):
            result = _check_bubble_auth()
        assert result.ok is True
        assert "no project" in result.detail

    def test_no_bubble_not_started(self, tmp_project):
        with patch("bobi.doctor.bound_root", return_value=tmp_project):
            result = _check_bubble_auth()
        assert result.ok is True
        assert "not started" in result.detail

    def test_no_bubble_but_server_running(self, tmp_project):
        state_dir = tmp_project / ".bobi" / "state"
        (state_dir / "event-server.pid").write_text("12345")
        with patch("bobi.doctor.bound_root", return_value=tmp_project):
            result = _check_bubble_auth()
        assert result.ok is False
        assert "no bubble credential" in result.detail

    def test_bubble_id_present_key_missing(self, tmp_project):
        _write_bubble(tmp_project, bubble_id="bub_abc123", bubble_key="")
        with patch("bobi.doctor.bound_root", return_value=tmp_project):
            result = _check_bubble_auth()
        assert result.ok is False
        assert "bubble_key missing" in result.detail

    def test_healthy_bubble(self, tmp_project):
        _write_bubble(tmp_project, bubble_id="bub_abc123def456", bubble_key="bkey_secret")
        with patch("bobi.doctor.bound_root", return_value=tmp_project):
            result = _check_bubble_auth()
        assert result.ok is True
        assert "bub_abc123def456" in result.detail
        assert "key present" in result.detail
        assert "bkey_secret" not in result.detail

    def test_remote_cleartext_url_warns(self, tmp_project):
        _write_bubble(tmp_project, bubble_id="bub_test", bubble_key="bkey_test")
        (tmp_project / ".bobi" / "agent.yaml").write_text(
            "agent: test\nevent_server_url: http://remote-host.example.com:8080\n"
        )
        with patch("bobi.doctor.bound_root", return_value=tmp_project):
            result = _check_bubble_auth()
        assert result.ok is False
        assert "cleartext" in result.detail
        assert "https://" in result.hint

    def test_remote_tls_url_ok(self, tmp_project):
        _write_bubble(tmp_project, bubble_id="bub_test", bubble_key="bkey_test")
        (tmp_project / ".bobi" / "agent.yaml").write_text(
            "agent: test\nevent_server_url: https://events.example.com\n"
        )
        with patch("bobi.doctor.bound_root", return_value=tmp_project):
            result = _check_bubble_auth()
        assert result.ok is True

    def test_localhost_url_ok(self, tmp_project):
        _write_bubble(tmp_project, bubble_id="bub_test", bubble_key="bkey_test")
        (tmp_project / ".bobi" / "agent.yaml").write_text(
            "agent: test\nevent_server_url: http://localhost:8080\n"
        )
        with patch("bobi.doctor.bound_root", return_value=tmp_project):
            result = _check_bubble_auth()
        assert result.ok is True

    def test_health_never_leaks_key(self, tmp_project):
        _write_bubble(tmp_project, bubble_id="bub_test", bubble_key="bkey_supersecret")
        with patch("bobi.doctor.bound_root", return_value=tmp_project):
            result = _check_bubble_auth()
        assert "bkey_supersecret" not in result.detail
        assert "bkey_supersecret" not in result.hint
