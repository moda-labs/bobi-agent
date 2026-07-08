"""Tests for the direct MCP initialize handshake primitive (#428 Stage 4).

The handshake reaches an MCP server through the ``mcp`` SDK (stdio or streamable
http) and reports a Claude-``get_mcp_status``-shaped entry. The transport clients
are stubbed so these stay hermetic; the real end-to-end wiring is covered by the
codex round-trip in test_codex_config and the preflight tests.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from bobi import mcp_handshake


class _FakeSession:
    def __init__(self, tools=("a", "b"), fail_on=None):
        self._tools = tools
        self._fail_on = fail_on

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        if self._fail_on == "initialize":
            raise RuntimeError("initialize refused")

    async def list_tools(self):
        class _Result:
            def __init__(self, names):
                self.tools = [type("T", (), {"name": n}) for n in names]
        return _Result(self._tools)


def _patch_handshake(monkeypatch, tools=("a", "b")):
    """Bypass the real transport: feed _handshake a fake ClientSession."""
    async def _fake_handshake(read, write):
        async with _FakeSession(tools) as s:
            await s.initialize()
            return [t.name for t in (await s.list_tools()).tools]
    monkeypatch.setattr(mcp_handshake, "_handshake", _fake_handshake)


# --- probe_server -----------------------------------------------------------


def test_probe_stdio_connected(monkeypatch):
    _patch_handshake(monkeypatch, tools=("x", "y", "z"))

    @contextlib.asynccontextmanager
    async def _fake_stdio(params):
        yield ("r", "w")
    monkeypatch.setattr("mcp.client.stdio.stdio_client", _fake_stdio)

    out = asyncio.run(mcp_handshake.probe_server(
        "s", {"type": "stdio", "command": "/bin/s"}))
    assert out == {"name": "s", "status": "connected",
                   "tools": ["x", "y", "z"], "error": None}


def test_probe_http_connected(monkeypatch):
    _patch_handshake(monkeypatch, tools=("t",))

    @contextlib.asynccontextmanager
    async def _fake_http(url, headers=None):
        yield ("r", "w", None)
    monkeypatch.setattr(
        "mcp.client.streamable_http.streamablehttp_client", _fake_http)

    out = asyncio.run(mcp_handshake.probe_server(
        "h", {"type": "http", "url": "https://x/mcp"}))
    assert out["status"] == "connected"
    assert out["tools"] == ["t"]


def test_probe_stdio_missing_command_is_failed():
    out = asyncio.run(mcp_handshake.probe_server("s", {"type": "stdio"}))
    assert out["status"] == "failed"
    assert "command" in out["error"]


def test_probe_http_missing_url_is_failed():
    out = asyncio.run(mcp_handshake.probe_server("h", {"type": "http"}))
    assert out["status"] == "failed"
    assert "url" in out["error"]


def test_probe_launch_failure_becomes_failed(monkeypatch):
    @contextlib.asynccontextmanager
    async def _boom(params):
        raise FileNotFoundError("spawn /bin/nope ENOENT")
        yield  # pragma: no cover
    monkeypatch.setattr("mcp.client.stdio.stdio_client", _boom)

    out = asyncio.run(mcp_handshake.probe_server(
        "s", {"type": "stdio", "command": "/bin/nope"}))
    assert out["status"] == "failed"
    assert "ENOENT" in out["error"]


def test_probe_timeout_becomes_failed(monkeypatch):
    @contextlib.asynccontextmanager
    async def _hang(params):
        await asyncio.sleep(10)
        yield ("r", "w")  # pragma: no cover
    monkeypatch.setattr("mcp.client.stdio.stdio_client", _hang)

    out = asyncio.run(mcp_handshake.probe_server(
        "s", {"type": "stdio", "command": "/bin/slow"}, timeout=0.05))
    assert out["status"] == "failed"
    assert "timed out" in out["error"]


def test_preflight_timeout_reads_env(monkeypatch):
    monkeypatch.setenv("BOBI_MCP_PREFLIGHT_TIMEOUT", "60")
    assert mcp_handshake.preflight_timeout() == 60.0


def test_preflight_timeout_rejects_invalid_values(monkeypatch):
    for value in ("", "nope", "0", "-1", "inf", "-inf", "nan"):
        monkeypatch.setenv("BOBI_MCP_PREFLIGHT_TIMEOUT", value)
        assert mcp_handshake.preflight_timeout(default=12.0) == 12.0


# --- probe_servers (fan-out) ------------------------------------------------


def test_probe_servers_returns_get_mcp_status_shape(monkeypatch):
    async def _fake_one(name, spec, timeout, env):
        return {"name": name, "status": "connected", "tools": [], "error": None}
    monkeypatch.setattr(mcp_handshake, "probe_server", _fake_one)

    out = asyncio.run(mcp_handshake.probe_servers(
        {"a": {"command": "/a"}, "b": {"command": "/b"}}))
    assert set(s["name"] for s in out["mcpServers"]) == {"a", "b"}


def test_probe_servers_default_timeout_stays_standalone_default(monkeypatch):
    seen = []

    async def _fake_one(name, spec, timeout, env):
        seen.append(timeout)
        return {"name": name, "status": "connected", "tools": [], "error": None}

    monkeypatch.setenv("BOBI_MCP_PREFLIGHT_TIMEOUT", "60")
    monkeypatch.setattr(mcp_handshake, "probe_server", _fake_one)
    asyncio.run(mcp_handshake.probe_servers(
        {"a": {"command": "/a"}, "b": {"command": "/b"}}))
    assert seen == [mcp_handshake.DEFAULT_TIMEOUT, mcp_handshake.DEFAULT_TIMEOUT]


def test_probe_servers_empty_is_empty():
    assert asyncio.run(mcp_handshake.probe_servers({})) == {"mcpServers": []}
