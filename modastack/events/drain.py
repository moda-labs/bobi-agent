"""Event drain loop — batches queued events and delivers to a session inbox."""

from __future__ import annotations

import logging
import time
from queue import SimpleQueue
from typing import Callable

log = logging.getLogger(__name__)

DRAIN_INTERVAL = 2

# Maps event source names to (service_name, credential_key) pairs for
# looking up service tokens from project config.
_SOURCE_TO_CREDENTIAL: dict[str, tuple[str, str]] = {
    "slack": ("slack", "bot_token"),
}


def _get_service_token(source: str) -> str:
    """Retrieve the service credential for *source* from project config.

    Returns empty string if unavailable (no project root, no config, etc.).
    """
    entry = _SOURCE_TO_CREDENTIAL.get(source)
    if not entry:
        return ""

    try:
        from modastack.sdk import get_project_root
        from modastack.config import Config

        root = get_project_root()
        if not root:
            return ""
        cfg = Config.load(root)
        return cfg.credential(*entry)
    except Exception:
        return ""


def _prepare_chat_events(events: list[dict]) -> list[dict]:
    """Run input channel handlers on chat events, returning augmented copies.

    Each handler may post placeholders, set typing status, or inject
    fields (e.g. ``placeholder_ts``) into the event before delivery.
    """
    from modastack.events.channels import get_channel_handler

    result: list[dict] = []
    for event in events:
        source = event.get("source", "")
        handler = get_channel_handler(source)
        if handler is None:
            result.append(event)
            continue

        token = _get_service_token(source)
        if not token:
            result.append(event)
            continue

        result.append(handler.prepare(event, token))

    return result


def drain_loop(session_name: str, queue: SimpleQueue | None = None,
               formatter: Callable | None = None) -> None:
    """Drain events from a queue and deliver to a session's inbox.

    Args:
        session_name: Target session to deliver events to.
        queue: Event queue to drain. Defaults to the global event_queue.
        formatter: Callable to format events for the session. Defaults to
            format_event_for_manager from the client module.
    """
    if queue is None:
        from modastack.events.client import event_queue
        queue = event_queue
    if formatter is None:
        from modastack.events.client import format_event_for_manager
        formatter = format_event_for_manager
    from modastack.inbox import deliver

    log.info("Drain loop active — delivering events to session inbox")

    while True:
        event = queue.get()

        time.sleep(DRAIN_INTERVAL)
        batch = [event]
        while not queue.empty():
            batch.append(queue.get_nowait())

        # Group by delivery class (v2). Chat events (e.g. Slack) are
        # delivered last so the agent sees bulk context first, then
        # interactive messages that may need an immediate reply.
        bulk_events = [e for e in batch if e.get("delivery") != "chat"]
        chat_events = [e for e in batch if e.get("delivery") == "chat"]

        # Run input channel handlers on chat events (placeholder, typing, etc.).
        if chat_events:
            chat_events = _prepare_chat_events(chat_events)

        for group in [bulk_events, chat_events]:
            if not group:
                continue

            lines = [formatter(e) for e in group]
            text = "\n\n".join(lines)

            log.info(f"Delivering {len(group)} event(s) to {session_name}")
            ok, _ = deliver(session_name, text, sender="event-bus")
            if not ok:
                log.warning(f"Event delivery failed for {len(group)} event(s)")
