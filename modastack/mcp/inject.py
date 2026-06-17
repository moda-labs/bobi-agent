"""Auto-inject built-in MCP servers based on agent.yaml connections.

When the config has image connections, this module adds the
generate_image MCP server to the mcp_servers dict passed to
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
    """
    result = dict(mcp_servers or {})

    # Inject image server if image connections are configured
    image_conns = [c for c in connections if getattr(c, "kind", "") == "image"
                   or (isinstance(c, dict) and c.get("kind") == "image")]
    if image_conns:
        # Serialize connections for the server subprocess
        conn_dicts = []
        for c in connections:
            if hasattr(c, "__dataclass_fields__"):
                from dataclasses import asdict
                conn_dicts.append(asdict(c))
            elif isinstance(c, dict):
                conn_dicts.append(c)

        result.setdefault("modastack-image", {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-m", "modastack.mcp.image_server",
                json.dumps(conn_dicts),
            ],
        })

    return result
