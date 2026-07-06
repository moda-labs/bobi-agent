"""End-to-end integration tests for the channel gateway (#190 Phase 2).

Drives the REAL path an agent reply takes: `bobi reply` signs a request with
the instance's bubble key, POSTs to the real local Node event server, which
delivers through the Chat SDK Slack adapter to a stubbed Slack Web API
(BOBI_ES_SLACK_API_URL). Verifies:

- the bubble-signed /channels/send, /channels/typing, /channels/history wire
  format (including the GET signature over path + query),
- server-side markdown delivery (markdown_text, no client conversion),
- the response-context contract (mode final clears the typing indicator),
- the typing flow the drain loop runs (SlackInputChannel via gateway),
- the workspace registration that seeds the bubble-scoped send credential.
"""

import json
import os
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import httpx
import pytest
from click.testing import CliRunner

from bobi.config import save_bubble_state
from bobi.events.server import _post_register, ensure_running
from bobi.events.signing import signed_request


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _SlackStub:
    """Minimal Slack Web API stub recording every call it receives."""

    def __init__(self):
        self.calls: list[dict] = []
        self.port = _free_port()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _respond(self, payload: dict):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                if "json" in ctype:
                    return json.loads(raw or b"{}")
                return {k: v[0] for k, v in
                        parse_qs(raw.decode(), keep_blank_values=True).items()}

            def do_POST(self):
                body = self._read_body()
                stub.calls.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "body": body,
                })
                if self.path.endswith("/upload"):
                    return self._respond({"ok": True})
                method = self.path.rsplit("/", 1)[-1]
                if method == "auth.test":
                    return self._respond({
                        "ok": True, "team_id": "T_GW",
                        "bot_id": "B_GW", "user_id": "U_BOTGW",
                    })
                if method == "bots.info":
                    return self._respond({"ok": True, "bot": {"app_id": "A_GW"}})
                if method == "chat.postMessage":
                    return self._respond({"ok": True, "ts": "100.001"})
                if method == "chat.update":
                    return self._respond({"ok": True, "ts": body.get("ts", "")})
                if method == "assistant.threads.setStatus":
                    return self._respond({"ok": True})
                if method == "conversations.replies":
                    return self._respond({"ok": True, "messages": [
                        {"user": "U_H", "text": "question", "ts": "100.000"},
                        {"user": "U_BOTGW", "text": "answer", "ts": "100.001",
                         "files": [{"id": "F1", "name": "a.png",
                                    "mimetype": "image/png",
                                    "url_private": "https://x"}]},
                    ]})
                if method == "files.getUploadURLExternal":
                    return self._respond({
                        "ok": True, "file_id": "F_GW1",
                        "upload_url": f"http://127.0.0.1:{stub.port}/upload",
                    })
                if method == "files.completeUploadExternal":
                    return self._respond({"ok": True, "files": [{"id": "F_GW1"}]})
                return self._respond({"ok": False, "error": "unknown_method"})

            do_GET = do_POST

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def named(self, method: str) -> list[dict]:
        return [c for c in self.calls if c["path"].endswith(f"/{method}")]


@pytest.fixture(scope="module")
def gateway_env(tmp_path_factory):
    """A real local event server wired to a Slack API stub, with a minted
    bubble and a signed (bubble-scoped) workspace registration."""
    base = tmp_path_factory.mktemp("gateway")
    project = base / "run"
    (project / "package").mkdir(parents=True)
    (project / "state").mkdir(parents=True)

    stub = _SlackStub()
    stub.start()

    port = _free_port()
    es_url = f"http://localhost:{port}"
    (project / "package" / "agent.yaml").write_text(
        f"entry_point: manager\nevent_server: {es_url}\n")

    status = ensure_running(
        port,
        project_path=project,
        slack_signing_secret="",
        extra_env={"BOBI_ES_SLACK_API_URL": f"http://127.0.0.1:{stub.port}/api/"},
    )
    assert status in ("started", "connected")

    # Mint the instance bubble (throwaway bootstrap deployment, as the real
    # client does) and persist it where the CLI's signer looks.
    minted = _post_register(es_url, "gateway-bootstrap", ["_bootstrap"])
    save_bubble_state(project, minted["bubble_id"], minted["bubble_key"])

    # Signed workspace registration — the server verifies the token against
    # (stubbed) auth.test and stores the bubble-scoped send credential.
    resp = signed_request(
        es_url, "POST", "/slack/workspaces",
        {"workspace_id": "T_GW", "bot_token": "xoxb-gw-test"},
        minted["bubble_id"], minted["bubble_key"], timeout=10,
    )
    assert resp.status_code == 200, resp.text

    yield project, stub, es_url, minted

    stub.stop()
    pid_file = project / "state" / "event-server.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass


