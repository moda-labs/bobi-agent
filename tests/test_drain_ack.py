"""ACK-after-processing drain tests (#688).

The drain must not ACK the event-server cursor when it pushes a batch into
the session inbox - only when the session finishes processing the pushed
message(s). Otherwise a restart destroys every queued-but-unprocessed
message: the server believes them delivered and never replays them.

Chat priority (#688) means messages complete out of push order, so the ack
must be a watermark: never ack past an older, still-unprocessed batch.
"""

import queue
from unittest.mock import patch

from bobi.events.drain import drain_loop
from bobi.inbox import register_local_inbox, unregister_local_inbox


class _ScriptedQueue:
    """Yields pre-scripted batches to drain_loop, then stops the loop.

    drain_loop forms a batch from one blocking get() plus get_nowait() until
    empty() - so each inner list here becomes exactly one delivered batch.
    """

    def __init__(self, batches):
        self._batches = [list(b) for b in batches]

    def _advance(self):
        while self._batches and not self._batches[0]:
            self._batches.pop(0)

    def get(self):
        self._advance()
        if not self._batches:
            raise KeyboardInterrupt
        return self._batches[0].pop(0)

    def empty(self):
        return not (self._batches and self._batches[0])

    def get_nowait(self):
        if self.empty():
            raise queue.Empty
        return self._batches[0].pop(0)


class _CaptureInbox:
    """Records pushed messages and their priority flag."""

    def __init__(self):
        self.messages = []
        self.priorities = []

    def push(self, msg, priority=False):
        self.messages.append(msg)
        self.priorities.append(priority)


def _run_drain(batches):
    """Run drain_loop over scripted batches; return (inbox, acks)."""
    inbox = _CaptureInbox()
    acks = []
    register_local_inbox("ack-test", inbox)
    try:
        with patch("bobi.events.drain.time.sleep"):
            try:
                drain_loop("ack-test", queue=_ScriptedQueue(batches),
                           formatter=lambda e: e.get("text", ""),
                           cursor_ack=acks.append)
            except KeyboardInterrupt:
                pass
    finally:
        unregister_local_inbox("ack-test")
    return inbox, acks


def _bulk(seq, text="bulk event"):
    return {"type": "ci.check_run", "text": text, "delivery": "bulk",
            "seq": seq}


def _chat(seq, text="chat message"):
    # An unknown source has no channel handler - passes through prepare.
    return {"type": "chat.message", "text": text, "delivery": "chat",
            "source": "testchan", "seq": seq}


class TestAckAfterProcessing:
    def test_no_ack_at_push_time(self):
        inbox, acks = _run_drain([[_bulk(5)]])
        assert len(inbox.messages) == 1
        assert acks == [], "cursor ACKed at push time - restart loses the message"
        inbox.messages[0].on_done()
        assert acks == [5]

    def test_ack_is_idempotent(self):
        inbox, acks = _run_drain([[_bulk(5)]])
        inbox.messages[0].on_done()
        inbox.messages[0].on_done()
        assert acks == [5]

    def test_batch_with_nothing_pushed_acks_immediately(self):
        # An inbox event with empty text pushes nothing - the batch is done
        # the moment the drain finishes with it.
        events = [{"source": "inbox", "type": "inbox/ack-test",
                   "payload": {"text": ""}, "seq": 7}]
        inbox, acks = _run_drain([events])
        assert inbox.messages == []
        assert acks == [7]

    def test_inbox_message_carries_ack(self):
        events = [{"source": "inbox", "type": "inbox/ack-test",
                   "payload": {"id": "m1", "sender": "peer", "text": "hi"},
                   "seq": 9}]
        inbox, acks = _run_drain([events])
        assert len(inbox.messages) == 1
        assert acks == []
        inbox.messages[0].on_done()
        assert acks == [9]

    def test_multi_group_batch_acks_only_after_all_processed(self):
        # One batch delivering both a bulk group and a chat group: the batch
        # seq is safe only when BOTH pushed messages have been processed.
        inbox, acks = _run_drain([[_bulk(29), _chat(30)]])
        assert len(inbox.messages) == 2
        inbox.messages[0].on_done()
        assert acks == []
        inbox.messages[1].on_done()
        assert acks == [30]

    def test_monitor_error_waits_for_message_completion(self):
        from bobi.events.drain import _MONITOR_ERROR_DELIVERED

        _MONITOR_ERROR_DELIVERED.clear()
        event = {
            "type": "system/monitor.error",
            "seq": 31,
            "payload": {
                "monitor": "sleep-cycle",
                "flavor": "curator",
                "reason": "spawn-failed",
            },
        }

        inbox, acks = _run_drain([[event]])

        assert len(inbox.messages) == 1
        assert acks == [], "monitor alert acked before the session handled it"
        assert inbox.messages[0].on_done is not None
        inbox.messages[0].on_done()
        assert acks == [31]


