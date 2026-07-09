"""Regression for the #718 inbox-loss narrative, tracked in #719 defect 3.

In #718, two user Slack messages sent during a killed turn "appeared nowhere in
the post-restart transcript" — every restart was silent data loss. The fix is
ACK-after-processing (#688): the event cursor is a watermark that only advances
once the session has PROCESSED an event, so a restart replays anything still
outstanding from the persisted cursor rather than resetting to next_seq:1.

This test ties the pieces together end-to-end — drain → watermark → cursor file
→ reconnect — and proves the exact #718 sequence no longer loses events:

1. events delivered to the inbox while the session is wedged/killed (pushed but
   never processed) never advance the cursor, and
2. a fresh process reconnects from that pinned cursor, so the server replays the
   undelivered events instead of skipping them.
"""

import json
import queue
from unittest.mock import patch

from bobi.events.client import (
    EventServerClient,
    _load_cursor,
    _save_cursor,
)
from bobi.events.drain import drain_loop
from bobi.inbox import register_local_inbox, unregister_local_inbox


class _ScriptedQueue:
    """Yields one scripted batch to drain_loop, then stops it."""

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


class _WedgedInbox:
    """Accepts pushes but never processes them — a session stuck in a killed
    turn. The delivered messages' on_done callbacks therefore never fire."""

    def __init__(self):
        self.messages = []

    def push(self, msg, priority=False):
        self.messages.append(msg)


def _user_chat(seq, text):
    # A user Slack-style message. Unknown source has no channel handler, so it
    # passes through chat preparation untouched.
    return {"type": "slack.message", "text": text, "delivery": "chat",
            "source": "slack", "seq": seq}


def _client(tmp_path):
    return EventServerClient(
        server_url="http://localhost:9999",
        deployment_id="dep-1",
        api_key="key-1",
        cursor_path=tmp_path / "cursor.json",
    )


def _capture_connect_url(client, monkeypatch):
    from bobi.events import client as client_mod
    captured = {}

    class FakeWSApp:
        def __init__(self, url, **kwargs):
            captured["url"] = url

        def run_forever(self, **kwargs):
            return

    monkeypatch.setattr(client_mod.websocket, "WebSocketApp", FakeWSApp)
    client._connect()
    return captured["url"]


def test_events_delivered_during_a_killed_turn_replay_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("bobi.events.client._log_event", lambda *a, **k: None)

    # A running process has processed events through seq 4.
    client = _client(tmp_path)
    _save_cursor(4, client.cursor_path)

    # Two user Slack messages (seq 5, 6) arrive during a killed turn: the drain
    # delivers them to the session inbox, but the wedged session never processes
    # them — exactly the #718 window.
    inbox = _WedgedInbox()
    register_local_inbox("director", inbox)
    try:
        with patch("bobi.events.drain.time.sleep"):
            try:
                drain_loop(
                    "director",
                    queue=_ScriptedQueue([[
                        _user_chat(5, "first message"),
                        _user_chat(6, "second message"),
                    ]]),
                    formatter=lambda e: e.get("text", ""),
                    cursor_ack=client.ack_through,
                )
            except KeyboardInterrupt:
                pass
    finally:
        unregister_local_inbox("director")

    # The messages were delivered to the inbox...
    assert len(inbox.messages) == 1  # one chat group carrying both lines
    assert "first message" in inbox.messages[0].text
    assert "second message" in inbox.messages[0].text
    # ...but never processed, so the cursor must NOT advance past them.
    assert _load_cursor(client.cursor_path) == 4

    # Restart: a fresh process (nothing enqueued in-memory yet) reconnects from
    # the persisted cursor, so the server replays seq 5 and 6 rather than
    # resetting to next_seq:1 and dropping them.
    fresh = _client(tmp_path)
    url = _capture_connect_url(fresh, monkeypatch)
    assert "last_seen=4" in url


def test_processed_events_advance_the_cursor_past_replay(tmp_path, monkeypatch):
    """Once the session DOES process the delivered messages, the watermark
    advances the cursor so they are not replayed again after a restart."""
    monkeypatch.setattr("bobi.events.client._log_event", lambda *a, **k: None)

    client = _client(tmp_path)
    _save_cursor(4, client.cursor_path)

    inbox = _WedgedInbox()
    register_local_inbox("director", inbox)
    try:
        with patch("bobi.events.drain.time.sleep"):
            try:
                drain_loop(
                    "director",
                    queue=_ScriptedQueue([[_user_chat(5, "hi"), _user_chat(6, "yo")]]),
                    formatter=lambda e: e.get("text", ""),
                    cursor_ack=client.ack_through,
                )
            except KeyboardInterrupt:
                pass
    finally:
        unregister_local_inbox("director")

    # The session processes the delivered batch.
    inbox.messages[0].on_done()

    # Cursor now covers seq 6; a fresh process replays only from there.
    assert _load_cursor(client.cursor_path) == 6
    fresh = _client(tmp_path)
    url = _capture_connect_url(fresh, monkeypatch)
    assert "last_seen=6" in url
