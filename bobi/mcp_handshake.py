"""Direct MCP ``initialize`` handshake over the SDK-native server spec (#428
Stage 4).

The runtime preflight (``validate._async_probe_mcp``) verifies MCP servers by
asking the *active brain's* session for its MCP status. Claude's SDK exposes
``get_mcp_status`` for free; Codex's ``codex exec`` reads ``~/.codex/config.toml``
but offers no status introspection (``codex mcp list`` only echoes config, it
does not connect). So a brain that can't self-report needs a way to actually
*reach* each configured server and confirm it answers ``initialize`` +
``tools/list``.

This module is that primitive: given a server's SDK-native spec
(``type: stdio|http|sse``; stdio ``command``/``args``/``env``; http/sse
``url``/``headers``) it opens the transport with the ``mcp`` SDK, runs the
handshake, and reports a status entry in the **same shape** Claude's
``get_mcp_status`` returns (``{"name", "status", "tools", "error"}``). That lets
the codex adapter expose a ``get_mcp_status`` and keep the preflight a single
loop across brains — no warn-degrade special case.

It is deliberately separate from ``bobi.setup.mcp_probe`` (the setup-wizard
connection tester, which speaks the wizard's entry-dict shape and does safe-tool
call-through/scrubbing): this one verifies runtime ``mcp_servers`` initialize,
nothing more.
"""

from __future__ import annotations

import asyncio
import math
import os

# A first stdio launch may resolve dependencies (npx/uvx download) before the
# server answers initialize. Keep the standalone handshake default unchanged,
# while the agent startup preflight uses the separate 10s ceiling shared with
# validate's MCP poll loop.
PREFLIGHT_TIMEOUT_ENV = "BOBI_MCP_PREFLIGHT_TIMEOUT"
PREFLIGHT_DEFAULT_TIMEOUT = 10.0
DEFAULT_TIMEOUT = 20.0


def preflight_timeout(default: float = PREFLIGHT_DEFAULT_TIMEOUT) -> float:
    """Configured MCP preflight timeout in seconds.

    Invalid, empty, zero, or negative values fall back to the caller's default
    so a typo in the environment does not accidentally disable startup checks.
    """
    raw = os.environ.get(PREFLIGHT_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


async def _handshake(read, write) -> list[str]:
    """initialize + list tools; return the tool names (proves the connection)."""
    from mcp import ClientSession

    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        return [t.name for t in tools]


async def _probe_stdio(spec: dict, base_env: dict | None) -> list[str]:
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = str(spec.get("command") or "").strip()
    if not command:
        raise ValueError("stdio server has no command")
    # Merge the caller's env (the runtime agent-spawn env, so bare commands
    # resolve on the same PATH the server will actually launch under) with the
    # spec's env winning. Falls back to the ambient process env.
    base = dict(base_env if base_env is not None else os.environ)
    env = {**base, **{k: str(v) for k, v in (spec.get("env") or {}).items()}}
    params = StdioServerParameters(
        command=command,
        args=[str(a) for a in (spec.get("args") or [])],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        return await _handshake(read, write)


async def _probe_http(spec: dict) -> list[str]:
    from mcp.client.streamable_http import streamablehttp_client

    url = str(spec.get("url") or "").strip()
    if not url:
        raise ValueError("http/sse server has no url")
    headers = {k: str(v) for k, v in (spec.get("headers") or {}).items()}
    async with streamablehttp_client(url, headers=headers) as streams:
        return await _handshake(streams[0], streams[1])


async def probe_server(name: str, spec: dict,
                       timeout: float = DEFAULT_TIMEOUT,
                       env: dict | None = None) -> dict:
    """Handshake one server; return a Claude-``get_mcp_status``-shaped entry.

    ``{"name", "status": "connected"|"failed", "tools": [...], "error": ...}`` —
    never raises: a launch/handshake failure becomes ``status: "failed"`` with
    the error text, so a probe loop can judge every server uniformly. ``env`` is
    the base environment for stdio launches (defaults to the process env).
    """
    spec = spec or {}
    server_type = str(spec.get("type") or "stdio")
    try:
        coro = _probe_http(spec) if server_type in ("http", "sse") \
            else _probe_stdio(spec, env)
        tools = await asyncio.wait_for(coro, timeout=timeout)
        return {"name": name, "status": "connected", "tools": tools, "error": None}
    except asyncio.TimeoutError:
        return {"name": name, "status": "failed", "tools": [],
                "error": f"timed out after {timeout:g}s"}
    except Exception as e:  # noqa: BLE001 — surface any launch/handshake failure
        return {"name": name, "status": "failed", "tools": [],
                "error": str(e) or type(e).__name__}


async def probe_servers(mcp_servers: dict,
                        timeout: float = DEFAULT_TIMEOUT,
                        env: dict | None = None) -> dict:
    """Handshake every server concurrently; return a ``get_mcp_status`` payload
    (``{"mcpServers": [entry, ...]}``). ``env`` is the base stdio launch env."""
    servers = mcp_servers or {}
    entries = await asyncio.gather(
        *(probe_server(name, spec, timeout, env) for name, spec in servers.items())
    )
    return {"mcpServers": list(entries)}
