"""GitHub OAuth authentication for modastack cloud event server.

Handles the full OAuth flow: opens browser for GitHub authorization,
receives callback on localhost, exchanges code via event server, and
persists credentials to ~/.modastack/auth.yaml.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import yaml

from modastack.config import GLOBAL_CONFIG_DIR

log = logging.getLogger(__name__)

AUTH_PATH = GLOBAL_CONFIG_DIR / "auth.yaml"

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
SCOPES = "read:user"


@dataclass
class AuthState:
    github_username: str = ""
    github_user_id: int = 0
    github_token: str = ""
    event_server_token: str = ""
    authenticated_at: str = ""


def load_auth() -> AuthState:
    if not AUTH_PATH.exists():
        return AuthState()
    raw = yaml.safe_load(AUTH_PATH.read_text()) or {}
    github = raw.get("github", {})
    event_server = raw.get("event_server", {})
    return AuthState(
        github_username=github.get("username", ""),
        github_user_id=github.get("user_id", 0),
        github_token=github.get("token", ""),
        event_server_token=event_server.get("token", ""),
        authenticated_at=raw.get("authenticated_at", ""),
    )


def save_auth(state: AuthState) -> None:
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    AUTH_PATH.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def clear_auth() -> None:
    AUTH_PATH.unlink(missing_ok=True)


def is_authenticated() -> bool:
    state = load_auth()
    return bool(state.event_server_token and state.github_username)


def github_login(client_id: str, event_server_url: str) -> AuthState:
    """Run the full GitHub OAuth flow.

    1. Start an ephemeral HTTP server on localhost
    2. Open browser to GitHub authorize URL
    3. Receive callback with ?code=...
    4. POST code to event server's /auth/github/callback
    5. Save and return AuthState
    """
    callback_result: dict = {}
    server_ready = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            error = params.get("error", [""])[0]

            if error:
                callback_result["error"] = error
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Authentication failed</h1>"
                                 b"<p>You can close this tab.</p></body></html>")
            elif code:
                callback_result["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Authenticated!</h1>"
                                 b"<p>You can close this tab.</p></body></html>")
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Missing code</h1></body></html>")

            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://localhost:{port}/callback"

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    authorize_url = (
        f"{GITHUB_AUTHORIZE_URL}?"
        f"client_id={urllib.parse.quote(client_id)}&"
        f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
        f"scope={urllib.parse.quote(SCOPES)}"
    )

    log.info(f"Opening browser for GitHub login...")
    opened = webbrowser.open(authorize_url)
    if not opened:
        log.info("Could not open browser automatically.")
        log.info(f"Open this URL manually:\n  {authorize_url}")

    server_thread.join(timeout=120)
    server.server_close()

    if "error" in callback_result:
        raise RuntimeError(f"GitHub OAuth error: {callback_result['error']}")
    if "code" not in callback_result:
        raise RuntimeError("OAuth flow timed out — no callback received")

    code = callback_result["code"]
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
        github_token=result.get("github_token", ""),
        event_server_token=result["token"],
        authenticated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    save_auth(state)
    return state


def ensure_authenticated(event_server_url: str) -> AuthState:
    """Load existing auth or trigger login flow.

    Checks if the event server is running in local mode (skips OAuth)
    or requires real GitHub auth.
    """
    existing = load_auth()
    if existing.event_server_token and existing.github_username:
        return existing

    req = urllib.request.Request(f"{event_server_url}/auth/config")
    with urllib.request.urlopen(req, timeout=5) as resp:
        config = json.loads(resp.read())

    if config.get("mode") == "local":
        req = urllib.request.Request(
            f"{event_server_url}/auth/github/callback",
            data=json.dumps({"code": "local", "redirect_uri": "local"}).encode(),
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
        save_auth(state)
        return state

    client_id = config.get("client_id", "")
    if not client_id:
        raise RuntimeError("Event server did not return a client_id — cannot authenticate")

    return github_login(client_id, event_server_url)
