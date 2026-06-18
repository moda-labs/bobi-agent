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

# Poison pill: pushing this onto a drain's queue makes drain_loop return, so a
# session can stop its drain thread cleanly on shutdown (the loop otherwise
# blocks forever on queue.get()).
_DRAIN_STOP = object()


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


def _is_inbox_event(event: dict) -> bool:
    """Whether an event is a directed inbox/<session> message.

    Inbox events carry source ``inbox`` and type ``inbox/<session>`` (see
    ``events.publish.publish_inbox``). They bypass auto-dispatch and formatting.
    """
    return (event.get("source") == "inbox"
            or str(event.get("type", "")).startswith("inbox/"))


def _thread_key(event: dict) -> tuple[str, str, str]:
    """Return (source, channel, thread_ts) for placeholder dedup."""
    fields = event.get("fields", {})
    channel = fields.get("channel", "")
    thread_ts = fields.get("thread_ts", "") or fields.get("ts", "")
    return (event.get("source", ""), channel, thread_ts)


def _prepare_chat_events(events: list[dict]) -> list[dict]:
    """Run input channel handlers on chat events, returning augmented copies.

    Each handler may post placeholders, set typing status, or inject
    fields (e.g. ``placeholder_ts``) into the event before delivery.

    When multiple events in a batch target the same thread, only the
    first triggers a placeholder — subsequent events reuse the same
    ``placeholder_ts`` to avoid duplicate "Evaluating…" messages (#232).

    Credential resolution is generic: the handler declares its
    ``credential_key`` and the drain loop resolves it via
    ``cfg.credential(source, key)`` from the project's service config.
    """
    from modastack.events.channels import get_channel_handler

    cfg = _get_project_config()

    # Track placeholder_ts per thread so we post at most one per batch.
    seen_threads: dict[tuple[str, str, str], str] = {}

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

        key = _thread_key(event)
        existing_ts = seen_threads.get(key)

        if existing_ts is not None:
            # Reuse the placeholder from the first event in this thread.
            fields = dict(event.get("fields", {}))
            fields["placeholder_ts"] = existing_ts
            result.append(dict(event, fields=fields))
        else:
            prepared = handler.prepare(event, token)
            placeholder_ts = prepared.get("fields", {}).get("placeholder_ts", "")
            if placeholder_ts:
                seen_threads[key] = placeholder_ts
            result.append(prepared)

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
    from modastack.inbox import get_local_inbox, Message, _msg_id

    log.info("Drain loop active — delivering events to session inbox")

    while True:
        event = queue.get()
        if event is _DRAIN_STOP:
            return

        time.sleep(DRAIN_INTERVAL)
        batch = [event]
        stop_after = False
        while not queue.empty():
            nxt = queue.get_nowait()
            if nxt is _DRAIN_STOP:
                stop_after = True
                break
            batch.append(nxt)

        # The drain runs in the same process as its session, so it pushes
        # straight into the session's in-process inbox queue — never back
        # through the transport (which would re-deliver to this same drain).
        inbox = get_local_inbox(session_name)
        if inbox is None:
            log.warning("No local inbox for %s — dropping %d event(s)",
                        session_name, len(batch))
            if stop_after:
                return
            continue

        # inbox/* events are already addressed agent→agent messages: deliver
        # them raw and skip auto-dispatch (they're not external triggers to
        # route) and skip formatting (the text is the message itself).
        external: list[dict] = []
        for e in batch:
            if _is_inbox_event(e):
                payload = e.get("payload") or {}
                text = payload.get("text", "")
                if not text:
                    continue
                inbox.push(Message(
                    id=payload.get("id") or _msg_id(),
                    sender=payload.get("sender", ""),
                    text=text,
                    wait=bool(payload.get("wait", False)),
                    reply_to=payload.get("reply_to", ""),
                ))
            else:
                external.append(e)

        if not external:
            if stop_after:
                return
            continue

        # Single pass: auto-dispatch + group by delivery class.
        # Bulk events are delivered first so the agent sees context
        # before interactive messages that may need an immediate reply.
        bulk_events: list[tuple[bool, dict]] = []
        chat_events: list[tuple[bool, dict]] = []
        for e in external:
            # A reactor failure on one event must not kill the drain
            # thread — that would silently stop ALL event delivery while
            # the queue grows unbounded.
            reactor_result = None
            if reactor:
                try:
                    reactor_result = reactor.process(e)
                except Exception:
                    log.exception("Reactor failed processing event %s — "
                                  "delivering it un-dispatched", e.get("type"))
            target = chat_events if e.get("delivery") == "chat" else bulk_events
            target.append((reactor_result, e))

        # Run input channel handlers on chat events (placeholder, typing, etc.).
        if chat_events:
            raw = [ev for _, ev in chat_events]
            prepared = _prepare_chat_events(raw)
            chat_events = [
                (dispatched, prepared_ev)
                for (dispatched, _), prepared_ev in zip(chat_events, prepared)
            ]

        for group in [bulk_events, chat_events]:
            if not group:
                continue

            lines = []
            for reactor_result, e in group:
                formatted = formatter(e)
                if reactor_result == "dispatched":
                    formatted += "\n  [AUTO-DISPATCHED: workflow launched — no action needed]"
                elif reactor_result == "suppressed":
                    formatted += "\n  [SUPPRESSED: informational event — no action needed]"
                lines.append(formatted)
            text = "\n\n".join(lines)

            log.info(f"Delivering {len(group)} event(s) to {session_name}")
            inbox.push(Message(id=_msg_id(), sender="event-bus", text=text))

        if stop_after:
            return
