"""Unit tests for modastack.auth — OAuth flow and token persistence."""

import json
import threading
import urllib.request

import pytest

from modastack.auth import (
    AuthState,
    load_auth,
    save_auth,
    clear_auth,
    is_authenticated,
    AUTH_PATH,
    ensure_authenticated,
)


@pytest.fixture(autouse=True)
def _clean_auth(tmp_path, monkeypatch):
    """Redirect auth storage to a temp dir."""
    auth_file = tmp_path / "auth.yaml"
    monkeypatch.setattr("modastack.auth.AUTH_PATH", auth_file)
    yield
    auth_file.unlink(missing_ok=True)


class TestAuthState:
    def test_default_state(self):
        state = AuthState()
        assert state.github_username == ""
        assert state.github_user_id == 0
        assert state.github_token == ""
        assert state.event_server_token == ""

    def test_save_and_load(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.yaml"
        monkeypatch.setattr("modastack.auth.AUTH_PATH", auth_file)

        state = AuthState(
            github_username="testuser",
            github_user_id=12345,
            github_token="gho_abc123",
            event_server_token="moda_sess_xyz",
            authenticated_at="2026-01-01T00:00:00Z",
        )
        save_auth(state)
        assert auth_file.exists()

        loaded = load_auth()
        assert loaded.github_username == "testuser"
        assert loaded.github_user_id == 12345
        assert loaded.github_token == "gho_abc123"
        assert loaded.event_server_token == "moda_sess_xyz"
        assert loaded.authenticated_at == "2026-01-01T00:00:00Z"

    def test_load_missing_file(self):
        state = load_auth()
        assert state.github_username == ""
        assert state.event_server_token == ""

    def test_clear_auth(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.yaml"
        monkeypatch.setattr("modastack.auth.AUTH_PATH", auth_file)

        save_auth(AuthState(github_username="test", event_server_token="tok"))
        assert auth_file.exists()
        clear_auth()
        assert not auth_file.exists()

    def test_clear_auth_no_file(self):
        clear_auth()  # should not raise


class TestIsAuthenticated:
    def test_not_authenticated_empty(self):
        assert not is_authenticated()

    def test_authenticated_with_token_and_username(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.yaml"
        monkeypatch.setattr("modastack.auth.AUTH_PATH", auth_file)

        save_auth(AuthState(
            github_username="user",
            event_server_token="moda_sess_abc",
        ))
        assert is_authenticated()

    def test_not_authenticated_missing_token(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.yaml"
        monkeypatch.setattr("modastack.auth.AUTH_PATH", auth_file)

        save_auth(AuthState(github_username="user"))
        assert not is_authenticated()

    def test_not_authenticated_missing_username(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.yaml"
        monkeypatch.setattr("modastack.auth.AUTH_PATH", auth_file)

        save_auth(AuthState(event_server_token="tok"))
        assert not is_authenticated()


class TestEnsureAuthenticated:
    def test_returns_existing_if_valid(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.yaml"
        monkeypatch.setattr("modastack.auth.AUTH_PATH", auth_file)

        save_auth(AuthState(
            github_username="cached",
            event_server_token="moda_sess_cached",
        ))
        result = ensure_authenticated("http://localhost:9999")
        assert result.github_username == "cached"
        assert result.event_server_token == "moda_sess_cached"

    def test_local_mode_skips_browser(self, tmp_path, monkeypatch):
        """When event server returns mode=local, skip OAuth and get a dummy token."""
        import http.server

        auth_file = tmp_path / "auth.yaml"
        monkeypatch.setattr("modastack.auth.AUTH_PATH", auth_file)

        class FakeHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({
                    "client_id": "local",
                    "mode": "local",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                content_len = int(self.headers.get("Content-Length", 0))
                self.rfile.read(content_len)
                body = json.dumps({
                    "token": "moda_sess_local",
                    "github_username": "local-dev",
                    "github_user_id": 0,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), FakeHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            result = ensure_authenticated(f"http://localhost:{port}")
            assert result.github_username == "local-dev"
            assert result.event_server_token == "moda_sess_local"
            # Should be persisted
            loaded = load_auth()
            assert loaded.event_server_token == "moda_sess_local"
        finally:
            server.shutdown()
