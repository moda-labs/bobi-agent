"""Venn.ai REST API client for startup validation.

Checks that required non-native services are connected in the user's
Venn account before starting the agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

VENN_API_BASE = "https://app.venn.ai/api/tooliq"

SERVICE_ALIASES: dict[str, list[str]] = {
    "email": ["gmail", "outlook"],
    "calendar": ["googlecalendar", "outlook-calendar"],
    "docs": ["googledocs", "notion"],
    "sheets": ["googlesheets"],
    "slides": ["googleslides"],
    "storage": ["gdrive", "dropbox"],
    "crm": ["salesforce", "hubspot"],
    "chat": ["slack", "discord"],
    "tickets": ["jira", "linear", "asana"],
}


@dataclass
class VennServer:
    server_id: str
    server_name: str
    connected: bool


@dataclass
class ServiceCheck:
    connected: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def list_servers(api_key: str) -> list[VennServer]:
    """Fetch connected servers from Venn REST API."""
    import urllib.request
    import json

    req = urllib.request.Request(
        f"{VENN_API_BASE}/tools/help",
        data=json.dumps({"action": "list_servers"}).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        log.error(f"Failed to query Venn API: {e}")
        return []

    servers = body.get("result", {}).get("servers", [])
    return [
        VennServer(
            server_id=s.get("server_id", ""),
            server_name=s.get("server_name", ""),
            connected=s.get("connected", False),
        )
        for s in servers
    ]


def check_services(api_key: str, required: list[str]) -> ServiceCheck:
    """Validate required services are connected in Venn.

    Matches service names against Venn's server_name values, with alias
    expansion (e.g., "email" matches "gmail" or "outlook").
    """
    servers = list_servers(api_key)
    connected_names = {s.server_name for s in servers if s.connected}

    result = ServiceCheck()
    for service in required:
        candidates = SERVICE_ALIASES.get(service, [service])
        if any(c in connected_names for c in candidates):
            result.connected.append(service)
        else:
            result.missing.append(service)

    return result


def format_service_report(
    check: ServiceCheck,
    native_services: list[str] | None = None,
) -> str:
    """Format a human-readable service status report for startup."""
    lines = ["Services:"]
    for name in native_services or []:
        lines.append(f"  ✓ {name:20} (native)")
    for name in check.connected:
        lines.append(f"  ✓ {name:20} (venn)")
    for name in check.missing:
        lines.append(f"  ✗ {name:20} (venn — not connected)")
        lines.append(f"    → Connect at venn.ai, then restart")
    return "\n".join(lines)
