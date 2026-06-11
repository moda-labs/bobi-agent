"""Slack message posting — shared by the CLI and the workflow orchestrator.

Handles message formatting (markdown → Slack mrkdwn), truncation, and
the HTTP POST to chat.postMessage.  All errors are raised, never swallowed
— callers decide how to handle failures.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def format_slack_message(text: str) -> str:
    """Convert markdown to Slack mrkdwn and truncate if needed."""
    # Escaped newlines from shell invocations
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    # Headings → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # Bold markdown → Slack bold
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Links
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
    if len(text) > 3000:
        text = text[:3000] + '\n_(truncated)_'
    return text


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

    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read())

    if not result.get("ok"):
        raise RuntimeError(f"Slack API error: {result.get('error', 'unknown')}")

    return result
