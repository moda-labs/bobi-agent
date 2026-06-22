"""Auto-inject built-in MCP servers based on agent.yaml connections.

When the config has codex connections, this module adds the corresponding
MCP server to the mcp_servers dict passed to ClaudeAgentOptions.

Image generation no longer goes through an injected MCP server: the
``kind: image`` shim was retired in #397 in favor of a direct OpenAI Images
API call (``curl`` + ``jq`` + ``base64`` — see ``tools/image.md``). The
remaining ``kind: codex`` branch is itself slated for teardown in #403.
"""

from __future__ import annotations

import json
import sys


def inject_builtin_mcp_servers(
    mcp_servers: dict[str, dict] | None,
    connections: list,
) -> dict[str, dict]:
    """Merge built-in MCP servers into the user-supplied mcp_servers dict.

    Currently adds:
    - modastack-codex: when any connection with kind=codex exists

    A ``kind: image`` connection injects nothing (the image MCP shim was
    retired in #397; image generation is a baked CLI capability now).
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
