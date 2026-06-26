"""Integration test for issue #326 — follow-up PR comments must each dispatch.

Reproduces the production bug: a reviewer drops several comments on the same
PR over time, but only the *first* triggers a ``pr-feedback`` dispatch; the
follow-ups are silently dropped.

This drives the REAL pipeline against the SHIPPED config:

    real eng-team agent.yaml `auto_dispatch` rules
        → EventReactor.from_config
        → drain_loop (the actual event-drain path)
        → launch_agent (mocked, so no live Claude session spawns)

Root cause (reactor.py): ``AutoDispatchRule.dedup_key`` keys on
``workflow:topic:number`` — PR-level, comment-agnostic. With the 1800s
``cooldown`` on the pr-feedback rules, any second comment on the same PR
inside the window collapses onto the first key and is treated as a duplicate
(``process`` returns None → no dispatch). Distinct comments are not
duplicates: the key must include a per-delivery discriminator.

This test asserts every distinct human comment dispatches. It FAILS against
the comment-agnostic dedup key (only the first dispatches) and PASSES once the
key incorporates the event's unique delivery id.
"""

import queue
import time
from pathlib import Path
from unittest.mock import patch

import yaml

from bobi.events.drain import drain_loop
from bobi.events.reactor import EventReactor

PACKAGE_ROOT = Path(__file__).parent.parent.parent
ENG_TEAM_AGENT_YAML = PACKAGE_ROOT / "agents" / "eng-team" / "agent.yaml"


def _wait_calls(mock, n, timeout=2.0):
    """Auto-dispatch offloads launch_agent to a daemon thread; wait for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock.call_count >= n:
            return
        time.sleep(0.005)


class _OneShotQueue:
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
        if self._calls == 1 and len(self._events) > 1:
            return False
        return True

    def get_nowait(self):
        if len(self._events) > 1:
            return self._events.pop(1)
        raise queue.Empty


def _reactor_from_shipped_config():
    """Build a reactor from the REAL eng-team auto_dispatch rules."""
    cfg = yaml.safe_load(ENG_TEAM_AGENT_YAML.read_text())
    rules = cfg.get("auto_dispatch", [])
    assert rules, "eng-team agent.yaml must define auto_dispatch rules"
    return EventReactor.from_config(rules, cwd="/tmp/proj-326")


def _human_pr_comment(*, number, delivery_id, body):
    """A human follow-up comment on a PR (github.issue_comment, is_pull_request)."""
    return {
        "type": "github.issue_comment",
        "id": delivery_id,            # unique per webhook delivery / replay-stable
        "source": "github",
        "delivery": "bulk",
        "topics": ["github:moda-labs/bobi"],
        "text": f"[moda-labs/bobi] comment PR #{number}",
        "fields": {
            "action": "created",
            "number": number,
            "is_pull_request": True,
            "sender": "underminedsk",
            "title": "Some PR",
        },
    }


def _drain_one_batch(events, reactor):
    """Run drain_loop for exactly one batch; return delivered inbox texts."""
    from bobi.inbox import register_local_inbox, unregister_local_inbox

    q = _OneShotQueue(events)
    delivered = []

    class _CaptureInbox:
        def push(self, msg):
            delivered.append(msg.text)

    def fake_formatter(event):
        return event.get("text", "")

    register_local_inbox("test-session-326", _CaptureInbox())
    try:
        with patch("bobi.events.drain.time.sleep"):
            try:
                drain_loop("test-session-326", queue=q,
                           formatter=fake_formatter, reactor=reactor)
            except KeyboardInterrupt:
                pass
    finally:
        unregister_local_inbox("test-session-326")
    return delivered


def _drain_sequentially(events, reactor):
    """Drive each event through its own drain batch on one shared reactor.

    Models a reviewer dropping comments over time: each arrives in a separate
    batch, so the reactor's cooldown state carries across them exactly as it
    does in production.
    """
    delivered = []
    for event in events:
        delivered.extend(_drain_one_batch([event], reactor))
    return delivered


@patch("bobi.subagent.launch_agent")
def test_followup_pr_comments_each_dispatch(mock_launch):
    """Two distinct human comments on the same PR → two pr-feedback dispatches.

    The first comment on PR #294 dispatches; the follow-up (distinct delivery
    id, same PR, within the 1800s cooldown) must ALSO dispatch. The bug drops
    the second.
    """
    mock_launch.return_value = "wf-pr-feedback-326"
    reactor = _reactor_from_shipped_config()

    first = _human_pr_comment(
        number=294, delivery_id="delivery-aaa", body="Please add a null check.")
    followup = _human_pr_comment(
        number=294, delivery_id="delivery-bbb", body="Also rename this method.")

    delivered = _drain_sequentially([first, followup], reactor)

    # Both comments reach the inbox (delivery is never the gap)...
    assert len(delivered) == 2
    # ...and BOTH must auto-dispatch pr-feedback (not just the first).
    _wait_calls(mock_launch, 2)
    assert mock_launch.call_count == 2, (
        "follow-up PR comment was dropped — only the first dispatched "
        "(issue #326: dedup key is comment-agnostic so the cooldown "
        "swallows the second distinct comment)"
    )


@patch("bobi.subagent.launch_agent")
def test_redelivery_of_same_comment_still_dedups(mock_launch):
    """Genuine redelivery of the SAME comment (stable id) must NOT re-dispatch.

    Guards the fix against over-correction: the dedup must still suppress a
    replay of the identical event so we don't double-handle one comment.
    """
    mock_launch.return_value = "wf-pr-feedback-326"
    reactor = _reactor_from_shipped_config()

    comment = _human_pr_comment(
        number=294, delivery_id="delivery-aaa", body="Please add a null check.")
    # Same event delivered twice (same delivery id) — e.g. stream replay.
    redelivery = _human_pr_comment(
        number=294, delivery_id="delivery-aaa", body="Please add a null check.")

    _drain_sequentially([comment, redelivery], reactor)

    _wait_calls(mock_launch, 1)
    assert mock_launch.call_count == 1, (
        "redelivery of the identical comment should dedup to a single dispatch"
    )
