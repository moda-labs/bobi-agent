"""Signed client for the event server's channel gateway endpoints (#190).

The gateway owns all outbound chat delivery: formatting (markdown goes out
raw and is rendered by the channel), typing UX, credentials, and
capability degradation. This module is the only Python path that talks to
``/channels/*`` - the CLI (``bobi reply``, ``bobi read-conversation``) and
the drain loop's input channel policy both call through here.

Requests are HMAC-signed with the instance's bubble key (same scheme as
:mod:`bobi.events.publish`). The channel credential itself lives on the
event server, registered at session start (``register_slack_workspaces``);
no Slack token is read or transmitted here.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from urllib.parse import urlencode

log = logging.getLogger(__name__)


class GatewayError(RuntimeError):
    """A gateway request failed - carries a human-readable reason."""


def _request(project_path: Path | None, method: str, path: str,
             payload: dict | None = None, *, timeout: float = 30.0) -> dict:
    """Send a bubble-signed request to the gateway and return the JSON body.

    ``path`` includes the query string when present; the signature covers the
    exact path and body bytes transmitted. Raises :class:`GatewayError` with
    the server's error message on any failure.
    """
    from bobi.events.signing import (
        SignedJSONRequestError,
        checked_signed_json_request,
    )

    try:
        return checked_signed_json_request(
            project_path, method, path, payload, timeout=timeout)
    except SignedJSONRequestError as exc:
        if exc.kind == "missing_credentials":
            raise GatewayError(
                "No bubble credential found - is the agent started? The channel "
                "gateway only accepts requests signed with the instance's bubble key."
            ) from exc
        if exc.kind == "unreachable":
            raise GatewayError(str(exc)) from exc
        detail = exc.detail or exc.response_text or (
            f"HTTP {exc.status_code}" if exc.status_code is not None
            else "invalid response"
        )
        raise GatewayError(
            f"Gateway {method} {path.split('?')[0]} failed: {detail}"
        ) from exc

def channels_send(project_path: Path | None, conversation: str, text: str = "",
                  *, mode: str = "post", edit_ref: str = "",
                  files: list[dict] | None = None,
                  timeout: float = 30.0) -> dict:
    """POST /channels/send. Text is raw markdown; the gateway formats it.

    ``mode`` is ``post | update | final`` (final = edit ``edit_ref`` when
    given, else post, then clear the typing indicator). ``files`` entries are
    ``{name, content_b64, title?}``. Returns the response dict (``ts`` is the
    posted/updated message id).
    """
    payload: dict = {"conversation": conversation, "mode": mode}
    if text:
        payload["text"] = text
    if edit_ref:
        payload["edit_ref"] = edit_ref
    if files:
        payload["files"] = files
    return _request(project_path, "POST", "/channels/send", payload,
                    timeout=timeout)


def channels_typing(project_path: Path | None, conversation: str,
                    on: bool) -> bool:
    """POST /channels/typing. Best-effort: returns False instead of raising,
    so a typing hiccup never breaks event delivery or a reply."""
    try:
        _request(project_path, "POST", "/channels/typing",
                 {"conversation": conversation, "on": bool(on)}, timeout=10.0)
        return True
    except GatewayError as exc:
        log.debug("channels/typing failed for %s: %s", conversation, exc)
        return False


def channels_history(project_path: Path | None, conversation: str,
                     limit: int = 100) -> list[dict]:
    """GET /channels/history - the conversation's messages, oldest-first."""
    query = urlencode({"conversation": conversation, "limit": limit})
    data = _request(project_path, "GET", f"/channels/history?{query}")
    messages = data.get("messages", [])
    return messages if isinstance(messages, list) else []


def file_payload(path: Path, *, title: str = "", filename: str = "") -> dict:
    """Build one /channels/send files[] entry from a local file."""
    entry: dict = {
        "name": filename or path.name,
        "content_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    if title:
        entry["title"] = title
    return entry
