"""Signed client for the event server's scoped ingest-token API (#640).

Mint / list / revoke tokens bound to (bubble, topic). Every call is
bubble-signed (the same auth as a generic publish - see signing.py); the
plaintext token appears only in the mint response and is never persisted
by either side. Instance context (event-server URL, bubble credential)
resolves through the gateway's shared helper so all signed clients agree
on where the server is and how the bubble loads.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from bobi import http as pooled
from bobi.events.gateway import GatewayError, _gateway_context

log = logging.getLogger(__name__)


class IngestTokenError(Exception):
    """A token operation the server rejected, with a printable reason."""


def _signed_request(method: str, path: str, body: str,
                    project_path: Path | None) -> dict:
    from bobi.events.signing import sign_headers

    try:
        es_url, bubble_id, bubble_key = _gateway_context(project_path)
    except GatewayError as e:
        raise IngestTokenError(
            "No bubble credential found. Start the agent first - ingest "
            "tokens are minted with the instance's bubble identity."
        ) from e

    headers = {"Content-Type": "application/json"} if body else {}
    headers.update(sign_headers(bubble_id, bubble_key, method, path, body))

    try:
        resp = pooled.request(
            method, f"{es_url}{path}",
            content=body or None, headers=headers, timeout=10.0,
        )
    except (httpx.HTTPError, OSError, TimeoutError) as e:
        raise IngestTokenError(f"Event server unreachable at {es_url}: {e}") from e

    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code >= 400:
        raise IngestTokenError(
            f"Server rejected {method} {path} ({resp.status_code}): "
            f"{data.get('error') or resp.text[:200]}"
        )
    return data


def create_token(topic: str, name: str | None = None,
                 project_path: Path | None = None) -> dict:
    """Mint a token bound to ``topic``. Returns the server's mint response;
    ``token`` in it is shown once and never recoverable afterwards."""
    from bobi.events.signing import serialize_body

    payload: dict = {"topic": topic}
    if name:
        payload["name"] = name
    body = serialize_body(payload)
    return _signed_request("POST", "/ingest-tokens", body, project_path)


def list_tokens(project_path: Path | None = None) -> list[dict]:
    data = _signed_request("GET", "/ingest-tokens", "", project_path)
    tokens = data.get("tokens", [])
    return tokens if isinstance(tokens, list) else []


def revoke_token(token_id: str, project_path: Path | None = None) -> None:
    _signed_request("DELETE", f"/ingest-tokens/{token_id}", "", project_path)
