"""Event-server launch: npm-failure surfacing and remote-URL guard.

The v0.14.1 release gate failed inside `npm install` with
capture_output=True — the CalledProcessError carried no output, so the
manager.log showed a bare traceback and diagnosing the real cause
(ENOSPC) required SSHing to the runner and re-running npm by hand.

Containerized instances (#336) must never start Node when
``event_server_url`` points to a remote server.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from modastack.events import server as es


def test_npm_failure_surfaces_stderr(tmp_path, monkeypatch, caplog):
    es_dir = tmp_path / "event-server"
    es_dir.mkdir()
    (es_dir / "package.json").write_text("{}")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="",
            stderr="npm warn tar TAR_ENTRY_ERROR ENOSPC: no space left on device",
        )

    monkeypatch.setattr(es.subprocess, "run", fake_run)
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "health", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="ENOSPC"):
        es.ensure_running(8080, project_path=tmp_path)


# ── Remote-URL guard (containerized-6) ──────────────────────────────


class TestIsLocalUrl:
    """Unit tests for _is_local_url."""

    @pytest.mark.parametrize("url", [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "http://localhost",
    ])
    def test_local_urls(self, url):
        assert es._is_local_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://events.example.com",
        "https://modastack-events.example.workers.dev",
        "http://10.0.0.5:8080",
        "http://event-server.internal:8080",
    ])
    def test_remote_urls(self, url):
        assert es._is_local_url(url) is False

    def test_empty_url(self):
        assert es._is_local_url("") is True


class TestEnsureRunningRemoteGuard:
    """ensure_running must refuse to start Node when event_server_url is remote."""

    def _write_agent_yaml(self, tmp_path, url):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agent.yaml").write_text(
            f"agent: test\nevent_server_url: {url}\n"
        )

    def test_remote_url_returns_skipped(self, tmp_path, monkeypatch):
        self._write_agent_yaml(tmp_path, "https://events.example.com")
        # Should never reach health check or npm
        monkeypatch.setattr(es, "health", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("health should not be called")))
        result = es.ensure_running(8080, project_path=tmp_path)
        assert result == "skipped"

    def test_local_url_not_blocked(self, tmp_path, monkeypatch):
        """A localhost event_server_url should not trigger the guard."""
        self._write_agent_yaml(tmp_path, "http://localhost:8080")
        monkeypatch.setattr(es, "health", lambda *a, **k: {"status": "ok"})
        result = es.ensure_running(8080, project_path=tmp_path)
        assert result == "connected"

    def test_no_config_not_blocked(self, tmp_path, monkeypatch):
        """No agent.yaml → no remote URL → should proceed normally."""
        monkeypatch.setattr(es, "health", lambda *a, **k: {"status": "ok"})
        result = es.ensure_running(8080, project_path=tmp_path)
        assert result == "connected"
