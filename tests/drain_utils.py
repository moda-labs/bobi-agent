"""Shared one-batch drain harness for the drain_loop test suites (Q043).

Runs the REAL ``bobi.events.drain.drain_loop`` for exactly one batch: a
pre-loaded queue yields the batch and then raises ``KeyboardInterrupt`` to
stop the loop, ``time.sleep`` is patched out, and a capturing inbox is
registered for the session (and always unregistered). The suites that used
to re-build this harness state only their events and assertions; the drift
points between the old copies (reactor or not, text vs Message capture,
session name) are explicit parameters here.
"""

import queue
from unittest.mock import patch


class OneShotQueue:
    """Yield a single pre-loaded batch of events, then stop the drain loop."""

    def __init__(self, events):
        self._events = list(events)
        self._calls = 0

    def get(self):
        self._calls += 1
        if self._calls == 1 and self._events:
            return self._events[0]
        raise KeyboardInterrupt

    def empty(self):
        return not (self._calls == 1 and len(self._events) > 1)

    def get_nowait(self):
        if len(self._events) > 1:
            return self._events.pop(1)
        raise queue.Empty


def drain_one_batch(events, *, session, reactor=None, capture="text"):
    """Run drain_loop for exactly one batch; return what reached the inbox.

    ``capture="text"`` collects each delivered message's text;
    ``capture="message"`` collects the Message objects themselves.
    """
    from bobi.events.drain import drain_loop
    from bobi.inbox import register_local_inbox, unregister_local_inbox

    delivered = []

    class _CaptureInbox:
        def push(self, msg, priority=False):
            delivered.append(msg.text if capture == "text" else msg)

    register_local_inbox(session, _CaptureInbox())
    try:
        with patch("bobi.events.drain.time.sleep"):
            try:
                drain_loop(session, queue=OneShotQueue(events),
                           formatter=lambda e: e.get("text", ""),
                           reactor=reactor)
            except KeyboardInterrupt:
                pass
    finally:
        unregister_local_inbox(session)
    return delivered
