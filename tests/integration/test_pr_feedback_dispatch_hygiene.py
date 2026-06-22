"""Integration test for issue #411 — pr-feedback dispatch hygiene.

Reproduces the production harm: the deterministic reactor auto-dispatched a
``pr-feedback`` engineer where it must not, and a single trigger fanned out
into multiple engines that each filed a duplicate ticket (#416/#417/#418).

This drives the REAL pipeline against the SHIPPED config (same approach as the
#326 follow-up test):

    real eng-team agent.yaml `auto_dispatch` rules
        → EventReactor.from_config (with the bot's resolved github login)
        → drain_loop (the actual event-drain path)
        → launch_agent (mocked, so no live Claude session spawns)

Three failure modes, each a separate test that FAILS on the comment-agnostic /
guard-less reactor and PASSES once the #411 fix lands:

  (a) the bot's OWN comment (rendered-spec link, "held" notes) must NOT dispatch,
  (b) a comment on a DRAFT PR held for human approval must NOT dispatch,
  (c) the SAME comment delivered twice must dispatch EXACTLY once (no fan-out).

Plus the happy-path regression: a human comment on a ready PR still dispatches.
"""

import queue
import time
from pathlib import Path
from unittest.mock import patch

import yaml

from modastack.events.drain import drain_loop
from modastack.events.reactor import EventReactor

PACKAGE_ROOT = Path(__file__).parent.parent.parent
ENG_TEAM_AGENT_YAML = PACKAGE_ROOT / "agents" / "eng-team" / "agent.yaml"

BOT_LOGIN = "modastack"


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
    """Build a reactor from the REAL eng-team auto_dispatch rules.

    Threads in the bot's github login so the self-author guard is active —
    exactly how the live agent wires it (subagent._resolve_self_github_login).
    """
    cfg = yaml.safe_load(ENG_TEAM_AGENT_YAML.read_text())
    rules = cfg.get("auto_dispatch", [])
    assert rules, "eng-team agent.yaml must define auto_dispatch rules"
    # The shipped pr-feedback rules must carry the #411 guards. Self-author skip
    # is the DEFAULT (per review, underminedsk) — these rules must NOT opt back
    # in via allow_self_authored — and skip_draft stays an explicit opt-in.
    pr_feedback = [r for r in rules if r.get("workflow") == "pr-feedback"]
    assert pr_feedback, "expected pr-feedback rules in shipped config"
    assert not any(r.get("allow_self_authored") for r in pr_feedback), \
        "shipped pr-feedback rules must rely on default self-author skip (#411)"
    assert all(r.get("skip_draft") for r in pr_feedback), \
        "shipped pr-feedback rules must set skip_draft (#411)"
    return EventReactor.from_config(rules, cwd="/tmp/proj-411",
                                    self_login=BOT_LOGIN)


def _pr_comment(*, number, delivery_id, comment_id, sender):
    """An issue_comment on a PR (github.issue_comment, is_pull_request)."""
    return {
        "type": "github.issue_comment",
        "id": delivery_id,
        "source": "github",
        "delivery": "bulk",
        "topics": ["github:moda-labs/modastack"],
        "text": f"[moda-labs/modastack] comment PR #{number}",
        "fields": {
            "action": "created",
            "number": number,
            "is_pull_request": True,
            "sender": sender,
            "comment_id": comment_id,
            "title": "Some PR",
        },
    }


def _drain_one_batch(events, reactor):
    """Run drain_loop for exactly one batch; return delivered inbox texts."""
    from modastack.inbox import register_local_inbox, unregister_local_inbox

    q = _OneShotQueue(events)
    delivered = []

    class _CaptureInbox:
        def push(self, msg):
            delivered.append(msg.text)

    def fake_formatter(event):
        return event.get("text", "")

    register_local_inbox("test-session-411", _CaptureInbox())
    try:
        with patch("modastack.events.drain.time.sleep"):
            try:
                drain_loop("test-session-411", queue=q,
                           formatter=fake_formatter, reactor=reactor)
            except KeyboardInterrupt:
                pass
    finally:
        unregister_local_inbox("test-session-411")
    return delivered


