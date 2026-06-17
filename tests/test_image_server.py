"""Tests for the built-in MCP image generation server."""

import json
from unittest.mock import patch

import httpx
import pytest

from modastack.mcp.image_server import generate_image, _handle_jsonrpc
from modastack import http as pooled


SAMPLE_CONNECTIONS = [
    {
        "name": "openai-images",
        "kind": "image",
        "provider": "openai",
        "api_key": "sk-test-123",
        "model": "gpt-image-1",
    },
    {
        "name": "google-images",
        "kind": "image",
        "provider": "google",
        "api_key": "AIza-test",
        "model": "imagen-3.0-generate-002",
    },
    {
        "name": "my-chat",
        "kind": "chat",
        "provider": "openai",
        "api_key": "sk-chat-key",
    },
]


class TestGenerateImageRouting:
    def test_connection_not_found(self):
        result = generate_image("nonexistent", "a cat", "1024x1024", SAMPLE_CONNECTIONS)
        assert "error" in result
        assert "not found" in result["error"]

    def test_wrong_kind(self):
        result = generate_image("my-chat", "a cat", "1024x1024", SAMPLE_CONNECTIONS)
        assert "error" in result
        assert "not 'image'" in result["error"]

    def test_unsupported_provider(self):
        conns = [{"name": "bad", "kind": "image", "provider": "midjourney", "api_key": "x"}]
        result = generate_image("bad", "a cat", "1024x1024", conns)
        assert "error" in result
        assert "Unsupported" in result["error"]

    def test_missing_api_key(self):
        conns = [{"name": "nokey", "kind": "image", "provider": "openai", "api_key": ""}]
        result = generate_image("nokey", "a cat", "1024x1024", conns)
        assert "error" in result
        assert "api_key" in result["error"]

    def test_default_connection_when_single(self):
        """When only one image connection exists, it's used as default."""
        conns = [{"name": "only-one", "kind": "image", "provider": "openai",
                  "api_key": "", "model": ""}]
        # Will fail at api_key check, but proves routing worked
        result = generate_image("", "a cat", "1024x1024", conns)
        assert "api_key" in result.get("error", "")

    def test_explicit_connection_selected(self):
        """Named connection is used even when multiple exist."""
        # Mock the HTTP call so it doesn't hit a real API
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={
                "generatedImages": [{"image": {"imageBytes": "abc", "mimeType": "image/png"}}]
            })
        )
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = generate_image("google-images", "a cat", "1024x1024", SAMPLE_CONNECTIONS)
        # It should route to google (not openai)
        assert "not found" not in result.get("error", "")
        assert result.get("b64_json") == "abc"


class TestMCPProtocol:
    def test_initialize(self):
        resp = _handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, [])
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "modastack-image"

    def test_tools_list(self):
        resp = _handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, [])
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "generate_image"

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
                "name": "generate_image",
                "arguments": {"prompt": "a cat", "connection": "missing"},
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


class TestMCPInject:
    def test_injects_image_server(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        connections = [
            ConnectionEntry(name="oai", kind="image", provider="openai", api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-image" in result
        assert result["modastack-image"]["type"] == "stdio"

    def test_no_injection_without_image_connections(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        connections = [
            ConnectionEntry(name="chat", kind="chat", provider="openai", api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-image" not in result

    def test_preserves_existing_mcp_servers(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        existing = {"my-crm": {"type": "http", "url": "https://crm.example.com"}}
        connections = [
            ConnectionEntry(name="oai", kind="image", provider="openai", api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(existing, connections)
        assert "my-crm" in result
        assert "modastack-image" in result

    def test_does_not_override_user_image_server(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        existing = {"modastack-image": {"type": "stdio", "command": "custom"}}
        connections = [
            ConnectionEntry(name="oai", kind="image", provider="openai", api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(existing, connections)
        assert result["modastack-image"]["command"] == "custom"

    def test_empty_connections(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        result = inject_builtin_mcp_servers(None, [])
        assert result == {}
