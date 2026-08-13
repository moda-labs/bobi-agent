import re
import socket
import stat
import types

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bobi.webui_common import resolve_static_asset
from bobi.webui_common.launcher import (
    open_browser_soon,
    serve_container,
    serve_local,
    serve_socket,
    write_secret,
)
from bobi.webui_common.security import (
    WEBUI_TOKEN_HEADER,
    install_security,
)
from bobi.webui_common.static import mount_static, serve_index


SECRET = "shared-secret"


def _client(app, *, host="127.0.0.1"):
    return TestClient(app, base_url=f"http://{host}")


def _secured_app(*, allowed_hosts=None):
    app = FastAPI()
    install_security(
        app,
        secret=SECRET,
        header_name=WEBUI_TOKEN_HEADER,
        error_message="bad or missing token",
        allowed_hosts=allowed_hosts,
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


def test_security_allows_configured_extra_host_via_param():
    # A hosted deployment names its public serving Host; that host is admitted
    # while an unlisted foreign Host still 403s.
    app = _secured_app(allowed_hosts={"127.0.0.1", "fleet.example.com"})
    ok = _client(app, host="fleet.example.com")
    assert ok.get("/", headers={WEBUI_TOKEN_HEADER: SECRET}).status_code == 200
    assert ok.get("/api/ping", headers={WEBUI_TOKEN_HEADER: SECRET}).json() == {"ok": True}
    evil = _client(app, host="evil.example.com")
    assert evil.get("/", headers={WEBUI_TOKEN_HEADER: SECRET}).status_code == 403


def test_security_host_match_is_case_insensitive(monkeypatch):
    # Hostnames are case-insensitive; an operator-typed env value or a proxy
    # forwarding mixed-case Host must still match.
    monkeypatch.setenv("BOBI_WEBUI_ALLOWED_HOSTS", "Fleet.Example.COM")
    c = _client(_secured_app(), host="fleet.example.com")
    assert c.get("/", headers={WEBUI_TOKEN_HEADER: SECRET}).status_code == 200
    # And the reverse: lowercase config, mixed-case request Host.
    app = _secured_app(allowed_hosts={"127.0.0.1", "fleet.example.com"})
    assert _client(app, host="Fleet.Example.com").get(
        "/", headers={WEBUI_TOKEN_HEADER: SECRET}
    ).status_code == 200


def test_security_reads_allowed_hosts_env(monkeypatch):
    # Wiring that build_app relies on: the env var threads through the default
    # (build_app does not forward an allowed_hosts kwarg).
    monkeypatch.setenv("BOBI_WEBUI_ALLOWED_HOSTS", "fleet.example.com, alt.example.com")
    c = _client(_secured_app(), host="alt.example.com")
    assert c.get("/", headers={WEBUI_TOKEN_HEADER: SECRET}).status_code == 200
    assert _client(_secured_app(), host="nope.example.com").get(
        "/", headers={WEBUI_TOKEN_HEADER: SECRET}
    ).status_code == 403


def test_security_default_hosts_unchanged_without_env(monkeypatch):
    monkeypatch.delenv("BOBI_WEBUI_ALLOWED_HOSTS", raising=False)
    assert _client(_secured_app(), host="evil.example.com").get(
        "/", headers={WEBUI_TOKEN_HEADER: SECRET}
    ).status_code == 403
    assert _client(_secured_app()).get("/").status_code == 200


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
    # Served from the shared dir even though static_dir is empty.
    assert "--bobi-acc" in r.text
    assert resolve_static_asset(static_dir, "tokens.css") is not None


def test_static_routes_serve_shared_brand_fonts(tmp_path):
    """The vendored brand faces are shared by both UIs, like tokens.css.

    They also live under a fonts/ subdirectory, so this covers prefix-based
    sharing and the woff2 media type on top of plain name matching.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    app = FastAPI()
    mount_static(app, static_dir)
    c = _client(app)

    sheet = c.get("/static/fonts.css")
    assert sheet.status_code == 200
    assert "@font-face" in sheet.text
    # Relative src paths keep resolving under a mounted base path.
    assert 'src: url("fonts/' in sheet.text
    assert "fonts.googleapis.com" not in sheet.text, "offline UI must not hit a CDN"

    face = c.get("/static/fonts/geist-latin.woff2")
    assert face.status_code == 200
    assert face.headers["content-type"] == "font/woff2"

    assert resolve_static_asset(static_dir, "fonts/geist-latin.woff2") is not None
    # The prefix must not become a path-traversal hole.
    assert resolve_static_asset(static_dir, "fonts/../../secret.txt") is None


class TestLaunchPrimitives:
    """The shared launch contract, called directly.

    `serve_local`/`serve_container` are not the only launchers: the webapp
    daemon binds its own socket because its port is fixed and it must write
    pid/port files between bind and serve. These primitives are what it
    composes instead of re-solving them (D097/Q024), so they are tested as
    the contract they now are.
    """

    def test_write_secret_persists_a_private_token(self, tmp_path):
        path = tmp_path / "ui.token"
        token = write_secret(path)

        assert len(token) > 20
        assert path.read_text() == token
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_write_secret_is_a_fresh_token_each_call(self, tmp_path):
        assert write_secret(tmp_path / "a") != write_secret(tmp_path / "b")

    def test_serve_socket_binds_loopback_and_is_reusable(self):
        sock = serve_socket("127.0.0.1", 0)
        try:
            assert sock.family == socket.AF_INET
            assert sock.getsockname()[0] == "127.0.0.1"
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        finally:
            sock.close()

    def test_serve_socket_picks_ipv6_for_an_ipv6_host(self):
        sock = serve_socket("::1", 0)
        try:
            assert sock.family == socket.AF_INET6
        finally:
            sock.close()

    def test_serve_socket_raises_when_the_port_is_taken(self):
        held = serve_socket("127.0.0.1", 0)
        try:
            port = held.getsockname()[1]
            held.listen(1)
            with pytest.raises(OSError):
                serve_socket("127.0.0.1", port).close()
        finally:
            held.close()

    def test_open_browser_soon_defers_the_open(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "bobi.webui_common.launcher.threading.Timer",
            lambda delay, fn: types.SimpleNamespace(
                start=lambda: seen.update(delay=delay, result=fn())))
        monkeypatch.setattr("bobi.webui_common.launcher.webbrowser.open",
                            lambda url: seen.setdefault("url", url))

        open_browser_soon("http://127.0.0.1:1/", delay=0.25)

        assert seen["url"] == "http://127.0.0.1:1/"
        assert seen["delay"] == 0.25


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


def test_serve_local_banner_is_the_label_line_operators_read(monkeypatch, capsys):
    """The startup banner is the only thing telling an operator where the UI is.

    `bobi setup` used to pass its own `announce` callback that reproduced this
    line character-for-character; the callback is gone, so the label branch is
    now the single source of that text and its exact shape is load-bearing.
    """
    class FakeServer:
        def __init__(self, config):
            pass

        def run(self, sockets):
            pass

    monkeypatch.setattr("bobi.webui_common.launcher.secrets.token_urlsafe",
                        lambda n: "minted-token")
    monkeypatch.setattr("bobi.webui_common.launcher.uvicorn.Server", FakeServer)

    assert serve_local(lambda secret: FastAPI(), open_browser=False,
                       label="bobi setup") == 0

    out = capsys.readouterr().out
    port = re.search(r"127\.0\.0\.1:(\d+)", out).group(1)
    assert out == (
        f"\n  bobi setup is running at "
        f"http://127.0.0.1:{port}/?n=minted-token\n  (Ctrl-C to stop)\n\n"
    )


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
