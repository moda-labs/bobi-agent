"""Signed client for the event server's scoped ingest-token API (#640).

Mint / list / revoke tokens bound to (bubble, topic). Every call is
bubble-signed (the same auth as a generic publish - see signing.py); the
plaintext token appears only in the mint response and is never persisted
by either side.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from bobi import http as pooled
from bobi.events.publish import DEFAULT_URL

log = logging.getLogger(__name__)


class IngestTokenError(Exception):
    """A token operation the server rejected, with a printable reason."""


def _signed_request(method: str, path: str, body: str,
                    project_path: Path | None) -> dict:
    if project_path is None:
        from bobi.paths import bobi_root
        project_path = bobi_root()
    else:
        project_path = Path(project_path)

    from bobi.config import Config, load_bubble_state
    from bobi.events.signing import sign_headers

    bubble = load_bubble_state(project_path)
    if not (bubble.get("bubble_id") and bubble.get("bubble_key")):
        raise IngestTokenError(
            "No bubble credential found. Start the agent first - ingest "
            "tokens are minted with the instance's bubble identity."
        )

    try:
        es_url = Config.load(project_path).event_server_url or DEFAULT_URL
    except Exception:
        es_url = DEFAULT_URL

    headers = {"Content-Type": "application/json"} if body else {}
    headers.update(sign_headers(
        bubble["bubble_id"], bubble["bubble_key"], method, path, body,
    ))

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
    if resp.status_code >= 400:
        reason = data.get("error") if isinstance(data, dict) else None
        raise IngestTokenError(
            f"Server rejected {method} {path} ({resp.status_code}): "
            f"{reason or resp.text[:200]}"
        )
    return data if isinstance(data, dict) else {}


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
