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


class IngestTokenError(Exception):
    """A token operation the server rejected, with a printable reason."""


def _request(method: str, path: str, payload: dict | None,
             project_path: Path | None) -> dict:
    from bobi.events.signing import (
        SignedJSONRequestError,
        checked_signed_json_request,
    )

    try:
        return checked_signed_json_request(
            project_path, method, path, payload, timeout=10.0)
    except SignedJSONRequestError as exc:
        if exc.kind == "missing_credentials":
            raise IngestTokenError(
                "No bubble credential found. Start the agent first - ingest "
                "tokens are minted with the instance's bubble identity."
            ) from exc
        if exc.kind == "unreachable":
            raise IngestTokenError(str(exc)) from exc
        detail = exc.detail or exc.response_text or (
            f"HTTP {exc.status_code}" if exc.status_code is not None
            else "invalid response"
        )
        raise IngestTokenError(
            f"Server rejected {method} {path} ({exc.status_code}): {detail}"
        ) from exc


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
