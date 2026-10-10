"""ACK-after-processing drain tests (#688).

The drain must not ACK the event-server cursor when it pushes a batch into
the session inbox - only when the session finishes processing the pushed
message(s). Otherwise a restart destroys every queued-but-unprocessed
message: the server believes them delivered and never replays them.

Chat priority (#688) means messages complete out of push order, so the ack
must be a watermark: never ack past an older, still-unprocessed batch.
"""

import asyncio
import queue
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bobi.events.drain import drain_loop
from bobi.inbox import Message, register_local_inbox, unregister_local_inbox


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
        self.readable = True

    def push(self, msg, priority=False):
        self.messages.append(msg)
        self.priorities.append(priority)

    def depth(self):
        return len(self.messages)


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


@pytest.mark.asyncio
async def test_real_stub_session_processes_aged_bulk_before_remaining_chat(
        bobi_install, monkeypatch, tmp_path):
    from bobi.brain.stub import StubBrain
    from bobi.events.client import _load_cursor, _save_cursor
    from bobi.sdk import SessionEntry, get_registry
    from bobi.session import Session

    monkeypatch.setenv("BOBI_STUB_BRAIN", "1")
    now = [0.0]
    monkeypatch.setattr("bobi.inbox.time", SimpleNamespace(
        time=time.time, monotonic=lambda: now[0]))
    client = StubBrain().make_session()
    original_query = client.query
    processed = []

    async def query(text):
        processed.append(text)
        now[0] = 120.0
        await original_query(text)

    client.query = query
    session = Session(name="aging-session", cwd=str(bobi_install.repo_path))
    session._make_brain_session = lambda resume=None: client
    original_recv = session.inbox.recv
    session.inbox.recv = lambda timeout=2.0: original_recv(timeout=0.01)
    get_registry().register(SessionEntry(name=session.name, status="running"))
    session.inbox.start()
    cursor_path = tmp_path / "cursor.json"
    _save_cursor(9, cursor_path)
    with patch("bobi.events.drain.time.sleep"):
        try:
            drain_loop(
                session.name,
                queue=_ScriptedQueue([[_bulk(10)], [_chat(11, "first chat")],
                                      [_chat(12, "second chat")]]),
                formatter=lambda event: event["text"],
                cursor_ack=lambda seq: _save_cursor(seq, cursor_path))
        except KeyboardInterrupt:
            pass
    assert _load_cursor(cursor_path) == 9
    run_task = asyncio.create_task(session._run())

    async def wait_for_completion():
        while _load_cursor(cursor_path) != 12:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_for_completion(), timeout=5)
        assert processed == ["first chat", "bulk event", "second chat"]
    finally:
        if session._keep_alive is not None:
            session._keep_alive.set()
        await asyncio.wait_for(run_task, timeout=2)
        session.inbox.close()


