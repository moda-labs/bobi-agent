"""Integration test for MDS-65 — the sub-agent completion-delivery loop.

Reproduces the production bug: detached sub-agents finish silently (their
lifecycle events are emitted into the void because nothing subscribes), and a
crash is recorded as ``done`` so the requester's thread never closes.

This drives the REAL pipeline, end to end, with no live Claude session:

    entry-point subscribe list (cli.py wiring)
        → lifecycle_subscription_keys()                     (RC#1: subscribe)
    a detached run's terminal event
        → drain_loop (the actual event-drain path)          (RC#1: deliver)
        → the manager's inbox, carrying requested_by        (RC#4: routing)
    a crashed run with no terminal event
        → reconcile_sessions()                              (RC#2/#3: backstop)
        → an honest agent/session.failed through the same drain → inbox

It asserts the loop is CLOSED: completions and reconciled crashes reach the
launcher's inbox without ``--wait``. Each piece also has unit coverage
(test_lifecycle_subscription / test_completion_delivery / test_reconcile); this
proves they compose.
"""

import queue
from unittest.mock import patch

from modastack.events.subscriptions import lifecycle_subscription_keys
from modastack.reconcile import reconcile_sessions
from modastack.sdk import (
    SessionEntry, SessionRegistry, get_registry, TERMINAL_CRASHED,
)


# --- minimal drain harness (mirrors test_pr_feedback_followup_dispatch) -----

class _OneShotQueue:
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


def _drain_one_batch(events):
    """Run the real drain_loop for one batch; return delivered inbox texts.

    No reactor → lifecycle events are pure deliver-to-inbox (the manager must be
    woken by its child's completion, never auto-dispatch it)."""
    from modastack.inbox import register_local_inbox, unregister_local_inbox
    from modastack.events.drain import drain_loop

    delivered = []

    class _CaptureInbox:
        def push(self, msg):
            delivered.append(msg.text)

    register_local_inbox("manager", _CaptureInbox())
    try:
        with patch("modastack.events.drain.time.sleep"):
            try:
                drain_loop("manager", queue=_OneShotQueue(events),
                           formatter=lambda e: e.get("text", ""), reactor=None)
            except KeyboardInterrupt:
                pass
    finally:
        unregister_local_inbox("manager")
    return delivered


def _lifecycle_event(event_type, *, run_key, requested_by, text):
    return {
        "type": event_type,
        "id": f"delivery-{run_key}",
        "source": "agent",
        "delivery": "bulk",
        "topics": lifecycle_subscription_keys(),
        "text": text,
        "fields": {"run_key": run_key, "requested_by": requested_by},
    }


# ---------------------------------------------------------------------------

def test_entry_point_subscribes_to_lifecycle_topics():
    """RC#1: the keys the entry point wires in cover both completed and failed,
    in both bare and source-qualified forms."""
    keys = lifecycle_subscription_keys()
    assert "agent/session.completed" in keys
    assert "agent/session.failed" in keys
    assert "session.completed" in keys and "session.failed" in keys


def test_completion_event_reaches_inbox_without_wait():
    """RC#1/RC#4: a detached run's session.completed flows through the real
    drain into the manager's inbox — not auto-dispatched, and the requester is
    preserved for thread routing."""
    ev = _lifecycle_event(
        "agent/session.completed", run_key="ENG-1",
        requested_by={"slack_user": "U1", "thread_ts": "1.2"},
        text="engineer finished ENG-1 in 42s",
    )
    delivered = _drain_one_batch([ev])
    assert any("finished ENG-1" in t for t in delivered)
    # delivered raw (no auto-dispatch annotation)
    assert not any("AUTO-DISPATCHED" in t for t in delivered)


def test_crashed_run_is_reconciled_and_failure_delivered(tmp_path, monkeypatch):
    """RC#2/#3: a run recorded with a dead pid (a crash) is reconciled to an
    honest `crashed` status, and the resulting agent/session.failed — carrying
    requested_by — reaches the launcher's inbox through the same drain. The old
    behaviour recorded the crash as `done` and emitted nothing."""
    monkeypatch.setattr("modastack.paths._root", tmp_path)
    reg = get_registry()
    reg.register(SessionEntry(
        name="wf-issue-lifecycle-app-7", run_key="7", role="engineer",
        project="moda-labs/app", status="running", pid=999999,
        requested_by={"slack_user": "U2", "thread_ts": "9.9"},
    ))

    emitted = []
    actions = reconcile_sessions(emit=lambda et, d: emitted.append((et, d)) or True,
                                 cancel=lambda n: None)

    # Honest terminal status — never `done`.
    assert reg.get("wf-issue-lifecycle-app-7").status == TERMINAL_CRASHED
    assert any(a["action"] == "crashed" for a in actions)

    # The reconciler emitted an honest failure carrying requested_by; feed it
    # through the real drain and confirm it reaches the manager's inbox.
    assert emitted and emitted[0][0] == "agent/session.failed"
    payload = emitted[0][1]
    assert payload["requested_by"] == {"slack_user": "U2", "thread_ts": "9.9"}

    ev = _lifecycle_event(
        "agent/session.failed", run_key="7",
        requested_by=payload["requested_by"], text=payload["text"],
    )
    delivered = _drain_one_batch([ev])
    assert any("crashed on 7" in t for t in delivered)
