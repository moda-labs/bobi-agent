"""Event drain loop — batches queued events and delivers to a session inbox."""

from __future__ import annotations

import logging
import threading
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
    """Whether an event is a ``memory.updated`` completion signal (#456).

    The sleep-cycle publishes ``system/memory.updated`` whenever it rewrites
    ``long_term_memory.md``. ``post_event`` routes it onto both the bare ``memory.updated``
    and the source-qualified ``system/memory.updated`` topic, so match either.
    """
    etype = str(event.get("type", ""))
    return (
        etype == "memory.updated"
        or etype.endswith("/memory.updated")
        or etype == "policy.updated"
        or etype.endswith("/policy.updated")
    )


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


# A pending map this large means the cursor floor has been pinned for a long
# time (a message dropped un-acked, or a no-inbox drop) - warn so a wedged
# watermark is visible in logs instead of surfacing as a giant replay later.
_WATERMARK_PENDING_WARN = 512


class _AckWatermark:
    """ACK-after-processing cursor tracker (#688).

    The server treats an ACK of seq N as "everything through N is processed",
    and chat priority makes messages complete out of push order - so acking
    the seq of whatever finished last could silently discard older events
    still queued in the inbox. The watermark acks the highest seq whose batch
    and every batch below it have fully completed.

    Each batch is a refcount: open_batch() takes one reference (released by
    close()), attach() takes one per pushed message (released by its done
    callback). A batch is complete at zero. A batch that never completes (a
    message dropped while the session is stopped/error) deliberately pins the
    floor so a restart replays it.
    """

    def __init__(self, ack: Callable[[int], None]) -> None:
        self._ack = ack
        self._lock = threading.Lock()
        self._pending: dict[int, int] = {}  # seq -> outstanding references
        self._acked = 0    # newest ackable seq decided so far
        self._sent = 0     # last seq actually handed to self._ack
        self._sending = False

    def open_batch(self, seq: int) -> "_BatchAck":
        self._add(seq)
        return _BatchAck(self, seq)

    def _add(self, seq: int) -> None:
        with self._lock:
            self._pending[seq] = self._pending.get(seq, 0) + 1
            if len(self._pending) == _WATERMARK_PENDING_WARN:
                log.warning(
                    "Ack watermark has %d outstanding batches - cursor "
                    "pinned near seq %d (a message was likely dropped "
                    "un-acked; a restart will replay from there)",
                    len(self._pending), min(self._pending))

    def _done(self, seq: int) -> None:
        with self._lock:
            if seq in self._pending:
                self._pending[seq] -= 1
            # Ascending scan (NOT insertion order: a reconnect replay can
            # register a lower seq after a higher one): pop fully-completed
            # batches until the first still-outstanding seq holds the floor.
            for s in sorted(self._pending):
                if self._pending[s] <= 0:
                    del self._pending[s]
                    self._acked = max(self._acked, s)
                else:
                    break
        self._flush()

    def _flush(self) -> None:
        """Send the newest ackable seq via self._ack, outside the state lock.

        The callback does real I/O (cursor-file write + WS send), and a
        blocked socket must never stall whoever holds the state lock - the
        drain thread and the session loop both take it on hot paths. One
        sender at a time; a newer target decided mid-send is picked up by
        the loop, so sends stay strictly increasing.
        """
        while True:
            with self._lock:
                if self._sending or self._sent == self._acked:
                    return
                self._sending = True
                target = self._acked
            try:
                self._ack(target)
            finally:
                with self._lock:
                    self._sent = target
                    self._sending = False


class _BatchAck:
    """Completion handle for one delivered batch's seq."""

    def __init__(self, tracker: _AckWatermark, seq: int) -> None:
        self._tracker = tracker
        self._seq = seq

    def attach(self) -> Callable[[], None]:
        """Register one pushed message; returns its once-only done callback."""
        self._tracker._add(self._seq)
        called = [False]

        def _done() -> None:
            if called[0]:
                return
            called[0] = True
            self._tracker._done(self._seq)

        return _done

    def close(self) -> None:
        """Release the batch's own reference; an empty batch completes now."""
        self._tracker._done(self._seq)


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
        cursor_ack: Optional callback invoked with the highest safe seq
            number once every message pushed for that seq (and every seq
            below it) has been PROCESSED by the session - not merely pushed
            into its in-memory inbox. A restart therefore replays anything
            still queued instead of destroying it (#278, #688).
    """
    if queue is None:
        from bobi.events.client import event_queue
        queue = event_queue
    if formatter is None:
        from bobi.events.client import format_event_for_manager
        formatter = format_event_for_manager
    from bobi.inbox import get_local_inbox, Message, _msg_id

    tracker = _AckWatermark(cursor_ack) if cursor_ack else None

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

        max_seq = max((e.get("seq", 0) for e in batch), default=0)
        batch_ack = (tracker.open_batch(max_seq)
                     if tracker and max_seq > 0 else None)

        # The drain runs in the same process as its session, so it pushes
        # straight into the session's in-process inbox queue — never back
        # through the transport (which would re-deliver to this same drain).
        inbox = get_local_inbox(session_name)
        if inbox is None:
            # batch_ack is deliberately never closed: the seq stays
            # outstanding, holding the ack floor so the server replays these
            # events after a restart instead of losing them.
            log.warning("No local inbox for %s - dropping %d event(s) "
                        "(seq<=%d, not ACKed; replayed after restart)",
                        session_name, len(batch), max_seq)
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
                    on_done=batch_ack.attach() if batch_ack else None,
                ))
            elif _is_policy_update(e):
                # Passive-by-default delivery (#456). The sleep_cycle publishes
                # memory.updated for observability on every rewrite, but working
                # agents already re-read long_term_memory.md on their next rebuilt prompt —
                # so a routine distillation must NOT push an inbox message that
                # interrupts every agent mid-task. Only an urgent update (a
                # reversed decision that invalidates in-flight work) is pushed
                # with a re-read instruction. The event still published; this
                # only gates the inbox push.
                payload = e.get("payload") or e.get("fields") or {}
                if not bool(payload.get("urgent", False)):
                    log.debug("Suppressing non-urgent memory.updated inbox push "
                              "for %s (passive re-read on next prompt)", session_name)
                    continue
                summary = str(payload.get("summary", "")).strip()
                inbox.push(Message(
                    id=_msg_id(),
                    sender="sleep-cycle",
                    text=(f"long_term_memory.md updated — {summary}\n"
                          "Re-read run/state/long_term_memory.md and reconcile any "
                          "in-flight plan against it."),
                    on_done=batch_ack.attach() if batch_ack else None,
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

        # Single pass: auto-dispatch + group by delivery class. Chat events
        # are pushed as priority messages (#688): a human is waiting on
        # them, so they must not sit behind queued bulk webhook batches.
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

        for is_chat, group in [(False, bulk_events), (True, chat_events)]:
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

            log.info("Delivering %d event(s) to %s (batch seq<=%d)",
                     len(group), session_name, max_seq)
            inbox.push(
                Message(id=_msg_id(), sender="event-bus", text=text,
                        on_done=batch_ack.attach() if batch_ack else None),
                priority=is_chat,
            )

        # The cursor is NOT acked here: each pushed message carries a
        # completion callback, and the watermark acks the batch seq only
        # once the session has processed everything at or below it (#688).
        # A crash before then means the server replays on reconnect.
        if batch_ack:
            batch_ack.close()

        if stop_after:
            return
