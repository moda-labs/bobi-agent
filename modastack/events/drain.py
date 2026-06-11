"""Event drain loop — batches queued events and delivers to a session inbox."""

from __future__ import annotations

import logging
import time
from queue import SimpleQueue
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from modastack.events.reactor import EventReactor

log = logging.getLogger(__name__)

DRAIN_INTERVAL = 2


def _get_project_config():
    """Load the project Config, or return None if unavailable."""
    try:
        from modastack.sdk import get_project_root
        from modastack.config import Config

        root = get_project_root()
        if not root:
            return None
        return Config.load(root)
    except Exception:
        return None


def _prepare_chat_events(events: list[dict]) -> list[dict]:
    """Run input channel handlers on chat events, returning augmented copies.

    Each handler may post placeholders, set typing status, or inject
    fields (e.g. ``placeholder_ts``) into the event before delivery.

    Credential resolution is generic: the handler declares its
    ``credential_key`` and the drain loop resolves it via
    ``cfg.credential(source, key)`` from the project's service config.
    """
    from modastack.events.channels import get_channel_handler

    cfg = _get_project_config()

    result: list[dict] = []
    for event in events:
        source = event.get("source", "")
        handler = get_channel_handler(source)
        if handler is None:
            result.append(event)
            continue

        token = cfg.credential(source, handler.credential_key) if cfg else ""
        if not token:
            result.append(event)
            continue

        result.append(handler.prepare(event, token))

    return result


def drain_loop(session_name: str, queue: SimpleQueue | None = None,
               formatter: Callable | None = None,
               reactor: "EventReactor | None" = None) -> None:
    """Drain events from a queue and deliver to a session's inbox.

    Args:
        session_name: Target session to deliver events to.
        queue: Event queue to drain. Defaults to the global event_queue.
        formatter: Callable to format events for the session. Defaults to
            format_event_for_manager from the client module.
        reactor: Optional EventReactor for deterministic auto-dispatch.
            When set, each event is checked against auto-dispatch rules
            before delivery. Matched events are still delivered but
            annotated so the LLM knows a workflow was already launched.
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

        # Auto-dispatch: check each event before formatting. Matching
        # events still get delivered but are annotated so the manager
        # knows the workflow was already launched.
        dispatched: set[int] = set()
        if reactor:
            for i, e in enumerate(batch):
                if reactor.process(e):
                    dispatched.add(i)

        # Group by delivery class (v2). Chat events (e.g. Slack) are
        # delivered last so the agent sees bulk context first, then
        # interactive messages that may need an immediate reply.
        bulk_events = [(i, e) for i, e in enumerate(batch) if e.get("delivery") != "chat"]
        chat_events = [(i, e) for i, e in enumerate(batch) if e.get("delivery") == "chat"]

        # Run input channel handlers on chat events (placeholder, typing, etc.).
        if chat_events:
            chat_events = _prepare_chat_events(chat_events)

        for group in [bulk_events, chat_events]:
            if not group:
                continue

            lines = []
            for i, e in group:
                formatted = formatter(e)
                if i in dispatched:
                    formatted += "\n  [AUTO-DISPATCHED: workflow launched — no action needed]"
                lines.append(formatted)
            text = "\n\n".join(lines)

            log.info(f"Delivering {len(group)} event(s) to {session_name}")
            ok, _ = deliver(session_name, text, sender="event-bus")
            if not ok:
                log.warning(f"Event delivery failed for {len(group)} event(s)")