class TestAckAfterProcessing:
    def test_external_event_retains_timestamp_until_consumption(self):
        event = _bulk(5)
        event["timestamp"] = "2026-08-21T19:58:47+00:00"

        inbox, _ = _run_drain([[event]])

        assert inbox.messages[0].event_timestamps == (event["timestamp"],)

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

    def test_unreadable_inbox_drops_without_ack_for_restart_replay(self):
        inbox = _CaptureInbox()
        inbox.readable = False
        acks = []
        register_local_inbox("ack-test", inbox)
        try:
            with patch("bobi.events.drain.time.sleep"):
                try:
                    drain_loop("ack-test", queue=_ScriptedQueue([[_bulk(5)]]),
                               formatter=lambda e: e.get("text", ""),
                               cursor_ack=acks.append)
                except KeyboardInterrupt:
                    pass
        finally:
            unregister_local_inbox("ack-test")

        assert inbox.messages == []
        assert acks == []


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

    def test_aged_bulk_unpins_real_cursor_without_ack_at_dequeue(self,
                                                               tmp_path,
                                                               monkeypatch):
        from bobi.events.client import _load_cursor, _save_cursor
        from bobi.inbox import Inbox

        now = [0.0]
        monkeypatch.setattr("bobi.inbox.time.monotonic", lambda: now[0])
        cursor_path = tmp_path / "cursor.json"
        _save_cursor(9, cursor_path)
        inbox = Inbox("aging-cursor-test")
        inbox.start()
        try:
            with patch("bobi.events.drain.time.sleep"):
                try:
                    drain_loop(
                        inbox.session_name,
                        queue=_ScriptedQueue([[_bulk(10)], [_chat(11)]]),
                        formatter=lambda event: event["text"],
                        cursor_ack=lambda seq: _save_cursor(seq, cursor_path))
                except KeyboardInterrupt:
                    pass
            chat = inbox.recv(timeout=0)
            assert chat.text == "chat message"
            chat.on_done()
            assert _load_cursor(cursor_path) == 9
            inbox.push(Message(id="new-chat", sender="human", text="new-chat"),
                       priority=True)
            now[0] = 120.0
            bulk = inbox.recv(timeout=0)
            assert bulk.text == "bulk event"
            assert _load_cursor(cursor_path) == 9
            bulk.on_done()
            assert _load_cursor(cursor_path) == 11
        finally:
            inbox.close()


class TestWatermarkDiagnostics:
    def test_real_drain_persists_pin_and_clears_it_after_processing(self,
                                                                bobi_install):
        from bobi.sdk import SessionEntry, get_registry

        registry = get_registry()
        registry.register(SessionEntry(name="ack-test", last_activity=10.0))
        inbox, acks = _run_drain([[_bulk(10)], [_chat(20)]])
        snapshot = registry.get("ack-test").ack_watermark
        assert snapshot["pinned_seq"] == 10
        assert snapshot["pending_batches"] == 2
        assert snapshot["oldest_event_type"] == "ci.check_run"
        assert snapshot["oldest_pending_at"] > 0
        inbox.messages[1].on_done()
        assert registry.get("ack-test").ack_watermark == snapshot
        assert acks == []
        inbox.messages[0].on_done()
        assert registry.get("ack-test").ack_watermark == {
            "pinned_seq": None, "pending_batches": 0,
            "oldest_pending_at": 0.0, "oldest_event_type": ""}
        assert registry.get("ack-test").last_activity == 10.0
        assert acks == [20]

    def test_diagnostic_write_failure_does_not_prevent_delivery_or_ack(self,
                                                                   monkeypatch,
                                                                   caplog):
        from bobi.sdk import SessionRegistry

        def fail_write(*args):
            raise OSError("read-only diagnostic state")

        monkeypatch.setattr(SessionRegistry, "update_ack_watermark", fail_write)
        inbox, acks = _run_drain([[_bulk(1)]])
        inbox.messages[0].on_done()
        assert acks == [1]
        assert "Could not persist ACK watermark" in caplog.text

    def test_concurrent_completion_never_acks_past_an_outstanding_batch(self):
        from bobi.events.drain import _AckWatermark

        acks = []
        tracker = _AckWatermark(acks.append)
        oldest = tracker.open_batch(1)
        callbacks = []
        for seq in range(2, 20):
            batch = tracker.open_batch(seq)
            callbacks.append(batch.attach())
            batch.close()
        workers = [threading.Thread(target=callback) for callback in callbacks]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()
        assert acks == []
        oldest.close()
        assert acks == [19]

    def test_oldest_batch_warns_on_age_and_repeats_with_backoff(self,
                                                              monkeypatch,
                                                              caplog):
        from bobi.events.drain import _AckWatermark

        now = [0.0]
        monkeypatch.setattr("bobi.events.drain.time.monotonic", lambda: now[0])
        acks = []
        tracker = _AckWatermark(acks.append)
        tracker.open_batch(10, "agent/session.completed")
        with caplog.at_level(logging.WARNING):
            for seq, elapsed in ((11, 299), (12, 300), (13, 359),
                                 (14, 360), (15, 479), (16, 480),
                                 (17, 719), (18, 720), (19, 1019),
                                 (20, 1020), (21, 1319), (22, 1320)):
                now[0] = float(elapsed)
                tracker.open_batch(seq, "chat.message").close()
        assert len(caplog.records) == 6
        assert all("seq 10" in record.message for record in caplog.records)
        assert all("agent/session.completed" in record.message
                   for record in caplog.records)
        assert "300" in caplog.records[0].message
        assert acks == []

    def test_batch_count_warns_at_64_and_repeats_above_threshold(self,
                                                              monkeypatch,
                                                              caplog):
        from bobi.events.drain import _AckWatermark

        now = [0.0]
        monkeypatch.setattr("bobi.events.drain.time.monotonic", lambda: now[0])
        tracker = _AckWatermark(lambda seq: None)
        with caplog.at_level(logging.WARNING):
            for seq in range(1, 66):
                tracker.open_batch(seq)
            assert len(caplog.records) == 1
            assert "64" in caplog.records[0].message
            now[0] = 60.0
            tracker.open_batch(66)
        assert len(caplog.records) == 2

    def test_warning_backoff_resets_when_pinning_batch_completes(self,
                                                              monkeypatch,
                                                              caplog):
        from bobi.events.drain import _AckWatermark

        now = [0.0]
        monkeypatch.setattr("bobi.events.drain.time.monotonic", lambda: now[0])
        tracker = _AckWatermark(lambda seq: None)
        oldest = tracker.open_batch(1)
        tracker.open_batch(2)
        with caplog.at_level(logging.WARNING):
            now[0] = 300.0
            tracker.open_batch(3)
            oldest.close()
            tracker.open_batch(4)
        assert ["seq 1" in record.message for record in caplog.records] == [
            True, False]
        assert "seq 2" in caplog.records[-1].message


