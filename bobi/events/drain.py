"""Event drain loop — batches queued events and delivers to a session inbox."""

from __future__ import annotations

import logging
import time
from queue import SimpleQueue
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from bobi.events.reactor import EventReactor

log = logging.getLogger(__name__)

DRAIN_INTERVAL = 2

# Poison pill: pushing this onto a drain's queue makes drain_loop return, so a
# session can stop its drain thread cleanly on shutdown (the loop otherwise
# blocks forever on queue.get()).
_DRAIN_STOP = object()
_MONITOR_ERROR_DELIVERED: dict[tuple[str, str, str, str], int] = {}
_MONITOR_ERROR_REPEAT_PUSH_EVERY = 3


def _get_project_root():
    """Resolve the project root, or None if unavailable."""
    try:
        from bobi.sdk import get_project_root

        return get_project_root() or None
    except Exception:
        return None


def _is_inbox_event(event: dict) -> bool:
    """Whether an event is a directed inbox/<session> message.

    Inbox events carry source ``inbox`` and type ``inbox/<session>`` (see
    ``events.publish.publish_inbox``). They bypass auto-dispatch and formatting.
    """
    return (event.get("source") == "inbox"
            or str(event.get("type", "")).startswith("inbox/"))


def _is_policy_update(event: dict) -> bool:
    """Whether an event is a ``policy.updated`` completion signal (#456).

    The policy-curator publishes ``system/policy.updated`` whenever it rewrites
    ``policy.md``. ``post_event`` routes it onto both the bare ``policy.updated``
    and the source-qualified ``system/policy.updated`` topic, so match either.
    """
    etype = str(event.get("type", ""))
    return etype == "policy.updated" or etype.endswith("/policy.updated")


def _is_monitor_error(event: dict) -> bool:
    """Whether an event is a monitor failure signal that should push actively."""
    etype = str(event.get("type", ""))
    return etype == "monitor.error" or etype.endswith("/monitor.error")


def _is_passive_slack_thread_reply(event: dict) -> bool:
    """Whether a Slack event should be delivered without placeholder UX."""
    return (
        event.get("source") == "slack"
        and event.get("type") == "slack.thread_reply"
    )


def _without_placeholder_fields(event: dict) -> dict:
    """Return an event copy with Slack placeholder metadata removed."""
    fields = dict(event.get("fields", {}))
    fields.pop("placeholder_ts", None)
    return dict(event, fields=fields)


def _prepare_chat_events(events: list[dict]) -> list[dict]:
    """Run input channel handlers on chat events, returning augmented copies.

    Each handler may set typing status or make other source-specific
    adjustments before delivery. Handlers talk to the channel gateway (#190),
    so no credential is resolved here - the event server holds the channel
    tokens.
    """
    from bobi.events.channels import get_channel_handler

    project_root = _get_project_root()

    result: list[dict] = []
    for event in events:
        if _is_passive_slack_thread_reply(event):
            result.append(_without_placeholder_fields(event))
            continue

        source = event.get("source", "")
        handler = get_channel_handler(source)
        if handler is None:
            result.append(event)
            continue

        result.append(handler.prepare(event, project_root))

    return result


def drain_loop(session_name: str, queue: SimpleQueue | None = None,
               formatter: Callable | None = None,
               reactor: "EventReactor | None" = None,
               cursor_ack: "Callable[[int], None] | None" = None) -> None:
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
        cursor_ack: Optional callback invoked with the highest seq number
            in each batch AFTER delivery to the inbox. Used to advance the
            cursor and ACK to the event server only once the event is
            durably delivered (#278).
    """
    if queue is None:
        from bobi.events.client import event_queue
        queue = event_queue
    if formatter is None:
        from bobi.events.client import format_event_for_manager
        formatter = format_event_for_manager
    from bobi.inbox import get_local_inbox, Message, _msg_id

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
            elif _is_policy_update(e):
                # Passive-by-default delivery (#456). The curator publishes
                # policy.updated for observability on every rewrite, but working
                # agents already re-read policy.md on their next rebuilt prompt —
                # so a routine distillation must NOT push an inbox message that
                # interrupts every agent mid-task. Only an urgent update (a
                # reversed decision that invalidates in-flight work) is pushed
                # with a re-read instruction. The event still published; this
                # only gates the inbox push.
                payload = e.get("payload") or e.get("fields") or {}
                if not bool(payload.get("urgent", False)):
                    log.debug("Suppressing non-urgent policy.updated inbox push "
                              "for %s (passive re-read on next prompt)", session_name)
                    continue
                summary = str(payload.get("summary", "")).strip()
                inbox.push(Message(
                    id=_msg_id(),
                    sender="policy-curator",
                    text=(f"policy.md updated — {summary}\n"
                          "Re-read run/state/policy.md and reconcile any "
                          "in-flight plan against it."),
                ))
            elif _is_monitor_error(e):
                payload = e.get("payload") or e.get("fields") or {}
                monitor = str(payload.get("monitor", "") or "unknown-monitor")
                flavor = str(payload.get("flavor", "") or "unknown")
                reason = str(payload.get("reason", "") or "unknown")
                detail = str(payload.get("detail", "") or "").strip()
                key = (session_name, monitor, flavor, reason)
                count = _MONITOR_ERROR_DELIVERED.get(key, 0) + 1
                _MONITOR_ERROR_DELIVERED[key] = count
                should_push = (
                    count == 1
                    or count % _MONITOR_ERROR_REPEAT_PUSH_EVERY == 0
                )
                if not should_push:
                    log.debug("Suppressing duplicate monitor.error inbox push "
                              "for %s/%s/%s", monitor, flavor, reason)
                    continue
                text = f"Monitor {monitor} failed ({flavor}: {reason})."
                if count > 1:
                    text += f" Repeated {count} times."
                if detail:
                    text += f"\n{detail}"
                inbox.push(Message(
                    id=_msg_id(),
                    sender="monitor-error",
                    text=text,
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
            if reactor_result == "deduped":
                log.info("Dropping duplicate event delivery %s", e.get("type"))
                continue
            target = chat_events if e.get("delivery") == "chat" else bulk_events
            target.append((reactor_result, e))

        # Run input channel handlers on chat events (typing, cleanup, etc.).
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

        # Advance cursor and ACK only AFTER all events in this batch have
        # been delivered to the inbox — a crash before here means the
        # server replays the events on reconnect (#278 bug 2).
        if cursor_ack:
            max_seq = max((e.get("seq", 0) for e in batch), default=0)
            if max_seq > 0:
                cursor_ack(max_seq)

        if stop_after:
            return
