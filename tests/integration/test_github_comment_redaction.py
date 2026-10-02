"""Real Claude tool execution with local model and GitHub CLI doubles."""

import json
import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from tests.test_github_redaction import _fake_gh
from .conftest import _drain, requires_claude
from .test_gateway_brain import _sse, _StubGatewayHandler
from .test_gateway_openai_brain import _ResponsesStub, requires_codex_responses_gateway


class _CommentGatewayHandler(_StubGatewayHandler):
    def _stream(self, model, reply):
        if any(
            item.get("type") == "tool_result"
            for request in self.server.requests
            for message in request["body"].get("messages", [])
            for item in message.get("content", [])
            if isinstance(item, dict)
        ):
            return super()._stream(model, reply)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_comment", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1},
            }}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": "tool_comment",
                    "name": "Bash", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps({
                    "command": 'gh issue comment 12 --body "$FIXTURE_COMMENT"',
                    "description": "Post a synthetic comment to the local CLI double",
                })}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        for event, payload in events:
            self.wfile.write(_sse(event, payload))
            self.wfile.flush()


@requires_claude
@pytest.mark.claude
@pytest.mark.timeout(120)
async def test_real_claude_comment_body_is_redacted(tmp_path, monkeypatch):
    from bobi.brain import GATEWAY_BASE_URL_ENV, get_brain
    from bobi.github_redaction import REAL_GH_ENV, install_github_comment_redaction

    real_gh, capture = _fake_gh(tmp_path)
    real_gh.rename(tmp_path / "gh")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv(REAL_GH_ENV, raising=False)
    monkeypatch.setenv("GH_CAPTURE", str(capture))
    monkeypatch.setenv("GH_HOST", "fixture.invalid")
    monkeypatch.setenv("FIXTURE_COMMENT", "github_pat_" + "A" * 30)
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "bobi-home"))
    monkeypatch.delenv("BOBI_ROOT", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-test-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "local-test-token")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "stub-model")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CommentGatewayHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, f"http://127.0.0.1:{server.server_port}")
    install_github_comment_redaction()
    session = get_brain("gateway").make_session(
        cwd=str(tmp_path), system_prompt="Local security test.",
        options={"max_turns": 3, "allowed_tools": ["Bash"]},
    )
    try:
        await session.connect()
        await session.query("Execute the supplied tool call.")
        await _drain(session)
    finally:
        await session.disconnect()
        server.shutdown()
        thread.join(timeout=5)

    assert capture.exists(), "real Claude did not execute the local gh double"
    assert json.loads(capture.read_text())["stdin"] == "[redacted]"


class _CodexCommentGatewayHandler(_ResponsesStub):
    def _stream(self, model):
        body = self.server.requests[-1]["body"]
        if any(item.get("type") == "function_call_output" for item in body.get("input", [])):
            return super()._stream(model)
        tools = {tool.get("name") for tool in body.get("tools", [])}
        name = "exec_command" if "exec_command" in tools else "shell_command"
        arguments = {"cmd" if name == "exec_command" else "command":
                     'gh issue comment 12 --body "$FIXTURE_COMMENT"'}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            ("response.created", {"type": "response.created", "response": {"id": "resp-comment"}}),
            ("response.output_item.done", {"type": "response.output_item.done", "item": {
                "id": "fc-comment", "type": "function_call", "call_id": "call-comment",
                "name": name, "arguments": json.dumps(arguments),
            }}),
            ("response.completed", {"type": "response.completed", "response": {
                "id": "resp-comment", "usage": {"input_tokens": 10, "output_tokens": 5,
                    "total_tokens": 15},
            }}),
        ]
        for event, payload in events:
            self.wfile.write(_sse(event, payload))
            self.wfile.flush()


@requires_codex_responses_gateway
@pytest.mark.timeout(120)
async def test_real_codex_comment_body_is_redacted(tmp_path, monkeypatch):
    from bobi.brain import GATEWAY_BASE_URL_ENV, get_brain
    from bobi.github_redaction import REAL_GH_ENV, install_github_comment_redaction

    real_gh, capture = _fake_gh(tmp_path)
    real_gh.rename(tmp_path / "gh")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv(REAL_GH_ENV, raising=False)
    monkeypatch.setenv("GH_CAPTURE", str(capture))
    monkeypatch.setenv("GH_HOST", "fixture.invalid")
    monkeypatch.setenv("FIXTURE_COMMENT", "github_pat_" + "A" * 30)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "bobi-home"))
    monkeypatch.delenv("BOBI_ROOT", raising=False)
    monkeypatch.setenv("BOBI_GATEWAY_API_KEY", "local-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-key")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "stub-model")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CodexCommentGatewayHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, f"http://127.0.0.1:{server.server_port}/v1")
    install_github_comment_redaction()
    session = get_brain("gateway-openai").make_session(cwd=str(tmp_path), system_prompt="Local test.")
    try:
        await session.connect()
        await session.query("Execute the supplied tool call.")
        await _drain(session)
    finally:
        await session.disconnect()
        server.shutdown()
        thread.join(timeout=5)

    assert capture.exists(), "real Codex did not execute the local gh double"
    assert json.loads(capture.read_text())["stdin"] == "[redacted]"
