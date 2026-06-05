"""GitHub OAuth authentication for modastack event server.

Handles the full OAuth flow: browser redirect → localhost callback → code
exchange via event server → persist session token.

Storage: ~/.modastack/auth.yaml (user identity, separate from per-repo config).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Event, Thread

import yaml

from modastack.config import GLOBAL_CONFIG_DIR

log = logging.getLogger(__name__)

AUTH_PATH = GLOBAL_CONFIG_DIR / "auth.yaml"


@dataclass
class AuthState:
    github_username: str = ""
    github_user_id: int = 0
    github_token: str = ""
    event_server_token: str = ""
    authenticated_at: str = ""


def load_auth(path: Path | None = None) -> AuthState:
    """Load auth state from disk. Returns empty AuthState if missing."""
    p = path or AUTH_PATH
    if not p.exists():
        return AuthState()
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return AuthState()
    gh = raw.get("github", {})
    es = raw.get("event_server", {})
    return AuthState(
        github_username=gh.get("username", ""),
        github_user_id=gh.get("user_id", 0),
        github_token=gh.get("token", ""),
        event_server_token=es.get("token", ""),
        authenticated_at=raw.get("authenticated_at", ""),
    )


def save_auth(state: AuthState, path: Path | None = None) -> None:
    """Persist auth state to disk with restrictive permissions."""
    p = path or AUTH_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "github": {
            "username": state.github_username,
            "user_id": state.github_user_id,
            "token": state.github_token,
        },
        "event_server": {
            "token": state.event_server_token,
        },
        "authenticated_at": state.authenticated_at,
    }
    p.write_text(yaml.dump(data, default_flow_style=False))
    os.chmod(p, 0o600)


def clear_auth(path: Path | None = None) -> None:
    """Remove auth state file."""
    p = path or AUTH_PATH
    p.unlink(missing_ok=True)


def is_authenticated(path: Path | None = None) -> bool:
    """Check whether a valid session token exists."""
    state = load_auth(path)
    return bool(state.github_token and state.event_server_token)


def ensure_authenticated(event_server_url: str, path: Path | None = None) -> AuthState:
    """Load existing auth or trigger login flow.

    Fetches the client_id from the event server's /auth/config, then
    runs github_login if not already authenticated.
    """
    state = load_auth(path)
    if state.github_token and state.event_server_token:
        return state

    req = urllib.request.Request(f"{event_server_url}/auth/config")
    with urllib.request.urlopen(req, timeout=5) as resp:
        config = json.loads(resp.read())

    client_id = config.get("client_id", "")
    if not client_id:
        raise RuntimeError("Event server did not return a client_id")

    if config.get("mode") == "local":
        # Local mode — get a dummy token without browser OAuth
        req = urllib.request.Request(
            f"{event_server_url}/auth/github/callback",
            data=json.dumps({"code": "local", "redirect_uri": ""}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        state = AuthState(
            github_username=result.get("github_username", "local"),
            github_user_id=result.get("github_user_id", 0),
            event_server_token=result["token"],
            authenticated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        save_auth(state, path=path)
        return state

    return github_login(client_id=client_id, event_server_url=event_server_url, auth_path=path)


def _open_browser(url: str) -> None:
    """Open URL in the default browser. Separated for testing."""
    webbrowser.open(url)


def github_login(
    client_id: str,
    event_server_url: str,
    auth_path: Path | None = None,
    timeout: int = 120,
) -> AuthState:
    """Run the full GitHub OAuth flow.

    1. Start ephemeral HTTP server on OS-assigned port
    2. Open browser to GitHub authorize URL
    3. Receive callback with ?code=...
    4. POST code to event server's /auth/github/callback
    5. Save and return AuthState
    """
    code_received = Event()
    auth_code: list[str] = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if code:
                auth_code.append(code)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authenticated! You can close this tab.</h2></body></html>"
                )
                code_received.set()
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code parameter")

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://localhost:{port}/callback"

    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        authorize_url = (
            f"https://github.com/login/oauth/authorize"
            f"?client_id={client_id}"
            f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
            f"&scope=read:user"
        )
        _open_browser(authorize_url)

        if not code_received.wait(timeout=timeout):
            raise TimeoutError(f"No OAuth callback received within {timeout}s")

        code = auth_code[0]

        # Exchange code via event server
        payload = json.dumps({
            "code": code,
            "redirect_uri": redirect_uri,
        }).encode()
        req = urllib.request.Request(
            f"{event_server_url}/auth/github/callback",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        state = AuthState(
            github_username=result["github_username"],
            github_user_id=result["github_user_id"],
            event_server_token=result["token"],
            authenticated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        save_auth(state, path=auth_path)
        return state
    finally:
        server.shutdown()
