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
