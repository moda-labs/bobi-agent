"""Event drain loop — batches queued events and delivers to a session inbox."""

from __future__ import annotations

import logging
import time
from queue import SimpleQueue
from typing import Callable

log = logging.getLogger(__name__)

DRAIN_INTERVAL = 2


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

        for group in [bulk_events, chat_events]:
            if not group:
                continue

            lines = [formatter(e) for e in group]
            text = "\n\n".join(lines)

            log.info(f"Delivering {len(group)} event(s) to {session_name}")
            ok, _ = deliver(session_name, text, sender="event-bus")
            if not ok:
                log.warning(f"Event delivery failed for {len(group)} event(s)")
