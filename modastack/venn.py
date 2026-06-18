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


class VennError(Exception):
    """Venn could not be queried with the given key — an auth failure (the key
    is wrong), a non-2xx response, an error body, or a transport problem. Raised
    only by `list_servers_verified`; `list_servers` masks these as an empty list
    for resilient startup checks."""


def _servers_from_body(body: dict) -> list[VennServer]:
    servers = body.get("result", {}).get("servers", [])
    return [
        VennServer(
            server_id=s.get("server_id", ""),
            server_name=s.get("server_name", ""),
            connected=s.get("connected", False),
        )
        for s in servers
    ]


def list_servers(api_key: str) -> list[VennServer]:
    """Fetch connected servers from Venn REST API. Resilient: returns [] on any
    error (used by startup/validation where a failure shouldn't crash)."""
    from modastack import http as pooled

    try:
        resp = pooled.post(
            f"{VENN_API_BASE}/tools/help",
            json={"action": "list_servers"},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )
        body = resp.json()
    except Exception as e:
        log.error(f"Failed to query Venn API: {e}")
        return []

    return _servers_from_body(body)


def list_servers_verified(api_key: str) -> list[VennServer]:
    """Like `list_servers`, but RAISES `VennError` when the key can't be
    verified instead of masking it as an empty list. Distinguishes "valid key,
    zero MCPs" (returns []) from "bad key / failure" (raises) — so setup's Venn
    modal can show an error state rather than a false "connected, 0 services"."""
    from modastack import http as pooled

    try:
        resp = pooled.post(
            f"{VENN_API_BASE}/tools/help",
            json={"action": "list_servers"},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )
    except Exception as e:
        raise VennError(f"couldn't reach Venn ({e})") from e

    if resp.status_code in (401, 403):
        raise VennError("Venn rejected the API key (unauthorized).")
    if resp.status_code >= 400:
        raise VennError(f"Venn returned HTTP {resp.status_code}.")
    try:
        body = resp.json()
    except Exception as e:
        raise VennError(f"unexpected response from Venn ({e}).") from e
    if not isinstance(body, dict):
        raise VennError("unexpected response from Venn.")
    # Some gateways answer 200 with an error envelope for a bad key.
    err = body.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else err
        raise VennError(f"Venn rejected the request ({msg}).")
    return _servers_from_body(body)


def list_available_services(api_key: str) -> set[str]:
    """Every service Venn supports for this account — connected or not — as
    lowercased server names. This is the *real* catalog (what Venn can reach),
    as opposed to the curated buckets in SERVICE_ALIASES. Empty on any error."""
    return {s.server_name.lower() for s in list_servers(api_key) if s.server_name}


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
    """Format a human-readable service status report for startup.

    native_services: names of services with a registered ingestion adapter.
    """
    lines = ["Services:"]
    for name in native_services or []:
        lines.append(f"  ✓ {name:20} (native)")
    for name in check.connected:
        lines.append(f"  ✓ {name:20} (venn)")
    for name in check.missing:
        lines.append(f"  ✗ {name:20} (venn — not connected)")
        lines.append(f"    → Connect at venn.ai, then restart")
    return "\n".join(lines)
