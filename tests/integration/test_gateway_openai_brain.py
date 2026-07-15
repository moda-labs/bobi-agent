"""Integration test for the OpenAI-compatible gateway brain (#777)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _codex_supports_chat_gateway() -> bool:
    exe = shutil.which("codex")
    if not exe:
        return False
    try:
        out = subprocess.run(
            [exe, "--version"], text=True, capture_output=True, timeout=5,
            check=False,
        ).stdout
    except Exception:
        return False
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not match:
        return False
    return tuple(int(p) for p in match.groups()) >= (0, 144, 4)


requires_codex_chat_gateway = pytest.mark.skipif(
    not _codex_supports_chat_gateway(),
    reason="codex CLI >=0.144.4 with chat wire support not installed",
)


class _ChatCompletionsStub(BaseHTTPRequestHandler):
    """Minimal streaming OpenAI Chat Completions stub for Codex."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        if body.get("stream"):
            self._stream(body.get("model", "stub-model"))
            return
        self._json(body.get("model", "stub-model"))

    def _json(self, model: str) -> None:
        payload = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "STUB-REPLY"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            },
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant"},
                          "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "STUB-REPLY"},
                          "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                       "total_tokens": 10}},
        ]
        for chunk in chunks:
            chunk.update({
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
            })
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture
def chat_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatCompletionsStub)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def _drain(session):
    from bobi.brain import AssistantText, TurnResult

    text, result = "", None
    async for msg in session.receive_response():
        if isinstance(msg, AssistantText) and msg.text:
            text += msg.text
        elif isinstance(msg, TurnResult):
            result = msg
    return text, result


@requires_codex_chat_gateway
@pytest.mark.timeout(120)
async def test_gateway_openai_session_routes_fresh_and_resume_to_stub(
    chat_stub, tmp_path, monkeypatch,
):
    from bobi.brain import GATEWAY_BASE_URL_ENV, GATEWAY_WIRE_API_ENV, get_brain

    base_url = f"http://127.0.0.1:{chat_stub.server_address[1]}/v1"
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, base_url)
    monkeypatch.setenv(GATEWAY_WIRE_API_ENV, "chat")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "stub-model")
    monkeypatch.setenv("BOBI_GATEWAY_API_KEY", "gateway-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    session = get_brain("gateway-openai").make_session(
        cwd=str(tmp_path),
        system_prompt="Reply exactly STUB-REPLY and stop.",
    )
    try:
        await session.connect("First turn.")
        first_text, first_result = await _drain(session)
        await session.query("Second turn.")
        second_text, second_result = await _drain(session)
    finally:
        await session.disconnect()

    assert session.provider == "gateway"
    assert first_result is not None and not first_result.is_error
    assert second_result is not None and not second_result.is_error
    assert first_result.session_id
    assert second_result.session_id == first_result.session_id
    assert "STUB-REPLY" in first_text
    assert "STUB-REPLY" in second_text
    assert first_result.costs or second_result.costs

    assert len(chat_stub.requests) >= 2
    assert all(r["path"] == "/v1/chat/completions"
               for r in chat_stub.requests)
    for req in chat_stub.requests:
        blob = json.dumps(req)
        assert "sk-real-openai" not in blob
        assert "gateway-key" in blob
        assert req["body"]["model"] == "stub-model"
