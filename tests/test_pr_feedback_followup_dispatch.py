"""PR comments should reach the director instead of auto-dispatching.

Question-only and actionable PR comments are both visible to the director. The
eng-team markdown policy decides whether to answer directly or launch
pr-feedback, so the shipped auto_dispatch config must not consume comments.
"""

import time
from pathlib import Path
from unittest.mock import patch

import yaml

from bobi.events.reactor import EventReactor

from tests.drain_utils import drain_one_batch

PACKAGE_ROOT = Path(__file__).parent.parent
ENG_TEAM_AGENT_YAML = PACKAGE_ROOT / "agents" / "eng-team" / "agent.yaml"


def _reactor_from_shipped_config():
    """Build a reactor from the real eng-team auto_dispatch rules."""
    cfg = yaml.safe_load(ENG_TEAM_AGENT_YAML.read_text())
    rules = cfg.get("auto_dispatch", [])
    assert rules, "eng-team agent.yaml must define auto_dispatch rules"
    pr_comment_events = {"github.issue_comment", "github.pull_request_review_comment"}
    pr_comment_rules = [r for r in rules if r.get("event") in pr_comment_events]
    assert pr_comment_rules, "PR comment redelivery must have structural dedup"
    assert all(r.get("dedup_only") for r in pr_comment_rules), (
        "PR comments must only be deduped structurally, not auto-dispatched"
    )
    return EventReactor.from_config(rules, cwd="/tmp/proj-326")


def _human_pr_comment(*, number, delivery_id, comment_id, body):
    """A human follow-up comment on a PR."""
    return {
        "type": "github.issue_comment",
        "id": delivery_id,
        "source": "github",
        "delivery": "bulk",
        "topics": ["github:moda-labs/bobi"],
        "text": f"[moda-labs/bobi] comment PR #{number}: {body}",
        "fields": {
            "action": "created",
            "number": number,
            "is_pull_request": True,
            "sender": "underminedsk",
            "comment_id": comment_id,
            "comment_body": body,
            "title": "Some PR",
        },
    }


def _drain_sequentially(events, reactor):
    """Drive each event through its own drain batch on one shared reactor."""
    delivered = []
    for event in events:
        delivered.extend(drain_one_batch(
            [event], session="test-session-326", reactor=reactor))
    return delivered


@patch("bobi.subagent.launch_agent")
def test_followup_pr_comments_reach_director_without_auto_dispatch(mock_launch):
    reactor = _reactor_from_shipped_config()

    first = _human_pr_comment(
        number=294, delivery_id="delivery-aaa", comment_id=1,
        body="Please add a null check.")
    followup = _human_pr_comment(
        number=294, delivery_id="delivery-bbb", comment_id=2,
        body="Why is this helper needed?")

    delivered = _drain_sequentially([first, followup], reactor)

    assert len(delivered) == 2
    assert "Please add a null check." in delivered[0]
    assert "Why is this helper needed?" in delivered[1]
    time.sleep(0.1)
    mock_launch.assert_not_called()
