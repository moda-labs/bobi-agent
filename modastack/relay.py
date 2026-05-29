"""Chat relay — mirror manager session I/O to external chat services.

Pluggable adapter interface so the transport (Slack, Discord, etc.)
can be swapped without changing the relay logic.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol, runtime_checkable

import httpx

log = logging.getLogger(__name__)


@runtime_checkable
class ChatAdapter(Protocol):
    def send(self, text: str, role: str = "assistant") -> None:
        """Send a message to the chat service.

        role: "assistant" for manager output, "user" for injected input/events.
        """
        ...


class NullAdapter:
    """No-op adapter when no chat service is configured."""

    def send(self, text: str, role: str = "assistant") -> None:
        pass


class SlackAdapter:
    """Posts messages to a Slack channel via the Web API."""

    def __init__(self, bot_token: str, channel_id: str):
        self._token = bot_token
        self._channel = channel_id
        self._client = httpx.Client(timeout=10)

    def send(self, text: str, role: str = "assistant") -> None:
        if not text or not text.strip():
            return

        if len(text) > 3000:
            text = text[:3000] + "\n_(truncated)_"

        payload = {
            "channel": self._channel,
            "text": text,
        }

        try:
            resp = self._client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self._token}"},
                json=payload,
            )
            data = resp.json()
            if not data.get("ok"):
                log.warning(f"Slack relay failed: {data.get('error', 'unknown')}")
        except Exception as e:
            log.warning(f"Slack relay error: {e}")


def build_adapter() -> ChatAdapter:
    """Build a chat adapter from global config. Returns NullAdapter if unconfigured."""
    try:
        from modastack.config import GlobalConfig
        config = GlobalConfig.load()
        token = config.slack_bot_token
        channel = getattr(config, "slack_dm_channel", "") or "D0B51JP1N4C"
        if token:
            log.info(f"Relay: Slack adapter → {channel}")
            return SlackAdapter(token, channel)
    except Exception as e:
        log.debug(f"Relay: failed to build adapter: {e}")
    return NullAdapter()
