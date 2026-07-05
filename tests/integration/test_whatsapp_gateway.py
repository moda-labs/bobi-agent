"""End-to-end integration tests for the WhatsApp channel (#656, #190 Phase 3).

Drives the REAL path with a stubbed Meta Graph API (BOBI_ES_WHATSAPP_API_URL):

- Meta's GET subscribe handshake against the real local event server,
- signed inbound webhook -> pipeline verify -> normalize -> grant-filtered
  delivery to a joined deployment,
- the 24h message-window contract: `bobi reply` sends free-form only after an
  inbound message, and returns the typed outside_message_window error
  otherwise,
- the signed /whatsapp/numbers registration through the production client
  (register_whatsapp_numbers), which seeds both the bubble-scoped send
  credential and the whatsapp resource grant.

No live Meta credentials exist yet; a live tier mirroring test_slack_live.py
is deferred until a test number is provisioned.
"""

import hashlib
import hmac
import json
import os
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from click.testing import CliRunner

from bobi.config import Config, ServiceConfig, save_bubble_state
from bobi.events.server import (
    _post_register,
    ensure_running,
    register_whatsapp_numbers,
)
from bobi.events.signing import signed_request

PNID = "747556541"
WA_USER = "15551234567"
CONV = f"whatsapp:{PNID}:dm:{WA_USER}"
APP_SECRET = "wa-app-secret"
VERIFY_TOKEN = "wa-verify-token"
ACCESS_TOKEN = "EAAG-gw-test"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _GraphStub:
    """Minimal Meta Graph API stub recording every call it receives."""

    def __init__(self):
        self.calls: list[dict] = []
        self.port = _free_port()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _respond(self, payload: dict, status: int = 200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                stub.calls.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                })
                # Token verification: GET /<pnid>?fields=id
                if self.path.startswith(f"/{PNID}?"):
                    return self._respond({"id": PNID})
                return self._respond({"error": {"message": "unknown node"}}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"_raw": True}
                stub.calls.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "body": body,
                })
                if self.path == f"/{PNID}/messages":
                    n = len([c for c in stub.calls
                             if c["path"] == f"/{PNID}/messages"])
                    return self._respond({"messages": [{"id": f"wamid.out.{n}"}]})
                return self._respond({"error": {"message": "unknown path"}}, 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def sends(self) -> list[dict]:
        return [c for c in self.calls if c["path"] == f"/{PNID}/messages"]


def _meta_webhook(text: str, wa_id: str = WA_USER,
                  msg_id: str = "wamid.in.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_GW",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550001111",
                                 "phone_number_id": PNID},
                    "contacts": [{"profile": {"name": "Ada"}, "wa_id": wa_id}],
                    "messages": [{
                        "from": wa_id, "id": msg_id,
                        "timestamp": "1783300000",
                        "type": "text", "text": {"body": text},
                    }],
                },
            }],
        }],
    }