@pytest.fixture
def gateway(gateway_env, monkeypatch):
    project, stub, es_url, bubble = gateway_env
    monkeypatch.setenv("BOBI_ROOT", str(project))
    stub.calls.clear()
    return project, stub, es_url, bubble


class TestReplyEndToEnd:
    def test_reply_delivers_markdown_and_clears_typing(self, gateway):
        _project, stub, _es_url, _bubble = gateway
        from bobi.cli import main

        result = CliRunner().invoke(main, [
            "reply", "slack:T_GW:channel:C_GW:thread:100.000", "**hi** _there_",
        ])
        assert result.exit_code == 0, result.output
        assert "Sent to slack:T_GW:channel:C_GW:thread:100.000" in result.output

        posts = stub.named("chat.postMessage")
        assert len(posts) == 1
        assert posts[0]["auth"] == "Bearer xoxb-gw-test"
        # Markdown goes out raw as markdown_text — the channel renders it.
        assert posts[0]["body"]["markdown_text"] == "**hi** _there_"
        assert posts[0]["body"]["channel"] == "C_GW"
        assert posts[0]["body"]["thread_ts"] == "100.000"
        assert "text" not in posts[0]["body"]

        # mode final resolved the response context: typing cleared.
        statuses = stub.named("assistant.threads.setStatus")
        assert len(statuses) == 1
        assert statuses[0]["body"]["status"] == ""
        assert statuses[0]["body"]["thread_ts"] == "100.000"

    def test_reply_edit_updates_placeholder(self, gateway):
        _project, stub, _es_url, _bubble = gateway
        from bobi.cli import main

        result = CliRunner().invoke(main, [
            "reply", "slack:T_GW:channel:C_GW:thread:100.000",
            "--edit", "100.005", "the real answer",
        ])
        assert result.exit_code == 0, result.output
        updates = stub.named("chat.update")
        assert len(updates) == 1
        assert updates[0]["body"]["ts"] == "100.005"
        assert updates[0]["body"]["markdown_text"] == "the real answer"
        assert not stub.named("chat.postMessage")

    def test_reply_file_uploads_through_gateway(self, gateway, tmp_path):
        _project, stub, _es_url, _bubble = gateway
        from bobi.cli import main

        f = tmp_path / "report.txt"
        f.write_bytes(b"gateway file bytes")
        result = CliRunner().invoke(main, [
            "reply", "slack:T_GW:channel:C_GW:thread:100.000",
            "--file", str(f), "here's the report",
        ])
        assert result.exit_code == 0, result.output
        get_url = stub.named("files.getUploadURLExternal")
        assert len(get_url) == 1
        assert get_url[0]["body"]["filename"] == "report.txt"
        uploads = stub.named("upload")
        assert len(uploads) == 1
        complete = stub.named("files.completeUploadExternal")
        assert len(complete) == 1
        assert complete[0]["body"]["channel_id"] == "C_GW"

    def test_read_conversation_signed_get(self, gateway):
        """The GET signature covers path + query with an empty body — this
        proves the Python signer and Node verifier agree on the encoded URL."""
        _project, stub, _es_url, _bubble = gateway
        from bobi.cli import main

        result = CliRunner().invoke(main, [
            "read-conversation", "slack:T_GW:channel:C_GW:thread:100.000",
        ])
        assert result.exit_code == 0, result.output
        assert "[100.000] U_H: question" in result.output
        assert ">> a.png (image/png)" in result.output
        replies = stub.named("conversations.replies")
        assert len(replies) == 1
        assert replies[0]["body"]["channel"] == "C_GW"
        assert replies[0]["body"]["ts"] == "100.000"

    def test_deprecated_slack_reply_shim(self, gateway):
        _project, stub, _es_url, _bubble = gateway
        from bobi.cli import main

        result = CliRunner().invoke(main, [
            "slack-reply", "-w", "T_GW", "-c", "C_GW", "-t", "100.000", "legacy",
        ])
        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output
        posts = stub.named("chat.postMessage")
        assert len(posts) == 1
        assert posts[0]["body"]["markdown_text"] == "legacy"


