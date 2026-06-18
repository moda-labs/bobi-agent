"""Auto-inject built-in MCP servers based on agent.yaml connections.

When the config has image or codex connections, this module adds the
corresponding MCP server to the mcp_servers dict passed to
ClaudeAgentOptions.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


def inject_builtin_mcp_servers(
    mcp_servers: dict[str, dict] | None,
    connections: list,
) -> dict[str, dict]:
    """Merge built-in MCP servers into the user-supplied mcp_servers dict.

    Currently adds:
    - modastack-image: when any connection with kind=image exists
    - modastack-codex: when any connection with kind=codex exists
    """
    result = dict(mcp_servers or {})

    def _has_kind(kind: str) -> bool:
        return any(
            getattr(c, "kind", "") == kind
            or (isinstance(c, dict) and c.get("kind") == kind)
            for c in connections
        )

    def _conn_dicts() -> list[dict]:
        out: list[dict] = []
        for c in connections:
            if hasattr(c, "__dataclass_fields__"):
                from dataclasses import asdict
                out.append(asdict(c))
            elif isinstance(c, dict):
                out.append(c)
        return out

    # Inject image server if image connections are configured
    if _has_kind("image"):
        result.setdefault("modastack-image", {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-m", "modastack.mcp.image_server",
                json.dumps(_conn_dicts()),
            ],
        })

    # Inject codex server if codex connections are configured
    if _has_kind("codex"):
        result.setdefault("modastack-codex", {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-m", "modastack.mcp.codex_server",
                json.dumps(_conn_dicts()),
            ],
        })

    return result
