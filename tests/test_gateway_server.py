"""Tests for the built-in MCP gateway server."""

import json
from unittest.mock import patch

import httpx
import pytest

from modastack.mcp.gateway_server import gateway_chat, _handle_jsonrpc
from modastack import http as pooled


SAMPLE_CONNECTIONS = [
    {
        "name": "openai-gw",
        "kind": "gateway",
        "provider": "openai",
        "api_key": "sk-test-123",
        "model": "gpt-4o",
    },
    {
        "name": "openrouter-gw",
        "kind": "gateway",
        "provider": "openrouter",
        "api_key": "or-test-456",
        "model": "anthropic/claude-sonnet-4",
        "base_url": "https://openrouter.ai/api/v1",
    },
    {
        "name": "my-images",
        "kind": "image",
        "provider": "openai",
        "api_key": "sk-img",
    },
]


def _mock_openai_response():
    """Return a mock OpenAI chat completion response."""
    return {
        "choices": [{
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class TestGatewayChatRouting:
    def test_connection_not_found(self):
        result = gateway_chat("nonexistent", [{"role": "user", "content": "hi"}],
                              "", SAMPLE_CONNECTIONS)
        assert "error" in result
        assert "not found" in result["error"]

    def test_wrong_kind(self):
        result = gateway_chat("my-images", [{"role": "user", "content": "hi"}],
                              "", SAMPLE_CONNECTIONS)
        assert "error" in result
        assert "not 'gateway'" in result["error"]

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        conns = [{"name": "nokey", "kind": "gateway", "provider": "openai",
                  "api_key": "", "model": "gpt-4o"}]
        result = gateway_chat("nokey", [{"role": "user", "content": "hi"}],
                              "", conns)
        assert "error" in result
        assert "api_key" in result["error"].lower() or "API_KEY" in result["error"]

    def test_default_connection_when_single(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        conns = [{"name": "only-one", "kind": "gateway", "provider": "openai",
                  "api_key": "", "model": "gpt-4o"}]
        result = gateway_chat("", [{"role": "user", "content": "hi"}],
                              "", conns)
        # Routes to the single gateway (fails at api_key check)
        assert "api_key" in result.get("error", "").lower() or "API_KEY" in result.get("error", "")

    def test_explicit_connection_selected(self):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json=_mock_openai_response())
        )
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = gateway_chat("openai-gw",
                                  [{"role": "user", "content": "hi"}],
                                  "", SAMPLE_CONNECTIONS)
        assert result.get("content") == "Hello!"
        assert result.get("model") == "gpt-4o"
        assert result["usage"]["input_tokens"] == 10

    def test_model_override(self):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json=_mock_openai_response())
        )
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = gateway_chat("openai-gw",
                                  [{"role": "user", "content": "hi"}],
                                  "gpt-4o-mini", SAMPLE_CONNECTIONS)
        assert "error" not in result

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
        conns = [{"name": "env-gw", "kind": "gateway", "provider": "openai",
                  "api_key": "", "model": "gpt-4o"}]
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json=_mock_openai_response())
        )
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = gateway_chat("env-gw",
                                  [{"role": "user", "content": "hi"}],
                                  "", conns)
        assert result.get("content") == "Hello!"

    def test_openai_error_object(self):
        error_resp = {"error": {"message": "Rate limited", "type": "rate_limit"}}
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json=error_resp)
        )
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = gateway_chat("openai-gw",
                                  [{"role": "user", "content": "hi"}],
                                  "", SAMPLE_CONNECTIONS)
        assert "error" in result
        assert "Rate limited" in result["error"]


class TestGatewayMCPProtocol:
    def test_initialize(self):
        resp = _handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, [])
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "modastack-gateway"

    def test_tools_list(self):
        resp = _handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, [])
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "gateway_chat"

    def test_tools_call_unknown_tool(self):
        resp = _handle_jsonrpc({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "unknown_tool", "arguments": {}},
        }, [])
        assert resp["result"]["isError"] is True
        assert "Unknown tool" in resp["result"]["content"][0]["text"]

    def test_tools_call_missing_connection(self):
        resp = _handle_jsonrpc({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "gateway_chat",
                "arguments": {
                    "messages": [{"role": "user", "content": "hi"}],
                    "connection": "missing",
                },
            },
        }, [])
        assert resp["result"]["isError"] is True
        content = resp["result"]["content"][0]["text"]
        assert "not found" in content

    def test_notifications_return_none(self):
        resp = _handle_jsonrpc({"method": "notifications/initialized"}, [])
        assert resp is None

    def test_unknown_method(self):
        resp = _handle_jsonrpc({"jsonrpc": "2.0", "id": 5, "method": "unknown/method"}, [])
        assert "error" in resp
        assert resp["error"]["code"] == -32601


class TestGatewayMCPInject:
    def test_injects_gateway_server(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        connections = [
            ConnectionEntry(name="gw", kind="gateway", provider="openai",
                            api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-gateway" in result
        assert result["modastack-gateway"]["type"] == "stdio"
        assert "modastack.mcp.gateway_server" in result["modastack-gateway"]["args"]

    def test_no_injection_without_gateway(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        connections = [
            ConnectionEntry(name="img", kind="image", provider="openai",
                            api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-gateway" not in result
        assert "modastack-image" in result
