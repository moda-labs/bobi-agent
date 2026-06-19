"""Auto-inject built-in MCP servers based on agent.yaml connections.

Uses a harness registry to map connection kinds to MCP server modules.
When the config has connections of a registered kind, this module adds
the corresponding MCP server to the mcp_servers dict passed to
ClaudeAgentOptions.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Harness registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HarnessSpec:
    """Describes one built-in MCP server that can be auto-injected."""

    name: str           # MCP server key (e.g. "modastack-image")
    kind: str           # connection kind that triggers injection (e.g. "image")
    module: str         # Python module path (e.g. "modastack.mcp.image_server")
    version: str = "1.0.0"
    provider_tag: str = ""  # optional provider filter ("" = any provider)


# The registry is a simple list — order does not matter because each
# harness is keyed by its ``name`` and triggers independently.
_HARNESS_REGISTRY: list[HarnessSpec] = [
    HarnessSpec(
        name="modastack-image",
        kind="image",
        module="modastack.mcp.image_server",
    ),
    HarnessSpec(
        name="modastack-codex",
        kind="codex",
        module="modastack.mcp.codex_server",
    ),
    HarnessSpec(
        name="modastack-gateway",
        kind="gateway",
        module="modastack.mcp.gateway_server",
    ),
]


def get_registry() -> list[HarnessSpec]:
    """Return a copy of the harness registry."""
    return list(_HARNESS_REGISTRY)


def register_harness(spec: HarnessSpec) -> None:
    """Add a harness spec to the registry.

    If a spec with the same name already exists it is replaced.
    """
    for i, existing in enumerate(_HARNESS_REGISTRY):
        if existing.name == spec.name:
            _HARNESS_REGISTRY[i] = spec
            return
    _HARNESS_REGISTRY.append(spec)


# ---------------------------------------------------------------------------
# Injection logic
# ---------------------------------------------------------------------------

def inject_builtin_mcp_servers(
    mcp_servers: dict[str, dict] | None,
    connections: list,
) -> dict[str, dict]:
    """Merge built-in MCP servers into the user-supplied mcp_servers dict.

    Iterates the harness registry and injects each server whose
    ``kind`` matches at least one configured connection.  User-supplied
    entries with the same key are never overwritten (``setdefault``).
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

    for spec in _HARNESS_REGISTRY:
        if _has_kind(spec.kind):
            result.setdefault(spec.name, {
                "type": "stdio",
                "command": sys.executable,
                "args": [
                    "-m", spec.module,
                    json.dumps(_conn_dicts()),
                ],
            })

    return result
