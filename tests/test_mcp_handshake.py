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
    # The transport yields mcp 2.0's 2-tuple: the caller may touch only
    # streams[0]/streams[1], so a 3-unpack regression fails here.
    _patch_handshake(monkeypatch, tools=("t",))

    @contextlib.asynccontextmanager
    async def _fake_http(url, headers=None):
        yield ("r", "w")
    monkeypatch.setattr(mcp_handshake, "open_streamable_http", _fake_http)

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


# --- open_streamable_http across the mcp 2.0 rename -------------------------


def _fake_transport_module(**attrs):
    import types
    mod = types.ModuleType("mcp.client.streamable_http")
    for name, value in attrs.items():
        setattr(mod, name, value)
    return mod


def test_open_streamable_http_new_spelling(monkeypatch):
    """mcp >= 1.25 / 2.0: headers ride an SDK-built httpx client.

    The transport module here deliberately does NOT carry the factory
    re-export: the factory resolves from its real home
    (mcp.shared._httpx_utils) and must never decide which transport branch
    runs — bundling it into the branch `try` reproduced the #1045 breakage
    shape (new spelling present, factory absent → legacy-branch ImportError
    naming the wrong symbol).
    """
    import sys
    import types
    seen = {}

    @contextlib.asynccontextmanager
    async def _client(url, *, http_client=None, terminate_on_close=True):
        seen["url"], seen["http_client"] = url, http_client
        yield ("r", "w")   # mcp 2.0 yields (read, write) — no session-id slot

    class _HttpClient:
        def __init__(self):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            return False

    def _create(headers=None):
        seen["headers"] = headers
        seen["client"] = _HttpClient()
        return seen["client"]

    utils = types.ModuleType("mcp.shared._httpx_utils")
    utils.create_mcp_http_client = _create
    monkeypatch.setitem(sys.modules, "mcp.shared._httpx_utils", utils)
    monkeypatch.setitem(
        sys.modules, "mcp.client.streamable_http",
        _fake_transport_module(streamable_http_client=_client))

    async def _run():
        async with mcp_handshake.open_streamable_http(
                "https://x/mcp", headers={"a": "b"}) as streams:
            return streams

    assert asyncio.run(_run()) == ("r", "w")
    assert seen["url"] == "https://x/mcp"
    assert seen["headers"] == {"a": "b"}
    assert seen["http_client"] is seen["client"]
    assert seen["client"].closed   # the SDK-built client is closed on exit


def test_open_streamable_http_builds_its_own_client_without_the_factory(
        monkeypatch):
    """New spelling present but the private factory module gone: fall back to
    a hand-built httpx client with the same MCP defaults — never the legacy
    import branch."""
    import sys
    seen = {}

    @contextlib.asynccontextmanager
    async def _client(url, *, http_client=None, terminate_on_close=True):
        seen["http_client"] = http_client
        yield ("r", "w")

    monkeypatch.setitem(
        sys.modules, "mcp.client.streamable_http",
        _fake_transport_module(streamable_http_client=_client))
    monkeypatch.setitem(sys.modules, "mcp.shared._httpx_utils", None)

    async def _run():
        async with mcp_handshake.open_streamable_http(
                "https://x/mcp", headers={"a": "b"}) as streams:
            return streams

    # Plain httpx, deliberately: every mcp the <2 pin permits types its
    # transport against httpx, so the fallback must never hand it an
    # httpx2 client even when httpx2 happens to be installed.
    import httpx
    assert asyncio.run(_run()) == ("r", "w")
    client = seen["http_client"]
    assert isinstance(client, httpx.AsyncClient)
    assert client.follow_redirects is True
    assert client.timeout.connect == 30.0
    assert client.timeout.read == 300.0
    assert client.headers["a"] == "b"


def test_real_installed_sdk_ships_the_symbols_the_shim_relies_on():
    """Contract test against the ACTUAL installed mcp: the shim's branch
    condition and both transports' imports must resolve. (The old string-patch
    was a de-facto pin on the SDK's names; this replaces it.)"""
    import mcp.client.streamable_http as transport
    assert (hasattr(transport, "streamable_http_client")
            or hasattr(transport, "streamablehttp_client"))
    from mcp import ClientSession, StdioServerParameters  # noqa: F401
    from mcp.client.stdio import stdio_client  # noqa: F401


def test_open_streamable_http_legacy_spelling(monkeypatch):
    """mcp < 1.25 ships only streamablehttp_client, which takes headers."""
    import sys
    seen = {}

    @contextlib.asynccontextmanager
    async def _legacy(url, headers=None):
        seen["url"], seen["headers"] = url, headers
        yield ("r", "w", None)

    monkeypatch.setitem(
        sys.modules, "mcp.client.streamable_http",
        _fake_transport_module(streamablehttp_client=_legacy))

    async def _run():
        async with mcp_handshake.open_streamable_http(
                "https://x/mcp", headers={"a": "b"}) as streams:
            return streams

    assert asyncio.run(_run()) == ("r", "w", None)
    assert seen == {"url": "https://x/mcp", "headers": {"a": "b"}}
