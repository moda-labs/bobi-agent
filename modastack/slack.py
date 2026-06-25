"""Slack message helpers — posting, editing, and typing status.

Shared by the CLI (``slack-reply``) and the event drain loop.
Handles markdown → Slack mrkdwn formatting, truncation, and the
HTTP calls to chat.postMessage, chat.update, and
assistant.threads.setStatus.

All errors are raised unless documented otherwise — callers decide
how to handle failures.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from modastack import http as pooled

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_slack_message(text: str) -> str:
    """Convert markdown to Slack mrkdwn and truncate if needed."""
    # Escaped newlines from shell invocations
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    # Headings → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # Bold markdown → Slack bold
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Unordered list markers → bullet character
    text = re.sub(r'^( *)[-*] ', r'\1• ', text, flags=re.MULTILINE)
    # Links
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
    if len(text) > 3000:
        text = text[:3000] + '\n_(truncated)_'
    return text


# ---------------------------------------------------------------------------
# Post / Update
# ---------------------------------------------------------------------------

def _slack_api(
    endpoint: str,
    token: str,
    payload: dict,
    *,
    timeout: float = 10,
) -> dict:
    """POST to a Slack Web API endpoint and return the parsed response.

    Raises ``RuntimeError`` for non-ok responses.
    """
    resp = pooled.post(
        f"https://slack.com/api/{endpoint}",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    result = resp.json()

    if not result.get("ok"):
        raise RuntimeError(f"Slack API error: {result.get('error', 'unknown')}")

    return result


def resolve_channel_id(token: str, channel: str, *, timeout: float = 10) -> str:
    """Resolve a Slack channel reference to its channel ID.

    Accepts either an ID (``C…``/``G…``/``D…``, returned unchanged) or a human
    name (with or without a leading ``#``), looked up via ``conversations.list``.
    Matches public and private channels, falling back to public-only if the token
    lacks ``groups:read``. Lets the config carry ``#codex-test`` instead of an
    opaque ``C0…`` id. Raises ``RuntimeError`` if a name can't be resolved.
    """
    ref = (channel or "").strip()
    if not ref:
        return ref
    # Already an ID? IDs start with C/G/D and are uppercase alphanumerics; a
    # '#'-prefixed value is always a name.
    if not ref.startswith("#") and re.fullmatch(r"[CGD][A-Z0-9]{6,}", ref):
        return ref
    want = ref.lstrip("#").lower()

    types = "public_channel,private_channel"
    cursor = ""
    while True:
        params: dict = {"types": types, "limit": 1000, "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        resp = pooled.client().get(
            "https://slack.com/api/conversations.list",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        result = resp.json()
        if not result.get("ok"):
            err = result.get("error", "unknown")
            # Private channels need groups:read; degrade to public-only rather
            # than fail when the app wasn't granted it.
            if err == "missing_scope" and types != "public_channel":
                types, cursor = "public_channel", ""
                continue
            raise RuntimeError(f"Slack API error: {err}")
        for ch in result.get("channels", []):
            if (ch.get("name") or "").lower() == want:
                return ch["id"]
        cursor = (result.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break
    raise RuntimeError(
        f"Slack channel '#{want}' not found — is the bot a member? "
        "(a private channel also needs the groups:read scope)."
    )


def post_slack_message(
    token: str,
    channel: str,
    text: str,
    thread_ts: str = "",
    *,
    timeout: float = 10,
) -> dict:
    """Post a message to Slack and return the API response dict.

    Raises on network errors or non-ok Slack responses.
    """
    text = format_slack_message(text)

    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    return _slack_api("chat.postMessage", token, payload, timeout=timeout)


def update_slack_message(
    token: str,
    channel: str,
    ts: str,
    text: str,
    *,
    timeout: float = 10,
) -> dict:
    """Edit an existing Slack message (chat.update) and return the response.

    Raises on network errors or non-ok Slack responses.
    """
    text = format_slack_message(text)

    payload: dict = {"channel": channel, "ts": ts, "text": text}

    return _slack_api("chat.update", token, payload, timeout=timeout)


# ---------------------------------------------------------------------------
# Typing status
# ---------------------------------------------------------------------------

def set_thread_status(
    token: str,
    channel: str,
    thread_ts: str,
    status: str,
    *,
    timeout: float = 10,
) -> None:
    """Set or clear the assistant typing status in a Slack thread.

    Calls ``assistant.threads.setStatus``.  Only works in thread context —
    silently no-ops if *thread_ts* is empty.  Failures are logged at debug
    level and swallowed (non-fatal), matching the Hermes pattern.
    """
    if not thread_ts:
        return

    payload: dict = {
        "channel_id": channel,
        "thread_ts": thread_ts,
        "status": status,
    }

    try:
        resp = pooled.post(
            "https://slack.com/api/assistant.threads.setStatus",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        result = resp.json()
        if not result.get("ok"):
            log.debug("setStatus failed: %s", result.get("error", "unknown"))
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        log.debug("setStatus request failed: %s", exc)


# ---------------------------------------------------------------------------
# File download / upload
# ---------------------------------------------------------------------------

def download_slack_file(
    token: str,
    url: str,
    *,
    timeout: float = 30,
) -> tuple[bytes, str]:
    """Download a file from Slack using its ``url_private`` or ``url_private_download``.

    Returns ``(content_bytes, content_type)``.
    Raises on network errors or non-2xx responses.
    """
    resp = pooled.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Slack file download failed: HTTP {resp.status_code}"
        )
    content_type = resp.headers.get("content-type", "application/octet-stream")
    return resp.content, content_type


def upload_slack_file(
    token: str,
    channel: str,
    file_data: bytes,
    filename: str,
    *,
    title: str = "",
    thread_ts: str = "",
    initial_comment: str = "",
    timeout: float = 30,
) -> dict:
    """Upload a file to a Slack channel using the V2 upload flow.

    1. ``files.getUploadURLExternal`` — get a presigned upload URL
    2. POST file bytes to that URL
    3. ``files.completeUploadExternal`` — share the file in the channel

    Returns the ``files.completeUploadExternal`` response dict.
    Raises ``RuntimeError`` on Slack API errors.
    """
    # Step 1: Get upload URL
    step1_resp = pooled.client().get(
        "https://slack.com/api/files.getUploadURLExternal",
        params={"filename": filename, "length": len(file_data)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    step1 = step1_resp.json()
    if not step1.get("ok"):
        raise RuntimeError(
            f"Slack getUploadURLExternal error: {step1.get('error', 'unknown')}"
        )

    upload_url = step1["upload_url"]
    file_id = step1["file_id"]

    # Step 2: Upload file bytes to the presigned URL
    upload_resp = pooled.post(
        upload_url,
        content=file_data,
        headers={"Content-Type": "application/octet-stream"},
        timeout=timeout,
    )
    if upload_resp.status_code >= 400:
        raise RuntimeError(
            f"Slack file upload failed: HTTP {upload_resp.status_code}"
        )

    # Step 3: Complete the upload (share in channel)
    file_entry: dict = {"id": file_id}
    if title:
        file_entry["title"] = title

    payload: dict = {
        "files": [file_entry],
        "channel_id": channel,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if initial_comment:
        payload["initial_comment"] = initial_comment

    return _slack_api(
        "files.completeUploadExternal", token, payload, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Thread reading
# ---------------------------------------------------------------------------

def fetch_slack_thread(
    token: str,
    channel: str,
    thread_ts: str,
    *,
    limit: int = 100,
    timeout: float = 10,
) -> list[dict]:
    """Fetch messages in a Slack thread using ``conversations.replies``.

    Returns a list of message dicts ordered oldest-first.  Each message
    includes ``user``, ``text``, ``ts``, and optionally ``files``.
    Raises ``RuntimeError`` on Slack API errors.
    """
    messages: list[dict] = []
    cursor = ""

    while True:
        params: dict = {
            "channel": channel,
            "ts": thread_ts,
            "limit": min(limit - len(messages), 200),
        }
        if cursor:
            params["cursor"] = cursor

        resp = pooled.client().get(
            "https://slack.com/api/conversations.replies",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        result = resp.json()

        if not result.get("ok"):
            raise RuntimeError(
                f"Slack API error: {result.get('error', 'unknown')}"
            )

        for msg in result.get("messages", []):
            entry: dict = {
                "user": msg.get("user", ""),
                "text": msg.get("text", ""),
                "ts": msg.get("ts", ""),
            }
            if msg.get("files"):
                entry["files"] = [
                    {
                        "id": f.get("id", ""),
                        "name": f.get("name", ""),
                        "mimetype": f.get("mimetype", ""),
                        "url_private": f.get("url_private", ""),
                    }
                    for f in msg["files"]
                ]
            messages.append(entry)

        if len(messages) >= limit:
            break

        cursor = result.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    return messages[:limit]


# ---------------------------------------------------------------------------
# Placeholder + refresh loop
# ---------------------------------------------------------------------------

def post_placeholder(
    token: str,
    channel: str,
    *,
    thread_ts: str = "",
    placeholder_text: str = "Evaluating\u2026",
) -> str:
    """Post a placeholder message and set typing status.

    Returns the placeholder message ``ts`` (empty string on failure).
    """
    try:
        result = post_slack_message(
            token, channel, placeholder_text, thread_ts=thread_ts,
        )
    except (RuntimeError, httpx.HTTPError, OSError, TimeoutError) as exc:
        log.warning("Placeholder post failed: %s", exc)
        return ""

    placeholder_ts = result.get("ts", "")

    if thread_ts:
        set_thread_status(token, channel, thread_ts, "is thinking\u2026")

    return placeholder_ts


class StatusRefreshLoop(threading.Thread):
    """Background thread that re-sets typing status every *interval* seconds.

    Slack enforces a 2-minute timeout on assistant status — this loop
    keeps it alive for long-running agent work.  Call ``stop()`` to
    terminate; pass ``clear=True`` to also clear the status on exit.
    """

    def __init__(
        self,
        token: str,
        channel: str,
        thread_ts: str,
        *,
        interval: float = 90,
        status_text: str = "is thinking\u2026",
        max_seconds: float = 600,
    ):
        super().__init__(daemon=True, name="slack-status-refresh")
        self._token = token
        self._channel = channel
        self._thread_ts = thread_ts
        self._interval = interval
        self._status_text = status_text
        self._max_seconds = max_seconds
        self._stop_event = threading.Event()
        self._clear_on_stop = False

    def run(self) -> None:
        deadline = time.monotonic() + self._max_seconds
        while not self._stop_event.wait(self._interval):
            if time.monotonic() >= deadline:
                # Safety cap \u2014 never leave the indicator refreshing forever
                # if something failed to stop the loop. Clear and exit.
                set_thread_status(
                    self._token, self._channel, self._thread_ts, "",
                )
                return
            set_thread_status(
                self._token, self._channel, self._thread_ts, self._status_text,
            )
        if self._clear_on_stop:
            set_thread_status(
                self._token, self._channel, self._thread_ts, "",
            )

    def stop(self, *, clear: bool = False) -> None:
        """Signal the loop to stop.  If *clear*, sends an empty status on exit."""
        self._clear_on_stop = clear
        self._stop_event.set()
