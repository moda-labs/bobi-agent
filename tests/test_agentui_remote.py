"""Tests for the remote-proxy helper behind the named UI command. The fly CLI calls
are mocked; we assert app resolution, token/port reads, command construction,
and the run() orchestration (no real `fly` subprocess)."""

import subprocess
import types

import pytest

# The tunnel is glue over the deploy engine (bobi_deploy, a separate
# package scheduled for deletion with this module); without it there is
# nothing to test.
pytest.importorskip("bobi_deploy")

from bobi.agentui import remote


@pytest.fixture(autouse=True)
def _fly_is_fly(monkeypatch):
    # Deterministic binary name regardless of what's on PATH in CI.
    monkeypatch.setattr(remote, "_fly", lambda: "fly")


def _fake_run(out, code=0):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")
    return run


# --- pure builders / resolution -----------------------------------------

def test_proxy_command():
    assert remote.proxy_command("moda-eng-team", 8080, 8080) == \
        ["fly", "proxy", "8080:8080", "-a", "moda-eng-team"]


def test_resolve_app_explicit_wins():
    assert remote.resolve_app(None, "moda-eng-team") == "moda-eng-team"
    assert remote.resolve_app("ignored", "moda-eng-team") == "moda-eng-team"


def test_resolve_app_via_deployment(monkeypatch):
    import bobi_deploy.deploy as deploy
    monkeypatch.setattr(deploy, "load_deploy_config",
                        lambda proj, name: types.SimpleNamespace(app_name=f"ci-{name}"))
    assert remote.resolve_app("canary", None) == "ci-canary"


def test_resolve_app_requires_target():
    with pytest.raises(ValueError):
        remote.resolve_app(None, None)


# --- ssh reads -----------------------------------------------------------

def test_fetch_token(monkeypatch):
    monkeypatch.setattr(remote.subprocess, "run", _fake_run("  abc123\n"))
    assert remote.fetch_token("app") == "abc123"


def test_fetch_token_failure(monkeypatch):
    monkeypatch.setattr(remote.subprocess, "run", _fake_run("", code=1))
    assert remote.fetch_token("app") == ""


def test_fetch_remote_port(monkeypatch):
    monkeypatch.setattr(remote.subprocess, "run", _fake_run("9100\n"))
    assert remote.fetch_remote_port("app") == 9100


def test_fetch_remote_port_defaults_on_garbage(monkeypatch):
    monkeypatch.setattr(remote.subprocess, "run", _fake_run("not-a-port"))
    assert remote.fetch_remote_port("app", default=8080) == 8080


# --- run() orchestration -------------------------------------------------

class _FakeProc:
    def __init__(self): self.terminated = False
    def wait(self, timeout=None): return 0
    def terminate(self): self.terminated = True
    def kill(self): pass


def _patch_run(monkeypatch, *, exists=True, token="tok", rport=8080):
    import bobi_deploy.deploy as deploy
    monkeypatch.setattr(deploy, "preflight_fly_or_exit", lambda: None)
    monkeypatch.setattr(deploy, "fly_app_exists", lambda app: exists)
    monkeypatch.setattr(remote, "fetch_remote_port", lambda *a, **k: rport)
    monkeypatch.setattr(remote, "fetch_token", lambda *a, **k: token)
    monkeypatch.setattr(remote, "_wait_for_port", lambda *a, **k: True)


def test_run_happy_path(monkeypatch):
    _patch_run(monkeypatch)
    monkeypatch.setattr(remote, "_free_local_port", lambda: 18081, raising=False)
    started, opened = {}, {}
    def fake_popen(cmd, **kw):
        started["cmd"] = cmd
        return _FakeProc()
    monkeypatch.setattr(remote.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(remote.webbrowser, "open",
                        lambda u: opened.__setitem__("url", u))

    rc = remote.run(name=None, app="moda-eng-team", open_browser=True)
    assert rc == 0
    assert started["cmd"] == ["fly", "proxy", "18081:8080", "-a", "moda-eng-team"]
    assert opened["url"] == "http://localhost:18081/?n=tok"


def test_run_default_local_port_uses_free_port(monkeypatch):
    _patch_run(monkeypatch, rport=8080)
    monkeypatch.setattr(remote, "_free_local_port", lambda: 19001, raising=False)
    started = {}
    monkeypatch.setattr(remote.subprocess, "Popen",
                        lambda cmd, **kw: started.setdefault("cmd", cmd) is None or _FakeProc())
    monkeypatch.setattr(remote.webbrowser, "open", lambda u: None)

    rc = remote.run(app="x", open_browser=False)

    assert rc == 0
    assert started["cmd"] == ["fly", "proxy", "19001:8080", "-a", "x"]


def test_run_local_port_override(monkeypatch):
    _patch_run(monkeypatch, rport=8080)
    started = {}
    monkeypatch.setattr(remote.subprocess, "Popen",
                        lambda cmd, **kw: started.setdefault("cmd", cmd) is None or _FakeProc())
    monkeypatch.setattr(remote.webbrowser, "open", lambda u: None)
    rc = remote.run(app="x", local_port=9999, open_browser=False)
    assert rc == 0
    assert started["cmd"] == ["fly", "proxy", "9999:8080", "-a", "x"]


def test_check_reports_reachable(monkeypatch, capsys):
    monkeypatch.setattr(remote, "_get_agents",
                        lambda port, tok: {"agents": [{"name": "mgr"}, {"name": "eng-1"}]})
    assert remote._check(8080, "tok") == 0
    assert "reachable" in capsys.readouterr().out


def test_get_agents_sends_canonical_header(monkeypatch):
    seen = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"agents": []}'

    def fake_urlopen(req, timeout=10):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(remote.urllib.request, "urlopen", fake_urlopen)

    assert remote._get_agents(18081, "tok") == {"agents": []}
    assert seen["url"] == "http://127.0.0.1:18081/api/dashboard"
    assert seen["headers"]["X-bobi-webui-token"] == "tok"
    assert "X-bobi-ui-token" not in seen["headers"]
    assert seen["timeout"] == 10


def test_check_reports_failure(monkeypatch):
    def boom(port, tok):
        raise OSError("connection refused")
    monkeypatch.setattr(remote, "_get_agents", boom)
    assert remote._check(8080, "tok") == 1


def test_run_check_mode_probes_and_skips_browser(monkeypatch):
    _patch_run(monkeypatch)
    monkeypatch.setattr(remote.subprocess, "Popen", lambda *a, **k: _FakeProc())
    opened = {"browser": False}
    monkeypatch.setattr(remote.webbrowser, "open",
                        lambda u: opened.__setitem__("browser", True))
    monkeypatch.setattr(remote, "_check", lambda port, tok: 0)
    rc = remote.run(app="x", check=True)
    assert rc == 0
    assert opened["browser"] is False     # check mode never opens a browser


def test_run_app_missing_returns_1(monkeypatch):
    _patch_run(monkeypatch, exists=False)
    called = {"popen": False}
    monkeypatch.setattr(remote.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", True))
    assert remote.run(app="nope") == 1
    assert called["popen"] is False


def test_run_token_missing_returns_1(monkeypatch):
    _patch_run(monkeypatch, token="")
    called = {"popen": False}
    monkeypatch.setattr(remote.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", True))
    assert remote.run(app="x") == 1
    assert called["popen"] is False
