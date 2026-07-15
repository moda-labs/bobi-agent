"""Integration tests for the gateway brain (#655, epic #548).

The hermetic test proves the env-injection contract end-to-end against a real
Claude CLI without any real model: a local stub speaking just enough of the
Anthropic Messages API (SSE streaming included) records what the CLI actually
sends. It asserts the request reached the configured base URL with the
configured model, that the ambient real ANTHROPIC_API_KEY never left the
process, and that the gateway's auth token did.

The live test drives a real Anthropic-compatible backend and is gated on
``BOBI_GATEWAY_LIVE=<base_url>[,<model>]`` (e.g. a local ``ollama serve`` or a
LiteLLM proxy), mirroring the ``BOBI_CODEX_XMODEL`` gating pattern.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import _drain, requires_claude

pytestmark = pytest.mark.claude


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


class _StubGatewayHandler(BaseHTTPRequestHandler):
    """Anthropic Messages API stub: any POST to */messages completes one
    text-only assistant turn (streaming or not) and records the request."""

    def log_message(self, *args):  # silence request logging
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        if "count_tokens" in self.path:
            self._json(200, {"input_tokens": 10})
            return
        model = body.get("model", "")
        reply = f"STUB-REPLY from {model}"
        if body.get("stream"):
            self._stream(model, reply)
        else:
            self._json(200, {
                "id": "msg_stub", "type": "message", "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": reply}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, model: str, reply: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event, payload in [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_stub", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": reply}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"}),
        ]:
            self.wfile.write(_sse(event, payload))
            self.wfile.flush()


@pytest.fixture
def stub_gateway():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubGatewayHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


@requires_claude
@pytest.mark.timeout(120)
async def test_gateway_session_routes_to_stub(stub_gateway, tmp_path, monkeypatch):
    from bobi.brain import GATEWAY_BASE_URL_ENV, get_brain

    base_url = f"http://127.0.0.1:{stub_gateway.server_address[1]}"
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, base_url)
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "stub-model")
    # An ambient real key that must never reach the gateway, and the gateway
    # token that must.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")

    brain = get_brain("gateway")
    session = brain.make_session(
        cwd=str(tmp_path),
        system_prompt="You are a test assistant.",
        options={"max_turns": 1},
    )
    try:
        await session.connect("Say hello.")
        text, result = await _drain(session)
    finally:
        await session.disconnect()

    assert session.provider == "gateway"
    assert stub_gateway.requests, "no request reached the stub gateway"
    message_reqs = [r for r in stub_gateway.requests
                    if "count_tokens" not in r["path"]]
    assert message_reqs, "no /v1/messages request reached the stub gateway"
    req = message_reqs[0]
    assert req["body"]["model"] == "stub-model"
    # key hygiene: the ambient real key never leaves the process; the
    # gateway token authenticates instead
    for r in stub_gateway.requests:
        assert "sk-ant-ambient-secret" not in json.dumps(r["headers"])
        assert "sk-ant-ambient-secret" not in json.dumps(r["body"])
    assert "gateway-token" in json.dumps(req["headers"])
    # the stub's reply made it back through the CLI as assistant text
    assert "STUB-REPLY" in text
    assert result is not None and not result.is_error


def _live_gateway():
    raw = os.environ.get("BOBI_GATEWAY_LIVE", "")
    if not raw:
        return None, None
    base_url, _, model = raw.partition(",")
    return base_url.strip(), model.strip()


@requires_claude
@pytest.mark.live
@pytest.mark.timeout(300)
@pytest.mark.skipif(not os.environ.get("BOBI_GATEWAY_LIVE"),
                    reason="BOBI_GATEWAY_LIVE=<base_url>[,<model>] not set")
async def test_gateway_live_backend_completes_a_turn(tmp_path, monkeypatch):
    from bobi.brain import GATEWAY_BASE_URL_ENV, get_brain

    base_url, model = _live_gateway()
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, base_url)
    if model:
        monkeypatch.setenv("BOBI_BRAIN_MODEL", model)

    session = get_brain("gateway").make_session(
        cwd=str(tmp_path),
        system_prompt="You are a test assistant. Reply concisely.",
        options={"max_turns": 1},
    )
    try:
        await session.connect("Reply with exactly: GATEWAY-OK")
        text, result = await _drain(session)
    finally:
        await session.disconnect()

    assert result is not None and not result.is_error
    assert text
