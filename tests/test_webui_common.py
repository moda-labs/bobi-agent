import socket
import stat
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bobi.webui_common import resolve_static_asset
from bobi.webui_common.launcher import serve_container, serve_local
from bobi.webui_common.security import (
    WEBUI_TOKEN_HEADER,
    install_security,
)
from bobi.webui_common.static import mount_static, serve_index


SECRET = "shared-secret"


def _client(app, *, host="127.0.0.1"):
    return TestClient(app, base_url=f"http://{host}")


def _secured_app():
    app = FastAPI()
    install_security(
        app,
        secret=SECRET,
        header_name=WEBUI_TOKEN_HEADER,
        error_message="bad or missing token",
    )

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.get("/")
    def index():
        return {"page": True}

    return app


def test_security_rejects_foreign_host():
    c = _client(_secured_app(), host="evil.example.com")
    assert c.get("/", headers={WEBUI_TOKEN_HEADER: SECRET}).status_code == 403


def test_security_allows_page_without_secret_but_guards_api():
    c = _client(_secured_app())
    assert c.get("/").status_code == 200
    assert c.get("/api/ping").status_code == 403
    assert c.get("/api/ping", headers={WEBUI_TOKEN_HEADER: "wrong"}).status_code == 403
    assert c.get("/api/ping", headers={WEBUI_TOKEN_HEADER: SECRET}).json() == {"ok": True}


def test_security_rejects_legacy_headers():
    c = _client(_secured_app())
    assert c.get("/api/ping", headers={"x-bobi-nonce": SECRET}).status_code == 403
    assert c.get("/api/ping", headers={"x-bobi-ui-token": SECRET}).status_code == 403


def test_static_routes_substitute_index_and_serve_no_store_assets(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    html = tmp_path / "index.html"
    html.write_text("<meta content='{{TOKEN}}'><script src='/static/app.js'></script>")
    (static_dir / "app.js").write_text("console.log('ok')")

    app = FastAPI()
    serve_index(app, html, {"{{TOKEN}}": SECRET})
    mount_static(app, static_dir)
    c = _client(app)

    page = c.get("/")
    assert page.status_code == 200
    assert SECRET in page.text
    assert "{{TOKEN}}" not in page.text
    assert page.headers["cache-control"] == "no-store, max-age=0"

    asset = c.get("/static/app.js")
    assert asset.status_code == 200
    assert "text/javascript" in asset.headers["content-type"]
    assert asset.headers["cache-control"] == "no-store, max-age=0"
    assert c.get("/static/../secret.txt").status_code == 404


def test_static_routes_still_serve_shared_tokens(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    app = FastAPI()
    mount_static(app, static_dir)

    r = _client(app).get("/static/tokens.css")
    assert r.status_code == 200
    assert "--accent: #C8612B;" in r.text
    assert resolve_static_asset(static_dir, "tokens.css") is not None


def test_serve_local_mints_secret_opens_browser_and_runs_bound_socket(monkeypatch):
    seen = {}

    class FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self, sockets):
            seen["sockname"] = sockets[0].getsockname()

    def app_factory(secret):
        seen["secret"] = secret
        return FastAPI()

    monkeypatch.setattr("bobi.webui_common.launcher.secrets.token_urlsafe",
                        lambda n: "minted-token")
    monkeypatch.setattr("bobi.webui_common.launcher.threading.Timer",
                        lambda delay, fn: types.SimpleNamespace(start=lambda: fn()))
    monkeypatch.setattr("bobi.webui_common.launcher.webbrowser.open",
                        lambda url: seen.setdefault("url", url))
    monkeypatch.setattr("bobi.webui_common.launcher.uvicorn.Server", FakeServer)

    assert serve_local(app_factory, open_browser=True, label="test ui") == 0
    assert seen["secret"] == "minted-token"
    assert seen["sockname"][0] == "127.0.0.1"
    assert seen["url"].startswith("http://127.0.0.1:")
    assert seen["url"].endswith("/?n=minted-token")


def test_serve_container_writes_token_and_port_and_uses_ipv6_host(tmp_path, monkeypatch):
    seen = {}
    real_socket = socket.socket

    class FakeSocket:
        def __init__(self, family, sock_type):
            seen["family"] = family
            self._sock = real_socket(socket.AF_INET6, sock_type)

        def setsockopt(self, *args):
            self._sock.setsockopt(*args)

        def bind(self, address):
            seen["bind"] = address
            self._sock.bind(("::1", 0))

        def getsockname(self):
            return self._sock.getsockname()

    class FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self, sockets):
            seen["ran"] = True

    def app_factory(secret):
        seen["secret"] = secret
        return FastAPI()

    monkeypatch.delenv("BOBI_UI_TOKEN", raising=False)
    monkeypatch.setenv("BOBI_UI_HOST", "::")
    monkeypatch.setenv("BOBI_UI_PORT", "8080")
    monkeypatch.setattr("bobi.webui_common.launcher.secrets.token_urlsafe",
                        lambda n: "container-token")
    monkeypatch.setattr("bobi.webui_common.launcher.socket.socket", FakeSocket)
    monkeypatch.setattr("bobi.webui_common.launcher.uvicorn.Server", FakeServer)
    monkeypatch.setattr("bobi.webui_common.launcher.threading.Thread",
                        lambda target, daemon, name:
                        types.SimpleNamespace(start=lambda: target()))

    port = serve_container(app_factory, state_dir=tmp_path)

    assert port > 0
    assert seen["family"] == socket.AF_INET6
    assert seen["bind"] == ("::", 8080)
    assert seen["secret"] == "container-token"
    assert (tmp_path / "ui.token").read_text() == "container-token"
    assert (tmp_path / "ui.port").read_text() == str(port)
    assert stat.S_IMODE((tmp_path / "ui.token").stat().st_mode) == 0o600
    assert seen["ran"] is True


def test_serve_local_attaches_server_for_self_shutdown(monkeypatch):
    # Setup's /api/shutdown flips should_exit on app.state.uvicorn_server —
    # serve_local must attach the real server instance to the app it builds.
    built = {}

    class FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self, sockets):
            pass

    def app_factory(secret):
        built["app"] = FastAPI()
        return built["app"]

    monkeypatch.setattr("bobi.webui_common.launcher.uvicorn.Server", FakeServer)
    assert serve_local(app_factory, open_browser=False) == 0
    assert isinstance(built["app"].state.uvicorn_server, FakeServer)
