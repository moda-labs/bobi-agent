"""Signed client for the event server's scoped ingest-token API (#640).

Mint / list / revoke tokens bound to (bubble, topic). Every call goes out
through the shared signed transport (:func:`bobi.events.signing.signed_request`)
with the instance's bubble credential - the same auth as a generic publish.
The plaintext token appears only in the mint response and is never persisted
by either side.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx


class IngestTokenError(Exception):
    """A token operation the server rejected, with a printable reason."""


def _request(method: str, path: str, payload: dict | None,
             project_path: Path | None) -> dict:
    from bobi.events.publish import bubble_context
    from bobi.events.signing import signed_request

    es_url, bubble_id, bubble_key = bubble_context(project_path)
    if not (bubble_id and bubble_key):
        raise IngestTokenError(
            "No bubble credential found. Start the agent first - ingest "
            "tokens are minted with the instance's bubble identity."
        )

    try:
        resp = signed_request(es_url, method, path, payload,
                              bubble_id, bubble_key, timeout=10.0)
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
    payload: dict = {"topic": topic}
    if name:
        payload["name"] = name
    return _request("POST", "/ingest-tokens", payload, project_path)


def list_tokens(project_path: Path | None = None) -> list[dict]:
    data = _request("GET", "/ingest-tokens", None, project_path)
    tokens = data.get("tokens", [])
    return tokens if isinstance(tokens, list) else []


def revoke_token(token_id: str, project_path: Path | None = None) -> None:
    # Quote so the signed path and the wire path stay byte-identical even for
    # ids that httpx would percent-encode (the signature covers the exact path).
    _request("DELETE", f"/ingest-tokens/{quote(token_id, safe='')}", None,
             project_path)
