"""Tests for the built-in MCP codex server."""

import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from modastack.mcp.codex_server import codex_exec, _handle_jsonrpc


SAMPLE_CONNECTIONS = [
    {
        "name": "my-codex",
        "kind": "codex",
        "provider": "openai-codex",
        "api_key": "sk-test-123",
        "model": "",
    },
    {
        "name": "second-codex",
        "kind": "codex",
        "provider": "openai-codex",
        "api_key": "sk-test-456",
        "model": "",
    },
    {
        "name": "my-chat",
        "kind": "chat",
        "provider": "openai",
        "api_key": "sk-chat-key",
    },
]


class TestCodexExecRouting:
    def test_connection_not_found(self):
        result = codex_exec("nonexistent", "review this", SAMPLE_CONNECTIONS)
        assert "error" in result
        assert "not found" in result["error"]

    def test_wrong_kind(self):
        result = codex_exec("my-chat", "review this", SAMPLE_CONNECTIONS)
        assert "error" in result
        assert "not 'codex'" in result["error"]

    def test_default_connection_when_single(self):
        """When only one codex connection exists, it's used as default."""
        conns = [{"name": "only-one", "kind": "codex", "provider": "openai-codex",
                  "api_key": "sk-test", "model": ""}]
        with patch("modastack.mcp.codex_server._run_codex_exec") as mock_run:
            mock_run.return_value = {"output": "looks good"}
            result = codex_exec("", "review this", conns)
        mock_run.assert_called_once()
        assert result == {"output": "looks good"}

    def test_explicit_connection_selected(self):
        """Named connection is used even when multiple exist."""
        with patch("modastack.mcp.codex_server._run_codex_exec") as mock_run:
            mock_run.return_value = {"output": "critique here"}
            result = codex_exec("second-codex", "review this", SAMPLE_CONNECTIONS)
        mock_run.assert_called_once()
        assert result == {"output": "critique here"}

    def test_codex_exec_passes_prompt(self):
        """The prompt is forwarded to the codex exec subprocess."""
        with patch("modastack.mcp.codex_server._run_codex_exec") as mock_run:
            mock_run.return_value = {"output": "done"}
            codex_exec("my-codex", "Review this code for security issues", SAMPLE_CONNECTIONS)
        call_args = mock_run.call_args
        assert call_args[0][0] == "Review this code for security issues"

    def test_codex_exec_subprocess_failure(self):
        """Subprocess errors are returned as error dicts."""
        with patch("modastack.mcp.codex_server._run_codex_exec") as mock_run:
            mock_run.return_value = {"error": "codex exec failed: exit code 1"}
            result = codex_exec("my-codex", "review this", SAMPLE_CONNECTIONS)
        assert "error" in result


class TestRunCodexExec:
    def test_successful_execution(self):
        from modastack.mcp.codex_server import _run_codex_exec
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "This code has a bug on line 5."
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result) as mock_subprocess:
            result = _run_codex_exec("Review this code", timeout=120)
        assert result == {"output": "This code has a bug on line 5."}
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        # Prompt should be passed via stdin (as '-')
        assert "-" in cmd

    def test_nonzero_exit_code(self):
        from modastack.mcp.codex_server import _run_codex_exec
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "something went wrong"
        with patch("subprocess.run", return_value=mock_result):
            result = _run_codex_exec("review this")
        assert "error" in result
        assert "something went wrong" in result["error"]

    def test_timeout(self):
        from modastack.mcp.codex_server import _run_codex_exec
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 300)):
            result = _run_codex_exec("review this", timeout=300)
        assert "error" in result
        assert "timed out" in result["error"]

    def test_os_error(self):
        from modastack.mcp.codex_server import _run_codex_exec
        with patch("subprocess.run", side_effect=FileNotFoundError("codex not found")):
            result = _run_codex_exec("review this")
        assert "error" in result


class TestMCPProtocol:
    def test_initialize(self):
        resp = _handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, [])
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "modastack-codex"

    def test_tools_list(self):
        resp = _handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, [])
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "codex_exec"

    def test_tools_call_unknown_tool(self):
        resp = _handle_jsonrpc({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "unknown_tool", "arguments": {}},
        }, [])
        assert resp["result"]["isError"] is True
        assert "Unknown tool" in resp["result"]["content"][0]["text"]

    def test_tools_call_routes_to_codex_exec(self):
        with patch("modastack.mcp.codex_server.codex_exec") as mock_exec:
            mock_exec.return_value = {"output": "all good"}
            resp = _handle_jsonrpc({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {
                    "name": "codex_exec",
                    "arguments": {"prompt": "review this code", "connection": "my-codex"},
                },
            }, SAMPLE_CONNECTIONS)
        assert resp["result"]["isError"] is False
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["output"] == "all good"

    def test_tools_call_error_propagated(self):
        with patch("modastack.mcp.codex_server.codex_exec") as mock_exec:
            mock_exec.return_value = {"error": "connection not found"}
            resp = _handle_jsonrpc({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {
                    "name": "codex_exec",
                    "arguments": {"prompt": "review", "connection": "missing"},
                },
            }, [])
        assert resp["result"]["isError"] is True

    def test_notifications_return_none(self):
        resp = _handle_jsonrpc({"method": "notifications/initialized"}, [])
        assert resp is None

    def test_unknown_method(self):
        resp = _handle_jsonrpc({"jsonrpc": "2.0", "id": 6, "method": "unknown/method"}, [])
        assert "error" in resp
        assert resp["error"]["code"] == -32601


class TestMCPInjectCodex:
    def test_injects_codex_server(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        connections = [
            ConnectionEntry(name="codex", kind="codex", provider="openai-codex", api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-codex" in result
        assert result["modastack-codex"]["type"] == "stdio"

    def test_no_injection_without_codex_connections(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        connections = [
            ConnectionEntry(name="chat", kind="chat", provider="openai", api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-codex" not in result

    def test_does_not_override_user_codex_server(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        existing = {"modastack-codex": {"type": "stdio", "command": "custom"}}
        connections = [
            ConnectionEntry(name="codex", kind="codex", provider="openai-codex", api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(existing, connections)
        assert result["modastack-codex"]["command"] == "custom"

    def test_both_image_and_codex_injected(self):
        from modastack.mcp.inject import inject_builtin_mcp_servers
        from modastack.config import ConnectionEntry

        connections = [
            ConnectionEntry(name="img", kind="image", provider="openai", api_key="sk-test"),
            ConnectionEntry(name="codex", kind="codex", provider="openai-codex", api_key="sk-test"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-image" in result
        assert "modastack-codex" in result
