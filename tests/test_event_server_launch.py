"""Event-server artifact launch, source rebuild, and remote-URL coverage."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from bobi import paths
from bobi.events import artifact
from bobi.events import server as es


def _output_entry(data: bytes) -> dict[str, int | str]:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _write_valid_artifact(es_dir: Path) -> None:
    es_dir.mkdir(parents=True, exist_ok=True)
    (es_dir / "package.json").write_text("{}")
    dist = es_dir / "dist"
    dist.mkdir()
    bundle = b"console.log('packaged')\n"
    notice = artifact.notice_bytes()
    (dist / artifact.BUNDLE_NAME).write_bytes(bundle)
    (dist / artifact.NOTICE_NAME).write_bytes(notice)
    manifest = {
        "bundled_dependencies": [
            dependency.manifest_entry()
            for dependency in artifact.AUDITED_DEPENDENCIES
        ],
        "inputs": {},
        "outputs": {
            artifact.BUNDLE_NAME: _output_entry(bundle),
            artifact.NOTICE_NAME: _output_entry(notice),
        },
        "schema_version": artifact.SCHEMA_VERSION,
        "tools": {"esbuild": "0.25.12", "node": "v20.19.2", "npm": "9.2.0"},
    }
    (dist / artifact.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def _started_health():
    calls = {"count": 0}

    def fake_health(*args, **kwargs):
        calls["count"] += 1
        return None if calls["count"] == 1 else {"status": "ok"}

    return fake_health


def test_npm_failure_surfaces_stderr(tmp_path, monkeypatch, caplog):
    es_dir = tmp_path / "event-server"
    es_dir.mkdir()

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="",
            stderr="npm warn tar TAR_ENTRY_ERROR ENOSPC: no space left on device",
        )

    monkeypatch.setattr(es.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="ENOSPC"):
        es._run_npm(["npm", "ci"], es_dir)


def test_installed_artifact_spawns_directly_with_sanitized_environment(
    tmp_path, monkeypatch,
):
    es_dir = tmp_path / "event-server"
    _write_valid_artifact(es_dir)
    paths.state_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    captured: dict = {}

    class FakePopen:
        pid = 12345

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakePopen()

    monkeypatch.setenv("NODE_OPTIONS", "--require=/tmp/hostile.js")
    monkeypatch.setenv("NODE_PATH", "/tmp/hostile-modules")
    monkeypatch.setattr(es, "_is_installed_event_server_dir", lambda path: True)
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "resolve_node_runtime", lambda: ("/node20", "v20.19.2"))
    monkeypatch.setattr(
        es,
        "_run_npm",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("installed startup must not invoke npm")
        ),
    )
    monkeypatch.setattr(es.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(es, "health", _started_health())

    result = es.ensure_running(
        8080,
        project_path=tmp_path,
        extra_env={"NODE_OPTIONS": "--require=extra.js", "NODE_PATH": "/extra"},
    )

    assert result == "started"
    assert captured["args"] == ["/node20", str(es_dir / "dist" / "local.js")]
    env = captured["env"]
    assert "NODE_OPTIONS" not in env
    assert "NODE_PATH" not in env
    assert env["WS_NO_BUFFER_UTIL"] == "1"
    assert env["WS_NO_UTF_8_VALIDATE"] == "1"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-bundle",
        "empty-bundle",
        "missing-manifest",
        "empty-manifest",
        "malformed-manifest",
        "non-utf8-manifest",
        "missing-notice",
        "empty-notice",
        "bundle-mismatch",
        "notice-mismatch",
    ],
)
def test_installed_artifact_failure_is_actionable_and_never_repairs(
    tmp_path, monkeypatch, mutation,
):
    es_dir = tmp_path / "event-server"
    _write_valid_artifact(es_dir)
    dist = es_dir / "dist"
    if mutation == "missing-bundle":
        (dist / artifact.BUNDLE_NAME).unlink()
    elif mutation == "empty-bundle":
        (dist / artifact.BUNDLE_NAME).write_bytes(b"")
    elif mutation == "missing-manifest":
        (dist / artifact.MANIFEST_NAME).unlink()
    elif mutation == "empty-manifest":
        (dist / artifact.MANIFEST_NAME).write_bytes(b"")
    elif mutation == "empty-notice":
        (dist / artifact.NOTICE_NAME).write_bytes(b"")
    elif mutation == "malformed-manifest":
        (dist / artifact.MANIFEST_NAME).write_text("{")
    elif mutation == "non-utf8-manifest":
        (dist / artifact.MANIFEST_NAME).write_bytes(b"\x80")
    elif mutation == "missing-notice":
        (dist / artifact.NOTICE_NAME).unlink()
    elif mutation == "bundle-mismatch":
        (dist / artifact.BUNDLE_NAME).write_bytes(b"tampered")
    else:
        (dist / artifact.NOTICE_NAME).write_bytes(b"tampered")

    monkeypatch.setattr(es, "_is_installed_event_server_dir", lambda path: True)
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "health", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        es,
        "resolve_node_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("artifact validation must precede Node")
        ),
    )
    monkeypatch.setattr(
        es,
        "_run_npm",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("installed startup must not invoke npm")
        ),
    )

    with pytest.raises(
        es.PackagedEventServerArtifactError,
        match=r"incomplete or corrupt.*Reinstall or upgrade",
    ):
        es.ensure_running(8080, project_path=tmp_path)


def test_setup_webhook_secrets_are_forwarded_to_local_server(tmp_path, monkeypatch):
    es_dir = tmp_path / "event-server"
    _write_valid_artifact(es_dir)
    paths.state_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    captured: dict = {}

    class FakePopen:
        pid = 12345

    def fake_popen(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return FakePopen()

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "slack-secret")
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "linear-secret")
    monkeypatch.setattr(es, "_is_installed_event_server_dir", lambda path: True)
    monkeypatch.setattr(es.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "health", _started_health())
    monkeypatch.setattr(es, "resolve_node_runtime", lambda: ("/node20", "v20.19.2"))

    result = es.ensure_running(8080, project_path=tmp_path)

    assert result == "started"
    env = captured["env"]
    assert "BOBI_ES_WEBHOOK_SECRET" not in env
    assert env["BOBI_ES_SLACK_SIGNING_SECRET"] == "slack-secret"
    assert env["BOBI_ES_LINEAR_WEBHOOK_SECRET"] == "linear-secret"


def test_explicit_webhook_secrets_override_setup_environment(
    tmp_path, monkeypatch,
):
    es_dir = tmp_path / "event-server"
    _write_valid_artifact(es_dir)
    paths.state_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    captured: dict = {}

    class FakePopen:
        pid = 12345

    def fake_popen(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return FakePopen()

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "ambient-slack")
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "ambient-linear")
    monkeypatch.setattr(es, "_is_installed_event_server_dir", lambda path: True)
    monkeypatch.setattr(es.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "health", _started_health())
    monkeypatch.setattr(es, "resolve_node_runtime", lambda: ("/node20", "v20.19.2"))

    result = es.ensure_running(
        8080,
        webhook_secret="explicit-github",
        slack_signing_secret="",
        linear_webhook_secret="explicit-linear",
        project_path=tmp_path,
    )

    assert result == "started"
    environment = captured["env"]
    assert environment["BOBI_ES_WEBHOOK_SECRET"] == "explicit-github"
    assert "BOBI_ES_SLACK_SIGNING_SECRET" not in environment
    assert environment["BOBI_ES_LINEAR_WEBHOOK_SECRET"] == "explicit-linear"


def test_node_and_npm_probes_receive_only_sanitized_environment(
    tmp_path, monkeypatch,
):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs["env"]))
        output = "v20.19.2\n" if args[-1] == "--version" and "node" in args[0] else ""
        if args[:2] == ["npm", "ls"]:
            output = "{}"
        elif args == ["npm", "--version"]:
            output = "9.2.0\n"
        return subprocess.CompletedProcess(args, returncode=0, stdout=output, stderr="")

    monkeypatch.setenv("NODE_OPTIONS", "--require=/tmp/hostile.cjs")
    monkeypatch.setenv("NODE_PATH", "/tmp/hostile-modules")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "provider-secret")
    monkeypatch.setenv("npm_config_node_options", "--require=/tmp/other.cjs")
    monkeypatch.setenv("npm_config_script_shell", "/tmp/hostile-shell")
    monkeypatch.setattr(es.shutil, "which", lambda name: f"/safe/{name}")
    monkeypatch.setattr(es.subprocess, "run", fake_run)

    es.resolve_node_runtime()
    for command in (
        ["npm", "ls", "--all", "--json", "--offline"],
        ["npm", "ci", "--no-audit", "--no-fund"],
        ["npm", "run", "build:local"],
        ["npm", "--version"],
    ):
        es._run_npm(command, tmp_path)

    assert [args for args, _ in calls] == [
        ["/safe/node", "--version"],
        ["npm", "ls", "--all", "--json", "--offline"],
        ["npm", "ci", "--no-audit", "--no-fund"],
        ["npm", "run", "build:local"],
        ["npm", "--version"],
    ]
    for _, environment in calls:
        assert environment["PATH"]
        assert "NODE_OPTIONS" not in environment
        assert "NODE_PATH" not in environment
        assert "SLACK_BOT_TOKEN" not in environment
        assert "npm_config_node_options" not in environment
        assert "npm_config_script_shell" not in environment


@pytest.mark.parametrize(
    ("which_result", "version", "match"),
    [
        (None, "", r"not found on PATH.*Node\.js 20\+"),
        ("/node", "v18.20.0", r"requires Node\.js 20\+.*v18\.20\.0"),
    ],
)
def test_node_runtime_prerequisite_is_actionable(
    monkeypatch, which_result, version, match,
):
    monkeypatch.setattr(
        es.shutil,
        "which",
        lambda name: which_result if name == "node" else None,
    )
    monkeypatch.setattr(
        es.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], returncode=0, stdout=version, stderr=""
        ),
    )

    with pytest.raises(es.NodeRuntimePrerequisiteError, match=match):
        es.resolve_node_runtime()


def _write_source_dependency_state(
    es_dir: Path, tree: dict,
) -> None:
    (es_dir / "node_modules" / "@moda-labs" / "bobi-events-core").mkdir(
        parents=True
    )
    esbuild = es_dir / "node_modules" / ".bin" / "esbuild"
    esbuild.parent.mkdir(parents=True, exist_ok=True)
    esbuild.write_text("#!/bin/sh\n")
    esbuild.chmod(0o755)
    (es_dir / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    stamp = {
        "installed_tree_sha256": artifact.canonical_json_digest(tree),
        "lockfile_sha256": artifact.file_sha256(es_dir / "package-lock.json"),
        "schema_version": 1,
    }
    (es_dir / "node_modules" / es.DEPENDENCY_STAMP_NAME).write_text(
        json.dumps(stamp)
    )


def test_source_dependency_stamp_requires_exact_lock_and_offline_tree(
    tmp_path, monkeypatch,
):
    tree = {
        "dependencies": {
            "@moda-labs/bobi-events-core": {"version": "0.1.0"},
            "esbuild": {"version": "0.25.12"},
        },
        "name": "bobi-event-server",
    }
    _write_source_dependency_state(tmp_path, tree)
    calls = []

    def run_npm(args, path):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, returncode=0, stdout=json.dumps(tree), stderr=""
        )

    monkeypatch.setattr(es, "_run_npm", run_npm)

    assert es._source_dependencies_valid(tmp_path)
    assert calls == [["npm", "ls", "--all", "--json", "--offline"]]

    (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 2}\n')
    assert not es._source_dependencies_valid(tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_source_dependency_stamp_schema_requires_an_exact_integer(
    tmp_path, monkeypatch, schema_version,
):
    tree = {"name": "bobi-event-server"}
    _write_source_dependency_state(tmp_path, tree)
    stamp_path = tmp_path / "node_modules" / es.DEPENDENCY_STAMP_NAME
    stamp = json.loads(stamp_path.read_text())
    stamp["schema_version"] = schema_version
    stamp_path.write_text(json.dumps(stamp))
    monkeypatch.setattr(
        es,
        "_run_npm",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("invalid stamp schema must fail before npm inspection")
        ),
    )

    assert es._source_dependencies_valid(tmp_path) is False


def test_deleted_transitive_dependency_triggers_exact_reinstall_and_build(
    tmp_path, monkeypatch,
):
    tree = {"name": "bobi-event-server"}
    _write_source_dependency_state(tmp_path, tree)
    monkeypatch.setattr(artifact, "is_artifact_current", lambda path: False)
    monkeypatch.setattr(
        es,
        "_run_npm",
        lambda args, path: (_ for _ in ()).throw(
            RuntimeError("npm dependency tree is invalid: missing transitive payload")
        ),
    )
    calls = []
    monkeypatch.setattr(
        es, "_install_source_dependencies", lambda path: calls.append("install")
    )
    monkeypatch.setattr(
        es, "_build_local", lambda path, version: calls.append("build")
    )

    es._ensure_source_artifact(tmp_path, "v20.19.2")

    assert calls == ["install", "build"]


def test_source_install_and_build_use_single_exact_commands(
    tmp_path, monkeypatch,
):
    calls = []

    def run_npm(args, path):
        calls.append(args)
        output = "9.2.0\n" if args == ["npm", "--version"] else ""
        return subprocess.CompletedProcess(
            args, returncode=0, stdout=output, stderr=""
        )

    generated = []
    monkeypatch.setattr(es, "_run_npm", run_npm)
    monkeypatch.setattr(
        es, "_refresh_dependency_stamp", lambda path: calls.append(["refresh-stamp"])
    )
    monkeypatch.setattr(
        artifact,
        "generate_artifact_metadata",
        lambda path, **kwargs: generated.append((path, kwargs)),
    )

    es._install_source_dependencies(tmp_path)
    es._build_local(tmp_path, "v20.19.2")

    assert calls == [
        ["npm", "ci", "--no-audit", "--no-fund"],
        ["refresh-stamp"],
        ["npm", "run", "build:local"],
        ["npm", "--version"],
    ]
    assert generated == [
        (
            tmp_path,
            {"node_version": "v20.19.2", "npm_version": "9.2.0"},
        )
    ]


def test_fresh_source_artifact_skips_npm(monkeypatch, tmp_path):
    monkeypatch.setattr(artifact, "is_artifact_current", lambda path: True)
    monkeypatch.setattr(
        es,
        "_source_dependencies_valid",
        lambda path: (_ for _ in ()).throw(
            AssertionError("fresh artifact must not inspect dependencies")
        ),
    )
    monkeypatch.setattr(
        es,
        "_build_local",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("fresh artifact must not build")
        ),
    )

    es._ensure_source_artifact(tmp_path, "v20.19.2")


def test_stale_source_with_valid_dependencies_runs_only_build(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(artifact, "is_artifact_current", lambda path: False)
    monkeypatch.setattr(es, "_source_dependencies_valid", lambda path: True)
    monkeypatch.setattr(
        es, "_install_source_dependencies", lambda path: calls.append("install")
    )
    monkeypatch.setattr(
        es, "_build_local", lambda path, version: calls.append(("build", version))
    )

    es._ensure_source_artifact(tmp_path, "v20.19.2")

    assert calls == [("build", "v20.19.2")]


def test_incomplete_source_dependencies_use_exact_install_then_build(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(artifact, "is_artifact_current", lambda path: False)
    monkeypatch.setattr(es, "_source_dependencies_valid", lambda path: False)
    monkeypatch.setattr(
        es, "_install_source_dependencies", lambda path: calls.append("install")
    )
    monkeypatch.setattr(
        es, "_build_local", lambda path, version: calls.append("build")
    )

    es._ensure_source_artifact(tmp_path, "v20.19.2")

    assert calls == ["install", "build"]


def test_valid_tree_build_failure_gets_one_exact_repair_and_retry(
    monkeypatch, tmp_path,
):
    calls = []
    builds = iter([RuntimeError("missing payload"), None])
    monkeypatch.setattr(artifact, "is_artifact_current", lambda path: False)
    monkeypatch.setattr(es, "_source_dependencies_valid", lambda path: True)
    monkeypatch.setattr(
        es, "_install_source_dependencies", lambda path: calls.append("install")
    )

    def build(path, version):
        calls.append("build")
        error = next(builds)
        if error:
            raise error

    monkeypatch.setattr(es, "_build_local", build)

    es._ensure_source_artifact(tmp_path, "v20.19.2")

    assert calls == ["build", "install", "build"]


def test_valid_tree_second_build_failure_surfaces_both_attempts(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(artifact, "is_artifact_current", lambda path: False)
    monkeypatch.setattr(es, "_source_dependencies_valid", lambda path: True)
    monkeypatch.setattr(es, "_install_source_dependencies", Mock())
    errors = iter([RuntimeError("first-build"), RuntimeError("retry-build")])
    monkeypatch.setattr(
        es, "_build_local", lambda *args: (_ for _ in ()).throw(next(errors))
    )

    with pytest.raises(RuntimeError, match=r"first-build.*retry-build"):
        es._ensure_source_artifact(tmp_path, "v20.19.2")


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
    """Patch pooled.request + Slack lookups; return a dict the call records into."""
    import bobi.http as pooled

    captured: dict = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["kwargs"] = kwargs

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

        return _Resp()

    monkeypatch.setattr(pooled, "request", fake_request)
    monkeypatch.setattr(es, "_slack_auth_info", lambda token: ("T_TEAM", "B_BOT", "U_BOT"))
    monkeypatch.setattr(es, "_slack_app_id", lambda token, bot_id: "A_APP")
    return captured


def test_slack_registration_signs_when_bubble_key_present(monkeypatch):
    """A bubble-keyed registration carries x-moda-* headers and sends raw bytes
    (content=), so the server reproduces the HMAC and writes the bubble-scoped
    record outbound channel sends need (#487)."""
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


def test_slack_registration_includes_app_token_for_signed_local_server(
    monkeypatch,
):
    """A server-reported local runtime may receive the app token even when
    reached over a non-loopback URL. URL shape is not the capability check."""
    captured = _capture_post(monkeypatch)
    health_calls = []
    monkeypatch.setattr(
        es,
        "health",
        lambda url: health_calls.append(url) or {"status": "ok", "mode": "local"},
    )
    cfg = _StubCfg({
        ("slack", "bot_token"): "xoxb-tok",
        ("slack", "signing_secret"): "signing-secret",
        ("slack", "app_token"): "xapp-socket-token",
    })

    es.register_slack_workspaces(
        "https://event-server.tailnet.example", cfg,
        bubble_id="bub_A", bubble_key="bkey_A",
    )

    assert health_calls == ["https://event-server.tailnet.example"]
    body = json.loads(captured["kwargs"]["content"])
    assert body["app_token"] == "xapp-socket-token"
    assert body["signing_secret"] == "signing-secret"
    assert captured["kwargs"]["headers"]["x-moda-signature"]


def test_slack_registration_without_app_token_skips_health_probe(monkeypatch):
    captured = _capture_post(monkeypatch)
    monkeypatch.setattr(
        es,
        "health",
        lambda url: (_ for _ in ()).throw(
            AssertionError("webhook registration must not probe socket health")
        ),
    )
    cfg = _StubCfg({
        ("slack", "bot_token"): "xoxb-tok",
        ("slack", "signing_secret"): "signing-secret",
        ("slack", "app_token"): "",
    })

    es.register_slack_workspaces(
        "http://localhost:8080", cfg,
        bubble_id="bub_A", bubble_key="bkey_A",
    )

    body = json.loads(captured["kwargs"]["content"])
    assert body["signing_secret"] == "signing-secret"
    assert "app_token" not in body


@pytest.mark.parametrize("health_payload", [
    pytest.param({"status": "ok", "mode": "worker"}, id="remote-worker"),
    pytest.param({"status": "ok"}, id="missing-mode"),
    pytest.param(None, id="unavailable"),
])
def test_slack_registration_omits_app_token_without_local_health(
    monkeypatch, health_payload,
):
    captured = _capture_post(monkeypatch)
    monkeypatch.setattr(es, "health", lambda url: health_payload)
    cfg = _StubCfg({
        ("slack", "bot_token"): "xoxb-tok",
        ("slack", "signing_secret"): "",
        ("slack", "app_token"): "xapp-socket-token",
    })

    es.register_slack_workspaces(
        "https://events.example.com", cfg,
        bubble_id="bub_A", bubble_key="bkey_A",
    )

    body = json.loads(captured["kwargs"]["content"])
    assert "app_token" not in body


@pytest.mark.parametrize("bubble_id,bubble_key", [
    pytest.param("", "bkey_A", id="missing-bubble-id"),
    pytest.param("bub_A", "", id="missing-bubble-key"),
])
def test_slack_registration_never_sends_app_token_unsigned(
    monkeypatch, bubble_id, bubble_key,
):
    captured = _capture_post(monkeypatch)
    monkeypatch.setattr(
        es,
        "health",
        lambda url: (_ for _ in ()).throw(
            AssertionError("unsigned registration must not probe health")
        ),
    )
    cfg = _StubCfg({
        ("slack", "bot_token"): "xoxb-tok",
        ("slack", "signing_secret"): "",
        ("slack", "app_token"): "xapp-socket-token",
    })

    es.register_slack_workspaces(
        "http://localhost:8080", cfg,
        bubble_id=bubble_id, bubble_key=bubble_key,
    )

    body = json.loads(captured["kwargs"]["content"])
    assert "app_token" not in body


def test_slack_registration_never_logs_app_token_on_failure(
    monkeypatch, caplog,
):
    app_token = "xapp-must-not-appear"
    monkeypatch.setattr(
        es, "health", lambda url: {"status": "ok", "mode": "local"},
    )
    monkeypatch.setattr(
        es, "_slack_auth_info", lambda token: ("T_TEAM", "B_BOT", "U_BOT"),
    )
    monkeypatch.setattr(es, "_slack_app_id", lambda token, bot_id: "A_APP")
    import bobi.http as pooled
    monkeypatch.setattr(
        pooled,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"request rejected with {app_token}")
        ),
    )
    cfg = _StubCfg({
        ("slack", "bot_token"): "xoxb-tok",
        ("slack", "signing_secret"): "",
        ("slack", "app_token"): app_token,
    })

    result = es.register_slack_workspaces(
        "http://localhost:8080", cfg,
        bubble_id="bub_A", bubble_key="bkey_A",
    )

    assert result == []
    assert app_token not in caplog.text
