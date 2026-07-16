"""Integration test for the OpenAI-compatible gateway brain (#777)."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _codex_meets_version_floor() -> bool:
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


@functools.lru_cache(maxsize=1)
def _codex_supports_chat_wire() -> bool:
    """Whether the installed codex still parses `wire_api = \"chat\"`.

    Some codex builds have DROPPED chat support entirely (config load fails
    with "no longer supported", see openai/codex discussion #7782) - a
    version floor can't detect that, so probe the config parser directly: a
    chat-provider override against a dead socket fails at config load on such
    builds, and at (or past) the network on builds that still support chat.
    This runs a codex subprocess, so it is called from the TEST BODY (cached),
    never at collection time.
    """
    try:
        probe = subprocess.run(
            ["codex", "exec", "-s", "read-only", "--skip-git-repo-check",
             "-c", 'model_provider="probe"',
             "-c", 'model_providers.probe.name="probe"',
             "-c", 'model_providers.probe.base_url="http://127.0.0.1:9/v1"',
             "-c", 'model_providers.probe.wire_api="chat"',
             "probe"],
            text=True, capture_output=True, timeout=20, check=False,
        )
    except Exception:
        return False
    return "no longer supported" not in (probe.stderr + probe.stdout)


requires_codex_responses_gateway = pytest.mark.skipif(
    not _codex_meets_version_floor(),
    reason="codex CLI >=0.144.4 not installed",
)

requires_codex_chat_gateway = pytest.mark.skipif(
    not _codex_meets_version_floor(),
    reason="codex CLI >=0.144.4 not installed",
)


def _skip_unless_chat_wire_supported() -> None:
    if not _codex_supports_chat_wire():
        pytest.skip("installed codex dropped wire_api=chat "
                    "(openai/codex#7782)")


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


class _ResponsesStub(BaseHTTPRequestHandler):
    """Minimal streaming OpenAI Responses API stub for Codex."""

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
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        self._stream(body.get("model", "stub-model"))

    def _stream(self, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        events = [
            ("response.created", {
                "type": "response.created",
                "response": {"id": "resp-stub"},
            }),
            ("response.output_text.delta", {
                "type": "response.output_text.delta",
                "delta": "STUB-REPLY",
                "output_index": 0,
                "content_index": 0,
            }),
            ("response.output_item.done", {
                "type": "response.output_item.done",
                "item": {
                    "id": "msg-stub",
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "STUB-REPLY",
                    }],
                },
            }),
            ("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": "resp-stub",
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                },
            }),
        ]
        for event, payload in events:
            payload.setdefault("model", model)
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
            )
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


@pytest.fixture
def responses_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ResponsesStub)
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


@requires_codex_responses_gateway
@pytest.mark.timeout(120)
async def test_gateway_openai_session_routes_default_responses_to_stub(
    responses_stub, tmp_path, monkeypatch,
):
    from bobi.brain import GATEWAY_BASE_URL_ENV, GATEWAY_WIRE_API_ENV, get_brain

    base_url = f"http://127.0.0.1:{responses_stub.server_address[1]}/v1"
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, base_url)
    monkeypatch.delenv(GATEWAY_WIRE_API_ENV, raising=False)
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "stub-model")
    monkeypatch.setenv("BOBI_GATEWAY_API_KEY", "gateway-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

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

    assert len(responses_stub.requests) >= 2
    assert all(r["path"] == "/v1/responses" for r in responses_stub.requests)
    for req in responses_stub.requests:
        blob = json.dumps(req)
        assert "sk-real-openai" not in blob
        assert "gateway-key" in blob
        assert req["body"]["model"] == "stub-model"
        assert "input" in req["body"]
        assert "messages" not in req["body"]


@requires_codex_chat_gateway
@pytest.mark.timeout(120)
async def test_gateway_openai_session_routes_fresh_and_resume_to_stub(
    chat_stub, tmp_path, monkeypatch,
):
    from bobi.brain import GATEWAY_BASE_URL_ENV, GATEWAY_WIRE_API_ENV, get_brain

    _skip_unless_chat_wire_supported()
    base_url = f"http://127.0.0.1:{chat_stub.server_address[1]}/v1"
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, base_url)
    monkeypatch.setenv(GATEWAY_WIRE_API_ENV, "chat")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "stub-model")
    monkeypatch.setenv("BOBI_GATEWAY_API_KEY", "gateway-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    # Recent codex CLIs refuse to start when CODEX_HOME does not exist
    # (pre-existing failure on main; unrelated to the session machinery).
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

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