class TestAckPrecedesDiagnostics:
    """The real cursor ACK must not queue behind the diagnostics write.

    _report() persists the watermark snapshot through
    SessionRegistry.update_ack_watermark, which takes a cross-process
    fcntl.flock on the session state file. The completion callback that
    reaches _done() is bounded by MESSAGE_ACK_TIMEOUT (bobi/session.py), and
    blowing that bound marks the session terminally errored. So a contended
    snapshot write ahead of the ACK starves the thing the budget exists for.
    """

    def test_stalled_diagnostic_write_does_not_delay_the_real_ack(
            self, monkeypatch):
        from bobi.events.drain import _AckWatermark

        acked = threading.Event()
        released = threading.Event()
        tracker = _AckWatermark(lambda seq: acked.set())
        batch = tracker.open_batch(1)
        done = batch.attach()
        batch.close()

        def stalled_report(self):
            # Stands in for a contended state-file flock: returns only once
            # the ACK has already gone out.
            released.wait(timeout=5)

        monkeypatch.setattr(_AckWatermark, "_report", stalled_report)
        worker = threading.Thread(target=done)
        worker.start()
        try:
            assert acked.wait(timeout=5), (
                "cursor ACK was gated behind the diagnostics write")
        finally:
            released.set()
            worker.join(timeout=5)
        assert not worker.is_alive()

    def test_failing_ack_still_persists_the_diagnostic_snapshot(
            self, monkeypatch):
        # The ACK running first must not hand it a veto over diagnostics:
        # ack_through -> _save_cursor raises on a cursor-file write failure,
        # and that is exactly when the snapshot explaining it matters.
        from bobi.events.drain import _AckWatermark

        def unwritable_cursor(seq):
            raise OSError("read-only cursor file")

        reports = []
        original_report = _AckWatermark._report

        def recording_report(self):
            reports.append(self._acked)
            original_report(self)

        tracker = _AckWatermark(unwritable_cursor)
        batch = tracker.open_batch(1)
        done = batch.attach()
        batch.close()
        monkeypatch.setattr(_AckWatermark, "_report", recording_report)
        with pytest.raises(OSError):
            done()
        assert reports == [1], "diagnostics skipped when the ACK raised"