def _drain_sequentially(events, reactor):
    """Drive each event through its own drain batch on one shared reactor."""
    delivered = []
    for event in events:
        delivered.extend(_drain_one_batch([event], reactor))
    return delivered


# (a) the bot's own comment must not dispatch a feedback engineer ------------

@patch("modastack.events.reactor._pr_is_draft", return_value=False)
@patch("modastack.subagent.launch_agent")
def test_bot_authored_comment_does_not_dispatch(mock_launch, _mock_draft):
    """The lead's own 'Rendered spec' / 'held' comment must NOT dispatch (a)."""
    mock_launch.return_value = "wf-pr-feedback-411"
    reactor = _reactor_from_shipped_config()

    bot_comment = _pr_comment(number=410, delivery_id="d-bot",
                              comment_id=1001, sender=BOT_LOGIN)
    delivered = _drain_sequentially([bot_comment], reactor)

    # Event still reaches the inbox (delivery is never the gap)...
    assert len(delivered) == 1
    # ...but no pr-feedback engine is launched on the bot's own comment.
    time.sleep(0.1)
    assert mock_launch.call_count == 0, \
        "pr-feedback dispatched on the bot's OWN comment (#411 part a)"


# (b) a comment on a draft PR held for approval must not dispatch ------------

@patch("modastack.events.reactor._pr_is_draft", return_value=True)
@patch("modastack.subagent.launch_agent")
def test_comment_on_draft_pr_does_not_dispatch(mock_launch, _mock_draft):
    """A human comment on a DRAFT PR held for approval must NOT dispatch (b)."""
    mock_launch.return_value = "wf-pr-feedback-411"
    reactor = _reactor_from_shipped_config()

    human_comment = _pr_comment(number=410, delivery_id="d-human",
                                comment_id=2002, sender="underminedsk")
    delivered = _drain_sequentially([human_comment], reactor)

    assert len(delivered) == 1
    time.sleep(0.1)
    assert mock_launch.call_count == 0, \
        "pr-feedback dispatched against a held DRAFT PR (#411 part b)"


# (c) one comment dispatches at most one engine (no fan-out) -----------------

@patch("modastack.events.reactor._pr_is_draft", return_value=False)
@patch("modastack.subagent.launch_agent")
def test_same_comment_redelivered_dispatches_once(mock_launch, _mock_draft):
    """The SAME comment via two deliveries → exactly one engine (c).

    This is the #411/#416-#418 fan-out: one trigger redelivered (a webhook plus
    a monitor re-poll, each with a different per-delivery id) must collapse onto
    the stable comment id and dispatch once — not once per delivery.
    """
    mock_launch.return_value = "wf-pr-feedback-411"
    reactor = _reactor_from_shipped_config()

    first = _pr_comment(number=294, delivery_id="d-aaa",
                        comment_id=3003, sender="underminedsk")
    redelivery = _pr_comment(number=294, delivery_id="d-bbb",
                             comment_id=3003, sender="underminedsk")

    _drain_sequentially([first, redelivery], reactor)

    _wait_calls(mock_launch, 1)
    assert mock_launch.call_count == 1, \
        "one comment fanned out into multiple engines (#411 part c)"


# regression: the happy path still works ------------------------------------

@patch("modastack.events.reactor._pr_is_draft", return_value=False)
@patch("modastack.subagent.launch_agent")
def test_human_comment_on_ready_pr_dispatches(mock_launch, _mock_draft):
    """A genuine human comment on a ready (non-draft) PR still dispatches."""
    mock_launch.return_value = "wf-pr-feedback-411"
    reactor = _reactor_from_shipped_config()

    human_comment = _pr_comment(number=294, delivery_id="d-ready",
                                comment_id=4004, sender="underminedsk")
    _drain_sequentially([human_comment], reactor)

    _wait_calls(mock_launch, 1)
    assert mock_launch.call_count == 1, \
        "a real human comment on a ready PR must still dispatch pr-feedback"
    # ...and with a deterministic, per-comment run_key (the cross-process guard).
    assert mock_launch.call_args[1]["run_key"] == "294-comment-4004"
