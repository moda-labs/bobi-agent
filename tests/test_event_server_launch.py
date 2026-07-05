"""Event-server launch: npm-failure surfacing and remote-URL guard.

The v0.14.1 release gate failed inside `npm install` with
capture_output=True — the CalledProcessError carried no output, so the
manager.log showed a bare traceback and diagnosing the real cause
(ENOSPC) required SSHing to the runner and re-running npm by hand.

Containerized instances (#336) must never start Node when
``event_server_url`` points to a remote server.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bobi import paths
from bobi.events import server as es


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


def test_existing_node_modules_without_esbuild_uses_npm_exec(tmp_path, monkeypatch):
    es_dir = tmp_path / "event-server"
    (es_dir / "node_modules").mkdir(parents=True)
    (es_dir / "src").mkdir()
    (es_dir / "src" / "local.ts").write_text("console.log('ok')\n")
    (es_dir / "package.json").write_text("{}")
    paths.state_dir(tmp_path).mkdir(parents=True, exist_ok=True)

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    health_calls = {"count": 0}

    def fake_health(*args, **kwargs):
        health_calls["count"] += 1
        if health_calls["count"] == 1:
            return None
        return {"status": "ok"}

    class FakePopen:
        pid = 12345

    monkeypatch.setattr(es.subprocess, "run", fake_run)
    monkeypatch.setattr(es.subprocess, "Popen", lambda *a, **k: FakePopen())
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "health", fake_health)

    result = es.ensure_running(8080, project_path=tmp_path)

    assert result == "started"
    assert calls[0][:6] == [
        "npm", "exec", "--yes", "--cache", str(es_dir / ".npm-cache"), "--package"
    ]


def test_setup_webhook_secrets_are_forwarded_to_local_server(tmp_path, monkeypatch):
    es_dir = tmp_path / "event-server"
    (es_dir / "dist").mkdir(parents=True)
    dist = es_dir / "dist" / "local.js"
    dist.write_text("console.log('ok')\n")
    (es_dir / "src").mkdir()
    (es_dir / "src" / "local.ts").write_text("console.log('ok')\n")
    os.utime(dist, (dist.stat().st_atime, dist.stat().st_mtime + 1))
    (es_dir / "node_modules").mkdir()
    (es_dir / "package.json").write_text("{}")
    paths.state_dir(tmp_path).mkdir(parents=True, exist_ok=True)

    captured: dict = {}
    health_calls = {"count": 0}

    def fake_health(*args, **kwargs):
        health_calls["count"] += 1
        if health_calls["count"] == 1:
            return None
        return {"status": "ok"}

    class FakePopen:
        pid = 12345

    def fake_popen(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return FakePopen()

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "slack-secret")
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "linear-secret")
    monkeypatch.setattr(es.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "health", fake_health)

    result = es.ensure_running(8080, project_path=tmp_path)

    assert result == "started"
    env = captured["env"]
    assert "BOBI_ES_WEBHOOK_SECRET" not in env
    assert env["BOBI_ES_SLACK_SIGNING_SECRET"] == "slack-secret"
    assert env["BOBI_ES_LINEAR_WEBHOOK_SECRET"] == "linear-secret"


def test_explicit_webhook_secrets_override_setup_env(tmp_path, monkeypatch):
    es_dir = tmp_path / "event-server"
    (es_dir / "dist").mkdir(parents=True)
    dist = es_dir / "dist" / "local.js"
    dist.write_text("console.log('ok')\n")
    (es_dir / "src").mkdir()
    (es_dir / "src" / "local.ts").write_text("console.log('ok')\n")
    os.utime(dist, (dist.stat().st_atime, dist.stat().st_mtime + 1))
    (es_dir / "node_modules").mkdir()
    (es_dir / "package.json").write_text("{}")
    paths.state_dir(tmp_path).mkdir(parents=True, exist_ok=True)

    captured: dict = {}
    health_calls = {"count": 0}

    def fake_health(*args, **kwargs):
        health_calls["count"] += 1
        if health_calls["count"] == 1:
            return None
        return {"status": "ok"}

    class FakePopen:
        pid = 12345

    def fake_popen(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return FakePopen()

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "ambient-slack")
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "ambient-linear")
    monkeypatch.setattr(es.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "health", fake_health)

    result = es.ensure_running(
        8080,
        webhook_secret="explicit-github",
        slack_signing_secret="",
        linear_webhook_secret="explicit-linear",
        project_path=tmp_path,
    )

    assert result == "started"
    env = captured["env"]
    assert env["BOBI_ES_WEBHOOK_SECRET"] == "explicit-github"
    assert "BOBI_ES_SLACK_SIGNING_SECRET" not in env
    assert env["BOBI_ES_LINEAR_WEBHOOK_SECRET"] == "explicit-linear"


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
        "https://bobi-events.example.workers.dev",
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
        paths.package_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        paths.agent_yaml_path(tmp_path).write_text(
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


# ---------------------------------------------------------------------------
# Slack workspace registration signing (#487)
# ---------------------------------------------------------------------------

class _StubCfg:
    """Minimal Config stand-in exposing the credentials used by registration."""

    def __init__(self, creds):
        self._creds = creds

    def credential(self, service, key):
        try:
            return self._creds[(service, key)]
        except KeyError as exc:
            raise RuntimeError(f"no credential {service}.{key}") from exc


def _capture_post(monkeypatch):
    """Patch pooled.post + Slack lookups; return a dict the call records into."""
    import bobi.http as pooled

    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

        return _Resp()

    monkeypatch.setattr(pooled, "post", fake_post)
    monkeypatch.setattr(es, "_slack_auth_info", lambda token: ("T_TEAM", "B_BOT", "U_BOT"))
    monkeypatch.setattr(es, "_slack_app_id", lambda token, bot_id: "A_APP")
    return captured


def test_slack_registration_signs_when_bubble_key_present(monkeypatch):
    """A bubble-keyed registration carries x-moda-* headers and sends raw bytes
    (content=), so the server reproduces the HMAC and writes the bubble-scoped
    record outbound /slack/send needs (#487)."""
    captured = _capture_post(monkeypatch)
    cfg = _StubCfg({("slack", "bot_token"): "xoxb-tok",
                    ("slack", "signing_secret"): "sek"})

    result = es.register_slack_workspaces(
        "http://localhost:8080", cfg,
        bubble_id="bub_A", bubble_key="bkey_A",
    )

    assert result == ["T_TEAM"]
    kwargs = captured["kwargs"]
    # Raw bytes, never json= (which would re-serialize and break the signature).
    assert "content" in kwargs and "json" not in kwargs
    headers = kwargs["headers"]
    assert headers["x-moda-bubble"] == "bub_A"
    assert headers["x-moda-algo"] == "hmac-sha256"
    assert headers["x-moda-signature"]

    # The signature must verify over the EXACT transmitted bytes.
    from bobi.events.signing import canonical_string
    import hashlib
    import hmac

    body = json.loads(kwargs["content"])
    assert body["bot_user_id"] == "U_BOT"

    msg = canonical_string(
        headers["x-moda-timestamp"], headers["x-moda-nonce"],
        "POST", "/slack/workspaces", kwargs["content"],
    )
    expected = hmac.new(b"bkey_A", msg.encode(), hashlib.sha256).hexdigest()
    assert headers["x-moda-signature"] == expected


def test_slack_registration_unsigned_without_bubble_key(monkeypatch):
    """Without a bubble key the registration is unsigned — it still writes the
    global self-reply record, so loop prevention keeps working for legacy
    clients."""
    captured = _capture_post(monkeypatch)
    cfg = _StubCfg({("slack", "bot_token"): "xoxb-tok",
                    ("slack", "signing_secret"): ""})

    es.register_slack_workspaces("http://localhost:8080", cfg)

    headers = captured["kwargs"]["headers"]
    assert "x-moda-signature" not in headers
    # Still raw bytes via content= (the body is serialized once regardless).
    assert "content" in captured["kwargs"]
    body = json.loads(captured["kwargs"]["content"])
    assert body["bot_user_id"] == "U_BOT"
