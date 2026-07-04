"""Input channel handlers - framework-level pre-delivery hooks per event source.

When the drain loop is about to deliver a chat event, it calls the
registered channel handler for that event's source.  The handler can
perform side-effects (post a placeholder, set typing status) and augment
the event with extra fields (``placeholder_ts``) before it reaches the
agent.

Since #190 Phase 2 the handlers are policy shims over the channel gateway:
all channel traffic goes through the event server's ``/channels/*``
endpoints (see :mod:`bobi.events.gateway`), addressed by the event's
``conversation`` reference.  No channel credential is read here.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

PLACEHOLDER_TEXT = "Evaluating…"


class InputChannelHandler(Protocol):
    """Pre-delivery hook for a chat event source."""

    def prepare(self, event: dict, project_path: Path | None) -> dict:
        """Process *event* before inbox delivery.

        May post placeholders, set typing status, etc.  Returns the
        (possibly augmented) event dict - e.g. with ``placeholder_ts``
        injected into ``fields``.

        Must never raise - failures are logged and the original event
        is returned unchanged.
        """
        ...


# ---------------------------------------------------------------------------
# Typing refresh loop
# ---------------------------------------------------------------------------

class TypingRefreshLoop(threading.Thread):
    """Background thread that re-sets the typing indicator every *interval*
    seconds via the gateway (``POST /channels/typing``).

    Slack expires its assistant status after ~2 minutes - this keeps it
    alive for long-running agent work.  Call ``stop()`` to terminate; pass
    ``clear=True`` to also clear the indicator on exit.
    """

    def __init__(self, project_path: Path | None, conversation: str, *,
                 interval: float = 90, max_seconds: float = 600):
        super().__init__(daemon=True, name="channel-typing-refresh")
        self._project_path = project_path
        self._conversation = conversation
        self._interval = interval
        self._max_seconds = max_seconds
        self._stop_event = threading.Event()
        self._clear_on_stop = False

    def run(self) -> None:
        import time

        from bobi.events.gateway import channels_typing

        deadline = time.monotonic() + self._max_seconds
        while not self._stop_event.wait(self._interval):
            if time.monotonic() >= deadline:
                # Safety cap - never leave the indicator refreshing forever
                # if something failed to stop the loop. Clear and exit.
                channels_typing(self._project_path, self._conversation, False)
                return
            channels_typing(self._project_path, self._conversation, True)
        if self._clear_on_stop:
            channels_typing(self._project_path, self._conversation, False)

    def stop(self, *, clear: bool = False) -> None:
        """Signal the loop to stop.  If *clear*, clears the indicator on exit."""
        self._clear_on_stop = clear
        self._stop_event.set()


# Active refresh loops keyed by conversation reference.
_active_loops: dict[str, TypingRefreshLoop] = {}


# ---------------------------------------------------------------------------
# Slack input channel
# ---------------------------------------------------------------------------

def _without_placeholder_fields(event: dict) -> dict:
    """Return an event copy with placeholder metadata removed."""
    fields = dict(event.get("fields", {}))
    fields.pop("placeholder_ts", None)
    return dict(event, fields=fields)


class SlackInputChannel:
    """Open a response context for an inbound Slack chat event.

    When a Slack mention/DM reaches the drain loop, this handler asks the
    gateway to:

    1. Post an "Evaluating..." placeholder (``/channels/send``, mode post)
    2. Set the typing indicator (``/channels/typing``)
    3. Keep the indicator alive with a background refresh loop
    4. Inject the placeholder's ``ts`` into the event fields so the agent
       resolves it with ``bobi reply --edit``
    """

    def prepare(self, event: dict, project_path: Path | None) -> dict:
        if event.get("type") == "slack.thread_reply":
            return _without_placeholder_fields(event)

        conversation = event.get("conversation", "")
        if not conversation:
            return event

        from bobi.events.gateway import GatewayError, channels_send, channels_typing

        try:
            # Short timeout: this runs synchronously in the drain loop, so a
            # slow event server must not stall delivery of the whole batch.
            result = channels_send(
                project_path, conversation, PLACEHOLDER_TEXT, mode="post",
                timeout=10.0,
            )
        except GatewayError as exc:
            log.warning("Placeholder failed for %s: %s", conversation, exc)
            return event

        placeholder_ts = str(result.get("ts", "") or "")
        if not placeholder_ts:
            return event

        # Inject placeholder_ts into event fields for the agent.
        fields = dict(event.get("fields", {}))
        fields["placeholder_ts"] = placeholder_ts
        event = dict(event, fields=fields)

        # Typing indicator + refresh (the gateway no-ops on channels
        # without typing support; Slack's expires after ~2min). A loop that
        # self-terminated at its safety cap leaves a dead entry - replace it.
        channels_typing(project_path, conversation, True)
        existing = _active_loops.get(conversation)
        if existing is None or not existing.is_alive():
            loop = TypingRefreshLoop(project_path, conversation)
            loop.start()
            _active_loops[conversation] = loop

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


def stop_all_refresh_loops() -> None:
    """Stop and clear every active typing refresh loop.

    Called when a manager turn completes.  The gateway already clears the
    indicator when the agent's reply resolves the response context
    (``bobi reply`` sends mode ``final``); this in-process sweep is the
    backstop for turns that never replied, so the indicator does not
    linger until it expires.
    """
    for key, loop in list(_active_loops.items()):
        try:
            loop.stop(clear=True)
        except Exception:
            pass
        _active_loops.pop(key, None)
