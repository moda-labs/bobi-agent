"""Phase 2: agent-pack routing correctness (D017, D119).

Each test here fails against the pre-fix tree. The shipped packs are asserted
directly rather than through a fixture copy — a dead route in a pack we
distribute is the actual defect, not a hypothetical one.
"""

import re
from pathlib import Path

import pytest
import yaml

from bobi.validate import (
    _EVENT_TYPE_SHAPES,
    _check_auto_dispatch_events,
    _check_chat_service,
    _unmatchable_reason,
)

REPO = Path(__file__).resolve().parent.parent
AGENTS = REPO / "agents"


def _pack(name: str) -> dict:
    return yaml.safe_load((AGENTS / name / "agent.yaml").read_text())


class _Svc:
    def __init__(self, name):
        self.name = name


class _Cfg:
    def __init__(self, auto_dispatch=(), chat="", services=()):
        self.auto_dispatch = list(auto_dispatch)
        self.chat = chat
        self.services = [_Svc(s) for s in services]


# --- the matcher itself ---------------------------------------------------

class TestUnmatchableReason:
    def test_flags_a_dotted_github_action(self):
        """D017's exact shape: the action belongs in match:, not the type."""
        reason = _unmatchable_reason("github.issues.assigned")
        assert reason
        assert "match: {action: assigned}" in reason

    @pytest.mark.parametrize("event_type", [
        "github.issues", "github.pull_request", "github.pull_request_review",
        "github.issue_comment",
    ])
    def test_accepts_real_github_types(self, event_type):
        assert _unmatchable_reason(event_type) == ""

    def test_accepts_three_segment_linear_types(self):
        """The naive 'three segments is wrong' rule would reject these.

        Linear puts the action IN the type (`linear.${dataType}.${action}`),
        which is exactly why the check has to be source-aware.
        """
        assert _unmatchable_reason("linear.Issue.create") == ""
        assert _unmatchable_reason("linear.Comment.update") == ""

    def test_flags_a_two_segment_linear_type(self):
        assert _unmatchable_reason("linear.Issue") != ""

    def test_accepts_the_closed_chat_sets(self):
        for t in ("slack.mention", "slack.dm", "slack.thread_reply",
                  "discord.dm", "discord.reply", "discord.mention",
                  "whatsapp.message"):
            assert _unmatchable_reason(t) == "", t

    def test_flags_an_invented_slack_type(self):
        assert _unmatchable_reason("slack.channel_message") != ""

    def test_fails_open_on_an_unknown_source(self):
        """A source this table has not been taught must never be flagged.

        The check is advisory; a false positive on a new adapter would be
        worse than the silent dead rule it exists to catch.
        """
        assert _unmatchable_reason("gitlab.merge_request.opened") == ""
        assert _unmatchable_reason("acme.thing") == ""

    def test_ignores_in_process_system_events(self):
        assert _unmatchable_reason("system/spend.cap.breached") == ""


# --- the checks -----------------------------------------------------------

class TestAutoDispatchCheck:
    def test_unmatchable_rule_fails_validation(self):
        checks = _check_auto_dispatch_events(
            _Cfg(auto_dispatch=[{"event": "github.issues.assigned",
                                 "workflow": "issue-lifecycle"}]))
        assert len(checks) == 1
        assert checks[0].ok is False
        assert "issue-lifecycle" in checks[0].detail

    def test_corrected_rule_passes(self):
        checks = _check_auto_dispatch_events(
            _Cfg(auto_dispatch=[{"event": "github.issues",
                                 "match": {"action": "assigned"},
                                 "workflow": "issue-lifecycle"}]))
        assert checks == []

    def test_is_a_warning_not_a_startup_blocker(self):
        """An inert rule must not refuse to start an already-deployed team.

        Making this required would turn a silent dead rule into an outage
        the first time a fleet box upgrades bobi without reinstalling its
        pack.
        """
        checks = _check_auto_dispatch_events(
            _Cfg(auto_dispatch=[{"event": "github.issues.assigned"}]))
        assert checks[0].required is False