def _post_webhook(es_url: str, payload: dict, *, secret: str = APP_SECRET):
    body = json.dumps(payload)
    sig = "sha256=" + hmac.new(
        secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return httpx.post(
        f"{es_url}/webhooks/whatsapp", content=body,
        headers={"Content-Type": "application/json",
                 "x-hub-signature-256": sig},
        timeout=10,
    )


@pytest.fixture(scope="module")
def whatsapp_env(tmp_path_factory):
    """A real local event server wired to a Graph API stub, with a minted
    bubble and a signed number registration through the production client."""
    base = tmp_path_factory.mktemp("whatsapp-gw")
    project = base / "run"
    (project / "package").mkdir(parents=True)
    (project / "state").mkdir(parents=True)

    stub = _GraphStub()
    stub.start()

    port = _free_port()
    es_url = f"http://localhost:{port}"
    (project / "package" / "agent.yaml").write_text(
        f"entry_point: manager\nevent_server: {es_url}\n")

    status = ensure_running(
        port,
        project_path=project,
        extra_env={
            "BOBI_ES_WHATSAPP_API_URL": f"http://127.0.0.1:{stub.port}/",
            "BOBI_ES_WHATSAPP_APP_SECRET": APP_SECRET,
            "BOBI_ES_WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN,
        },
    )
    assert status in ("started", "connected")

    minted = _post_register(es_url, "whatsapp-bootstrap", ["_bootstrap"])
    save_bubble_state(project, minted["bubble_id"], minted["bubble_key"])

    # Production registration client: verifies the token against the (stubbed)
    # Graph API, stores the send credential, writes the whatsapp grant.
    cfg = Config(services=[ServiceConfig(name="whatsapp", credentials={
        "access_token": ACCESS_TOKEN, "phone_number_id": PNID,
    })])
    registered = register_whatsapp_numbers(
        es_url, cfg, minted["bubble_id"], minted["bubble_key"])
    assert registered == [PNID]

    yield project, stub, es_url, minted

    stub.stop()
    pid_file = project / "state" / "event-server.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass


@pytest.fixture
def whatsapp(whatsapp_env, monkeypatch):
    project, stub, es_url, bubble = whatsapp_env
    monkeypatch.setenv("BOBI_ROOT", str(project))
    stub.calls.clear()
    return project, stub, es_url, bubble


class TestHandshake:
    def test_get_handshake_echoes_challenge_as_raw_text(self, whatsapp):
        _project, _stub, es_url, _bubble = whatsapp
        resp = httpx.get(
            f"{es_url}/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                    "hub.challenge": "424242"},
            timeout=10,
        )
        assert resp.status_code == 200
        # Meta compares byte-for-byte: raw text, never a JSON-quoted string.
        assert resp.text == "424242"

    def test_wrong_verify_token_is_rejected(self, whatsapp):
        _project, _stub, es_url, _bubble = whatsapp
        resp = httpx.get(
            f"{es_url}/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                    "hub.challenge": "424242"},
            timeout=10,
        )
        assert resp.status_code == 403


class TestInboundDelivery:
    def test_signed_webhook_delivers_to_granted_subscriber(self, whatsapp):
        _project, _stub, es_url, bubble = whatsapp
        # JOIN a deployment subscribed to the number's topic. Registration
        # succeeding proves the whatsapp grant exists (#488 gate).
        resp = signed_request(
            es_url, "POST", "/deployments",
            {"name": "wa-inbound", "subscriptions": [f"whatsapp:{PNID}"]},
            bubble["bubble_id"], bubble["bubble_key"], timeout=10,
        )
        assert resp.status_code == 201, resp.text

        result = _post_webhook(es_url, _meta_webhook("hola bobi"))
        assert result.status_code == 200
        assert result.json().get("delivered_to") == 1

    def test_bad_signature_is_rejected_and_not_delivered(self, whatsapp):
        _project, _stub, es_url, _bubble = whatsapp
        result = _post_webhook(
            es_url, _meta_webhook("forged"), secret="wrong-secret")
        assert result.status_code == 401


class TestReplyWindow:
    def test_reply_sends_within_window_opened_by_inbound(self, whatsapp):
        _project, stub, es_url, _bubble = whatsapp
        assert _post_webhook(
            es_url, _meta_webhook("open the window")).status_code == 200

        from bobi.cli import main
        result = CliRunner().invoke(main, ["reply", CONV, "hola *back*"])
        assert result.exit_code == 0, result.output

        sends = stub.sends()
        assert len(sends) == 1
        assert sends[0]["auth"] == f"Bearer {ACCESS_TOKEN}"
        assert sends[0]["body"] == {
            "messaging_product": "whatsapp", "to": WA_USER,
            "type": "text", "text": {"body": "hola *back*"},
        }

    def test_reply_outside_window_returns_typed_error(self, whatsapp):
        _project, stub, es_url, _bubble = whatsapp
        # A user who never messaged us: no window record exists.
        from bobi.cli import main
        result = CliRunner().invoke(main, [
            "reply", f"whatsapp:{PNID}:dm:19998887777", "cold outreach",
        ])
        assert result.exit_code == 1
        assert "outside_message_window" in result.output
        assert stub.sends() == []