class TestAckWatermark:
    def test_out_of_order_completion_holds_ack_floor(self):
        # Chat (seq 20) jumps the queue and completes before bulk (seq 10).
        # Acking 20 then would tell the server the bulk event was processed -
        # a restart would lose it. The watermark must hold until 10 completes.
        inbox, acks = _run_drain([[_bulk(10)], [_chat(20)]])
        assert len(inbox.messages) == 2
        bulk_msg, chat_msg = inbox.messages
        chat_msg.on_done()
        assert acks == [], "acked past an unprocessed older batch"
        bulk_msg.on_done()
        assert acks == [20]

    def test_in_order_completion_acks_each_batch(self):
        inbox, acks = _run_drain([[_bulk(10)], [_bulk(20)]])
        inbox.messages[0].on_done()
        assert acks == [10]
        inbox.messages[1].on_done()
        assert acks == [10, 20]

    def test_empty_batch_between_outstanding_batches_waits_its_turn(self):
        # Batch seq 20 pushes nothing while batch seq 10 is still
        # outstanding: an immediate ack of 20 would discard batch 10.
        suppressed = [{"source": "inbox", "type": "inbox/ack-test",
                       "payload": {"text": ""}, "seq": 20}]
        inbox, acks = _run_drain([[_bulk(10)], suppressed])
        assert acks == []
        inbox.messages[0].on_done()
        assert acks == [20]


class TestChatPriorityDelivery:
    def test_chat_group_is_pushed_with_priority(self):
        inbox, _ = _run_drain([[_bulk(1), _chat(2)]])
        # Bulk group first (normal), chat group second (priority).
        assert inbox.priorities == [False, True]

    def test_agent_inbox_messages_are_not_priority(self):
        # Only chat-class external events jump the queue - agent-to-agent
        # inbox messages keep normal ordering.
        events = [{"source": "inbox", "type": "inbox/ack-test",
                   "payload": {"id": "m1", "sender": "peer", "text": "hi"},
                   "seq": 3}]
        inbox, _ = _run_drain([events])
        assert inbox.priorities == [False]


class TestWatermarkReplayOrdering:
    """A reconnect replay can register a LOWER seq after a higher pending one
    (a wedged batch holds the floor, the server replays, and the replay's
    drain batches draw different boundaries). The scan must be by ascending
    seq, not registration order, or the higher seq acks past the lower."""

    def test_lower_seq_registered_late_still_holds_the_floor(self):
        from bobi.events.drain import _AckWatermark

        acks = []
        tracker = _AckWatermark(acks.append)
        b10 = tracker.open_batch(10)
        done10 = b10.attach()
        b10.close()
        b8 = tracker.open_batch(8)  # replayed older events, registered later
        done8 = b8.attach()
        b8.close()

        done10()
        assert acks == [], "acked seq 10 past the unprocessed replayed seq 8"
        done8()
        assert acks == [10]

    def test_refolded_replay_batch_shares_the_seq_refcount(self):
        from bobi.events.drain import _AckWatermark

        acks = []
        tracker = _AckWatermark(acks.append)
        first = tracker.open_batch(4)
        done_a = first.attach()
        first.close()
        replay = tracker.open_batch(4)  # same seq re-delivered
        done_b = replay.attach()
        replay.close()

        done_a()
        assert acks == []
        done_b()
        assert acks == [4]


class TestCursorReplaySimulation:
    def test_cursor_file_not_advanced_until_processed(self, tmp_path):
        # What a restart replays is driven by the saved cursor: a fresh
        # client loads it as last_seen and the server replays everything
        # after. Until the message is processed the file must not move.
        from bobi.events.client import _load_cursor, _save_cursor

        cursor_path = tmp_path / "cursor.json"
        _save_cursor(3, cursor_path)

        inbox = _CaptureInbox()
        register_local_inbox("cursor-test", inbox)
        try:
            with patch("bobi.events.drain.time.sleep"):
                try:
                    drain_loop("cursor-test",
                               queue=_ScriptedQueue([[_bulk(8)]]),
                               formatter=lambda e: e.get("text", ""),
                               cursor_ack=lambda s: _save_cursor(s, cursor_path))
                except KeyboardInterrupt:
                    pass
        finally:
            unregister_local_inbox("cursor-test")

        assert _load_cursor(cursor_path) == 3, (
            "cursor advanced before processing - a restart here would "
            "permanently lose the queued message")
        inbox.messages[0].on_done()
        assert _load_cursor(cursor_path) == 8
