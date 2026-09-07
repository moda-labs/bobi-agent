"""Deterministic issue-assignment dispatch (unit lane: in-process drain,
mocked launch boundary).

The matcher and drain path are brain-agnostic, so the launch boundary is mocked
while the test drives the shipped eng-team rule through the real drain loop.
"""

import time
from pathlib import Path
from unittest.mock import patch

import yaml

from bobi.events.reactor import EventReactor

from tests.drain_utils import drain_one_batch


PACKAGE_ROOT = Path(__file__).parent.parent
ENG_TEAM_AGENT_YAML = PACKAGE_ROOT / "agents" / "eng-team" / "agent.yaml"
BOT_LOGIN = "bobi"


def _reactor_from_shipped_config() -> EventReactor:
    config = yaml.safe_load(ENG_TEAM_AGENT_YAML.read_text())
    return EventReactor.from_config(
        config["auto_dispatch"],
        cwd="/tmp/mod-297",
        self_login=BOT_LOGIN,
    )


def _issue_event(*, action: str, assignees: str) -> dict:
    return {
        "type": "github.issues",
        "id": f"mod-297-{action}-{assignees}",
        "source": "github",
        "delivery": "bulk",
        "topics": ["github:moda-labs/bobi-agent"],
        "text": f"[moda-labs/bobi-agent] {action} issue #814",
        "fields": {
            "action": action,
            "number": 814,
            "title": "Fix assignment dispatch",
            "assignees": assignees,
        },
    }


def _drain_one(event: dict, reactor: EventReactor) -> list[str]:
    return drain_one_batch([event], session="mod-297", reactor=reactor)


@patch("bobi.subagent.launch_agent")
def test_self_assignment_launches_issue_lifecycle(mock_launch):
    delivered = _drain_one(
        _issue_event(action="assigned", assignees="alice, bobi"),
        _reactor_from_shipped_config(),
    )

    deadline = time.monotonic() + 2
    while mock_launch.call_count == 0 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert len(delivered) == 1
    assert "AUTO-DISPATCHED" in delivered[0]
    mock_launch.assert_called_once()
    assert mock_launch.call_args.kwargs["workflow_name"] == "issue-lifecycle"


@patch("bobi.subagent.launch_agent")
def test_human_assignment_and_other_actions_do_not_launch(mock_launch):
    reactor = _reactor_from_shipped_config()

    human_assignment = _drain_one(
        _issue_event(action="assigned", assignees="alice, bob"), reactor
    )
    other_actions = [
        _drain_one(_issue_event(action=action, assignees="bobi"), reactor)
        for action in ("opened", "closed", "labeled")
    ]
    delivered = human_assignment + [
        text for action_delivery in other_actions for text in action_delivery
    ]

    assert len(delivered) == 4
    assert all("AUTO-DISPATCHED" not in text for text in delivered)
    mock_launch.assert_not_called()