class TestInboundMentionEndToEnd:
    def test_webhook_to_typing_full_loop(self, gateway, monkeypatch):
        """The full inbound loop minus the agent: a raw app_mention webhook
        hits the real server, the Chat SDK bridge normalizes it, a subscribed
        deployment receives it over WebSocket, and that REAL delivered event
        (not a hand-built one) drives the drain loop's typing-only
        policy back out through the gateway to the Slack stub."""
        import websocket

        project, stub, es_url, bubble = gateway
        from bobi.events.drain import _prepare_chat_events

        # JOIN a deployment on the instance bubble, subscribed to the
        # workspace topic. The /slack/workspaces registration in the fixture
        # already granted slack:T_GW to this bubble.
        resp = signed_request(
            es_url, "POST", "/deployments",
            {"name": "gateway-inbound", "subscriptions": ["slack:T_GW"]},
            bubble["bubble_id"], bubble["bubble_key"], timeout=10,
        )
        assert resp.status_code == 201, resp.text
        dep = resp.json()

        received: list[dict] = []

        def _subscribe():
            ws = websocket.create_connection(
                f"{es_url.replace('http://', 'ws://')}"
                f"/deployments/{dep['deployment_id']}/subscribe?last_seen=0",
                header=[f"Authorization: Bearer {dep['api_key']}"], timeout=10)
            try:
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("type") in ("event", "replay"):
                        received.append(msg["data"])
                        return
            finally:
                ws.close()

        listener = threading.Thread(target=_subscribe, daemon=True)
        listener.start()

        resp = httpx.post(f"{es_url}/webhooks/slack", json={
            "type": "event_callback", "team_id": "T_GW",
            "event": {"type": "app_mention", "user": "U_HUMAN",
                      "channel": "C_GW", "channel_type": "channel",
                      "text": "<@U_BOTGW> ship the report",
                      "ts": "1700000000.000100"},
        }, timeout=10)
        assert resp.status_code == 200, resp.text
        assert resp.json().get("delivered_to", 0) >= 1

        listener.join(timeout=10)
        assert received, "subscribed deployment never received the event"
        event = received[0]

        # The bridge-normalized contract the drain loop depends on.
        assert event["source"] == "slack"
        assert event["type"] == "slack.mention"
        assert event["delivery"] == "chat"
        assert event["conversation"] == \
            "slack:T_GW:channel:C_GW:thread:1700000000.000100"
        assert event["fields"]["channel"] == "C_GW"
        assert event["fields"]["ts"] == "1700000000.000100"

        monkeypatch.setattr(
            "bobi.events.drain._get_project_root", lambda: project)
        stub.calls.clear()
        prepared = _prepare_chat_events([event])
        assert "placeholder_ts" not in prepared[0]["fields"]

        posts = stub.named("chat.postMessage")
        assert posts == []
        statuses = stub.named("assistant.threads.setStatus")
        assert statuses and statuses[-1]["body"]["status"] == "is thinking…"

        from bobi.events.channels import stop_all_refresh_loops
        stop_all_refresh_loops()


class TestTypingFlowEndToEnd:
    def test_drain_typing_via_gateway(self, gateway, monkeypatch):
        """The drain loop's input channel sets typing through the gateway
        without posting a placeholder or injecting placeholder_ts."""
        project, stub, _es_url, _bubble = gateway
        from bobi.events.drain import _prepare_chat_events

        monkeypatch.setattr(
            "bobi.events.drain._get_project_root", lambda: project)

        event = {
            "source": "slack",
            "type": "slack.mention",
            "delivery": "chat",
            "text": "hello bot",
            "conversation": "slack:T_GW:channel:C_GW:thread:100.000",
            "fields": {
                "channel": "C_GW",
                "ts": "100.000",
                "placeholder_ts": "stale",
            },
        }
        prepared = _prepare_chat_events([event])
        assert "placeholder_ts" not in prepared[0]["fields"]

        posts = stub.named("chat.postMessage")
        assert posts == []
        statuses = stub.named("assistant.threads.setStatus")
        assert statuses and statuses[-1]["body"]["status"] == "is thinking…"

        from bobi.events.channels import stop_all_refresh_loops
        stop_all_refresh_loops()
