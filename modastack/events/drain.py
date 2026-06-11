"""Event drain loop — batches queued events and delivers to a session inbox."""

from __future__ import annotations

import logging
import time
from queue import SimpleQueue
from typing import Callable

log = logging.getLogger(__name__)

DRAIN_INTERVAL = 2

# Active status refresh loops keyed by (channel, thread_ts).
_active_loops: dict[tuple[str, str], object] = {}


def _get_slack_token() -> str:
    """Retrieve the Slack bot token from project config.

    Returns empty string if unavailable (no project root, no config, etc.).
    """
    try:
        from modastack.sdk import get_project_root
        from modastack.config import Config

        root = get_project_root()
        if not root:
            return ""
        cfg = Config.load(root)
        return cfg.credential("slack", "bot_token")
    except Exception:
        return ""


def _post_slack_placeholders(events: list[dict], token: str) -> dict[int, str]:
    """Post placeholders for Slack chat events and return idx→placeholder_ts map."""
    from modastack.slack import post_placeholder, StatusRefreshLoop

    result: dict[int, str] = {}
    for i, event in enumerate(events):
        if event.get("source") != "slack":
            continue

        fields = event.get("fields", {})
        channel = fields.get("channel", "")
        thread_ts = fields.get("thread_ts", "") or fields.get("ts", "")

        if not channel:
            continue

        try:
            placeholder_ts = post_placeholder(
                token, channel, thread_ts=thread_ts,
            )
        except Exception as exc:
            log.warning("Placeholder failed for %s: %s", channel, exc)
            continue

        if placeholder_ts:
            result[i] = placeholder_ts

            # Start a refresh loop for threads (status auto-clears after 2min)
            if thread_ts:
                loop = StatusRefreshLoop(token, channel, thread_ts)
                loop.start()
                _active_loops[(channel, thread_ts)] = loop

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

        # Post placeholders for Slack chat events before delivering.
        placeholder_map: dict[int, str] = {}
        if chat_events:
            token = _get_slack_token()
            if token:
                placeholder_map = _post_slack_placeholders(chat_events, token)

        for group in [bulk_events, chat_events]:
            if not group:
                continue

            lines = []
            for i, event in enumerate(group):
                line = formatter(event)
                # Append placeholder_ts so the agent can edit it later
                if group is chat_events and i in placeholder_map:
                    line += f"\n  placeholder_ts: {placeholder_map[i]}"
                lines.append(line)

            text = "\n\n".join(lines)

            log.info(f"Delivering {len(group)} event(s) to {session_name}")
            ok, _ = deliver(session_name, text, sender="event-bus")
            if not ok:
                log.warning(f"Event delivery failed for {len(group)} event(s)")
