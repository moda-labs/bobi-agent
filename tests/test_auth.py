"""Tests for modastack/auth.py — OAuth flow, token persistence, AuthState."""

import json
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from modastack.auth import AuthState, load_auth, save_auth, clear_auth, is_authenticated, github_login


# ---------------------------------------------------------------------------
# AuthState dataclass
# ---------------------------------------------------------------------------


class TestAuthState:
    def test_default_empty(self):
        state = AuthState()
        assert state.github_username == ""
        assert state.github_user_id == 0
        assert state.github_token == ""
        assert state.event_server_token == ""
        assert state.authenticated_at == ""

    def test_from_kwargs(self):
        state = AuthState(
            github_username="alice",
            github_user_id=42,
            github_token="gho_xxxx",
            event_server_token="moda_sess_yyyy",
            authenticated_at="2026-06-05T00:00:00Z",
        )
        assert state.github_username == "alice"
        assert state.github_user_id == 42
        assert state.github_token == "gho_xxxx"
        assert state.event_server_token == "moda_sess_yyyy"


# ---------------------------------------------------------------------------
# Persistence: load / save / clear
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        state = AuthState(
            github_username="bob",
            github_user_id=99,
            github_token="gho_abc",
            event_server_token="moda_sess_def",
            authenticated_at="2026-06-05T12:00:00Z",
        )
        save_auth(state, path=auth_file)
        assert auth_file.exists()

        loaded = load_auth(path=auth_file)
        assert loaded.github_username == "bob"
        assert loaded.github_user_id == 99
        assert loaded.github_token == "gho_abc"
        assert loaded.event_server_token == "moda_sess_def"
        assert loaded.authenticated_at == "2026-06-05T12:00:00Z"

    def test_load_missing_file_returns_empty(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        state = load_auth(path=auth_file)
        assert state.github_username == ""
        assert state.github_token == ""

    def test_clear_removes_file(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        save_auth(AuthState(github_username="x"), path=auth_file)
        assert auth_file.exists()
        clear_auth(path=auth_file)
        assert not auth_file.exists()

    def test_clear_missing_file_no_error(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        clear_auth(path=auth_file)

    def test_save_creates_parent_dirs(self, tmp_path):
        auth_file = tmp_path / "sub" / "dir" / "auth.yaml"
        save_auth(AuthState(github_username="nested"), path=auth_file)
        assert auth_file.exists()
        loaded = load_auth(path=auth_file)
        assert loaded.github_username == "nested"

    def test_saved_file_is_valid_yaml(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        save_auth(AuthState(
            github_username="alice",
            github_user_id=42,
            github_token="gho_secret",
        ), path=auth_file)
        raw = yaml.safe_load(auth_file.read_text())
        assert raw["github"]["username"] == "alice"
        assert raw["github"]["user_id"] == 42
        assert raw["github"]["token"] == "gho_secret"

    def test_file_permissions_are_restrictive(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        save_auth(AuthState(github_token="secret"), path=auth_file)
        import stat
        mode = auth_file.stat().st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# is_authenticated
# ---------------------------------------------------------------------------


class TestIsAuthenticated:
    def test_true_when_token_present(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        save_auth(AuthState(
            github_username="alice",
            github_token="gho_xxx",
            event_server_token="moda_sess_yyy",
        ), path=auth_file)
        assert is_authenticated(path=auth_file) is True

    def test_false_when_no_file(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        assert is_authenticated(path=auth_file) is False

    def test_false_when_no_token(self, tmp_path):
        auth_file = tmp_path / "auth.yaml"
        save_auth(AuthState(github_username="alice"), path=auth_file)
        assert is_authenticated(path=auth_file) is False


# ---------------------------------------------------------------------------
# github_login OAuth flow
# ---------------------------------------------------------------------------


class TestGitHubLogin:
    def test_login_starts_server_and_opens_browser(self, tmp_path):
        """Test the full OAuth flow with mocked HTTP calls."""
        auth_file = tmp_path / "auth.yaml"

        callback_response = {
            "token": "moda_sess_test123",
            "github_username": "testuser",
            "github_user_id": 12345,
        }

        # We need the real urlopen for hitting localhost, but mock it for the
        # event server callback exchange. Use side_effect to distinguish.
        real_urlopen = urllib.request.urlopen

        def selective_urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if "events.example.com" in url:
                resp = MagicMock()
                resp.read.return_value = json.dumps(callback_response).encode()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                return resp
            # Real request (to localhost callback server)
            kwargs = {"timeout": timeout} if timeout else {}
            return real_urlopen(req, **kwargs)

        browser_opened = []

        def fake_open_browser(url):
            browser_opened.append(url)
            # Simulate GitHub redirecting back with a code
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            redirect_uri = params["redirect_uri"][0]

            def hit_callback():
                time.sleep(0.2)
                try:
                    real_urlopen(f"{redirect_uri}?code=test_auth_code", timeout=2)
                except Exception:
                    pass
            threading.Thread(target=hit_callback, daemon=True).start()

        with patch("modastack.auth._open_browser", fake_open_browser), \
             patch("modastack.auth.urllib.request.urlopen", selective_urlopen):
            state = github_login(
                client_id="test_client_id",
                event_server_url="https://events.example.com",
                auth_path=auth_file,
                timeout=5,
            )

        assert state.github_username == "testuser"
        assert state.github_user_id == 12345
        assert state.event_server_token == "moda_sess_test123"
        assert state.authenticated_at != ""
        assert len(browser_opened) == 1
        assert "test_client_id" in browser_opened[0]

        loaded = load_auth(path=auth_file)
        assert loaded.github_username == "testuser"
        assert loaded.event_server_token == "moda_sess_test123"

    def test_login_timeout_raises(self, tmp_path):
        """If no callback arrives, login should raise after timeout."""
        auth_file = tmp_path / "auth.yaml"

        with patch("modastack.auth._open_browser", lambda url: None):
            with pytest.raises(TimeoutError):
                github_login(
                    client_id="test_id",
                    event_server_url="https://events.example.com",
                    auth_path=auth_file,
                    timeout=1,
                )