class TestChatServiceCheck:
    def test_chat_without_the_service_fails(self):
        checks = _check_chat_service(_Cfg(chat="slack",
                                          services=["github", "email"]))
        assert len(checks) == 1 and checks[0].ok is False

    def test_chat_with_the_service_passes(self):
        assert _check_chat_service(
            _Cfg(chat="slack", services=["github", "slack"])) == []

    def test_no_chat_and_cli_chat_pass(self):
        assert _check_chat_service(_Cfg(chat="", services=["github"])) == []
        assert _check_chat_service(_Cfg(chat="cli", services=["github"])) == []


# --- the shipped packs ----------------------------------------------------

class TestShippedPacksRoute:
    @pytest.mark.parametrize("pack", ["eng-team", "personal-assistant"])
    def test_no_pack_carries_an_unmatchable_dispatch_rule(self, pack):
        raw = _pack(pack)
        cfg = _Cfg(auto_dispatch=raw.get("auto_dispatch", []))
        bad = [c.detail for c in _check_auto_dispatch_events(cfg)]
        assert bad == [], f"{pack}: {bad}"

    @pytest.mark.parametrize("pack", ["eng-team", "personal-assistant"])
    def test_no_pack_names_an_undeclared_chat_service(self, pack):
        raw = _pack(pack)
        cfg = _Cfg(chat=raw.get("chat", ""),
                   services=[s["name"] for s in raw.get("services", [])])
        bad = [c.detail for c in _check_chat_service(cfg)]
        assert bad == [], f"{pack}: {bad}"

    def test_issue_assignment_dispatches_deterministically(self):
        """D017: the rule that makes issue pickup not depend on the LLM."""
        from bobi.events.reactor import AutoDispatchRule

        raw = _pack("eng-team")
        rules = [AutoDispatchRule(**r) for r in raw["auto_dispatch"]
                 if r.get("workflow")]
        event = {
            "type": "github.issues",
            "fields": {"action": "assigned", "number": "42",
                       "repo": "moda-labs/bobi-agent", "title": "a task"},
        }
        matched = [r for r in rules if r.matches(event)]
        assert [r.workflow for r in matched] == ["issue-lifecycle"]


# --- the drift pin --------------------------------------------------------

class TestEventTypeTableMatchesTheAdapters:
    """The table is hand-maintained from TypeScript; pin it like the tokens.

    Nothing in Python can import the adapters, so the only thing standing
    between this table and silent drift is a test that reads their source.
    """

    def _adapter(self, name: str) -> str:
        return (REPO / "event-server" / "core" / "src" / "adapters"
                / f"{name}.ts").read_text()

    def test_github_still_emits_two_segments(self):
        assert "type: `github.${eventHeader}`" in self._adapter("github")
        assert _EVENT_TYPE_SHAPES["github"]["segments"] == 2

    def test_linear_still_embeds_the_action(self):
        assert "type: `linear.${dataType}.${action}`" in self._adapter("linear")
        assert _EVENT_TYPE_SHAPES["linear"]["segments"] == 3

    def test_slack_literals_match(self):
        src = self._adapter("chat-sdk-slack")
        found = set(re.findall(r'slackEventType = "(slack\.[a-z_]+)"', src))
        assert found == _EVENT_TYPE_SHAPES["slack"]["types"], found

    def test_discord_literals_match(self):
        src = self._adapter("discord")
        found = set(re.findall(r'type = "(discord\.[a-z_]+)"', src))
        assert found == _EVENT_TYPE_SHAPES["discord"]["types"], found

    def test_whatsapp_literal_matches(self):
        assert 'type: "whatsapp.message"' in self._adapter("whatsapp")
        assert _EVENT_TYPE_SHAPES["whatsapp"]["types"] == {"whatsapp.message"}
