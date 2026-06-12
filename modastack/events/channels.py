"""Input channel handlers — framework-level pre-delivery hooks per event source.

When the drain loop is about to deliver a chat event, it calls the
registered channel handler for that event's source.  The handler can
perform side-effects (post a placeholder, set typing status) and augment
the event with extra fields (``placeholder_ts``) before it reaches the
agent.

This is the extension point for per-source UX — every agent gets it for
free without per-pack wiring.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class InputChannelHandler(Protocol):
    """Pre-delivery hook for a chat event source."""

    @property
    def credential_key(self) -> str:
        """The credential key to look up in the service's config.

        The drain loop calls ``cfg.credential(source, handler.credential_key)``
        to resolve the token for this handler.  For example, Slack's handler
        returns ``"bot_token"`` so the drain loop looks up
        ``cfg.credential("slack", "bot_token")``.
        """
        ...

    def prepare(self, event: dict, token: str) -> dict:
        """Process *event* before inbox delivery.

        May post messages, set status, etc.  Returns the (possibly
        augmented) event dict — e.g. with ``placeholder_ts`` injected
        into ``fields``.

        *token* is the service credential (e.g. Slack bot token).
        Must never raise — failures are logged and the original event
        is returned unchanged.
        """
        ...


# ---------------------------------------------------------------------------
# Slack input channel
# ---------------------------------------------------------------------------

# Active status refresh loops keyed by (channel, thread_ts).
_active_loops: dict[tuple[str, str], object] = {}


class SlackInputChannel:
    """Post an "Evaluating..." placeholder and set typing status on arrival.

    When a Slack chat event reaches the drain loop, this handler:

    1. Posts a placeholder message (``chat.postMessage``)
    2. Sets the typing indicator (``assistant.threads.setStatus``)
    3. Starts a background refresh loop to keep the indicator alive
    4. Injects ``placeholder_ts`` into the event fields so the agent
       can edit the placeholder with its real response via ``--edit``
    """

    @property
    def credential_key(self) -> str:
        return "bot_token"

    def prepare(self, event: dict, token: str) -> dict:
        from modastack.slack import post_placeholder, StatusRefreshLoop

        fields = event.get("fields", {})
        channel = fields.get("channel", "")
        thread_ts = fields.get("thread_ts", "") or fields.get("ts", "")

        if not channel:
            return event

        try:
            placeholder_ts = post_placeholder(
                token, channel, thread_ts=thread_ts,
            )
        except Exception as exc:
            log.warning("Placeholder failed for %s: %s", channel, exc)
            return event

        if not placeholder_ts:
            return event

        # Inject placeholder_ts into event fields for the agent.
        fields = dict(fields)
        fields["placeholder_ts"] = placeholder_ts
        event = dict(event, fields=fields)

        # Start a refresh loop for threads (status auto-clears after 2min).
        if thread_ts:
            loop = StatusRefreshLoop(token, channel, thread_ts)
            loop.start()
            _active_loops[(channel, thread_ts)] = loop

        return event


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_handlers: dict[str, InputChannelHandler] = {
    "slack": SlackInputChannel(),
}


def get_channel_handler(source: str) -> InputChannelHandler | None:
    """Return the input channel handler for *source*, or ``None``."""
    return _handlers.get(source)


def stop_refresh_loop(channel: str, thread_ts: str) -> None:
    """Stop and remove the status refresh loop for a thread.

    Called when the agent replies (``slack-reply --edit``) so the
    background thread is cleaned up and the ``_active_loops`` dict
    doesn't grow unbounded.  No-ops silently if no loop exists.
    """
    key = (channel, thread_ts)
    loop = _active_loops.pop(key, None)
    if loop is not None:
        loop.stop(clear=True)  # type: ignore[union-attr]


def stop_all_refresh_loops() -> None:
    """Stop and clear every active status refresh loop.

    Called when a manager turn completes. The per-reply ``stop_refresh_loop``
    runs in the ``slack-reply`` CLI *subprocess*, where ``_active_loops`` is a
    different (empty) dict — so it can never reach the manager's loops. This
    in-process sweep is what actually clears the "is thinking…" indicator once
    the agent is done, so it does not linger forever.
    """
    for key, loop in list(_active_loops.items()):
        try:
            loop.stop(clear=True)  # type: ignore[union-attr]
        except Exception:
            pass
        _active_loops.pop(key, None)
