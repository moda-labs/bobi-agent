"""Tests for event reactor — deterministic auto-dispatch of workflows on event match."""

import time
from unittest.mock import patch, MagicMock

import pytest

from modastack.events.reactor import AutoDispatchRule, EventReactor


class TestAutoDispatchRule:
    """AutoDispatchRule matches events by type and optional field conditions."""

    def test_matches_event_type_exact(self):
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
        )
        event = {"type": "github.pull_request_review", "fields": {}}
        assert rule.matches(event) is True

    def test_rejects_wrong_event_type(self):
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
        )
        event = {"type": "github.issues", "fields": {}}
        assert rule.matches(event) is False

    def test_matches_with_field_condition(self):
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
            match={"review_state": "changes_requested"},
        )
        event = {
            "type": "github.pull_request_review",
            "fields": {"review_state": "changes_requested", "number": 42},
        }
        assert rule.matches(event) is True

    def test_rejects_when_field_condition_fails(self):
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
            match={"review_state": "changes_requested"},
        )
        event = {
            "type": "github.pull_request_review",
            "fields": {"review_state": "approved", "number": 42},
        }
        assert rule.matches(event) is False

    def test_matches_with_multiple_field_conditions(self):
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
            match={"review_state": "changes_requested", "sender": "alice"},
        )
        event = {
            "type": "github.pull_request_review",
            "fields": {"review_state": "changes_requested", "sender": "alice"},
        }
        assert rule.matches(event) is True

    def test_rejects_when_one_field_condition_fails(self):
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
            match={"review_state": "changes_requested", "sender": "alice"},
        )
        event = {
            "type": "github.pull_request_review",
            "fields": {"review_state": "changes_requested", "sender": "bob"},
        }
        assert rule.matches(event) is False

    def test_matches_without_match_dict(self):
        """No match conditions = match any event of the right type."""
        rule = AutoDispatchRule(
            event="github.pull_request_review_comment",
            workflow="pr-feedback",
        )
        event = {
            "type": "github.pull_request_review_comment",
            "fields": {"number": 10, "sender": "reviewer"},
        }
        assert rule.matches(event) is True

    def test_field_condition_missing_field_does_not_match(self):
        rule = AutoDispatchRule(
            event="github.issue_comment",
            workflow="pr-feedback",
            match={"is_pull_request": True},
        )
        event = {
            "type": "github.issue_comment",
            "fields": {"number": 5},
        }
        assert rule.matches(event) is False

    def test_builds_dedup_key_from_event(self):
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
        )
        event = {
            "type": "github.pull_request_review",
            "topics": ["github:moda-labs/test"],
            "fields": {"number": 42},
        }
        key = rule.dedup_key(event)
        assert key == "pr-feedback:github:moda-labs/test:42"

    def test_dedup_key_without_number(self):
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
        )
        event = {
            "type": "github.pull_request_review",
            "topics": ["github:moda-labs/test"],
            "fields": {},
        }
        key = rule.dedup_key(event)
        assert key == "pr-feedback:github:moda-labs/test:unknown"


class TestEventReactor:
    """EventReactor matches events to rules and dispatches workflows."""

    def _make_reactor(self, rules=None, cwd="/tmp/project", cooldown=1800):
        rules = rules or [
            AutoDispatchRule(
                event="github.pull_request_review",
                workflow="pr-feedback",
                match={"review_state": "changes_requested"},
                cooldown=cooldown,
            ),
            AutoDispatchRule(
                event="github.pull_request_review_comment",
                workflow="pr-feedback",
                cooldown=cooldown,
            ),
        ]
        return EventReactor(rules=rules, cwd=cwd)

    def _make_review_event(self, review_state="changes_requested", number=42):
        return {
            "type": "github.pull_request_review",
            "source": "github",
            "topics": ["github:moda-labs/test"],
            "fields": {
                "action": "submitted",
                "number": number,
                "title": "Fix bug",
                "state": "open",
                "sender": "reviewer1",
                "review_state": review_state,
            },
        }

    def _make_review_comment_event(self, number=42):
        return {
            "type": "github.pull_request_review_comment",
            "source": "github",
            "topics": ["github:moda-labs/test"],
            "fields": {
                "action": "created",
                "number": number,
                "title": "Fix bug",
                "sender": "reviewer1",
                "comment_body": "This needs a null check.",
                "comment_path": "src/handler.ts",
            },
        }

    @patch("modastack.subagent.launch_agent")
    def test_dispatches_on_matching_event(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event = self._make_review_event()

        result = reactor.process(event)

        assert result == "dispatched"
        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs[1]["workflow_name"] == "pr-feedback"
        assert "PR #42" in call_kwargs[1]["task"]

    @patch("modastack.subagent.launch_agent")
    def test_no_dispatch_on_non_matching_event(self, mock_launch):
        reactor = self._make_reactor()
        event = {"type": "github.issues", "fields": {"action": "opened"}}

        result = reactor.process(event)

        assert result is None
        mock_launch.assert_not_called()

    @patch("modastack.subagent.launch_agent")
    def test_no_dispatch_when_review_state_is_approved(self, mock_launch):
        reactor = self._make_reactor()
        event = self._make_review_event(review_state="approved")

        result = reactor.process(event)

        assert result is None
        mock_launch.assert_not_called()

    @patch("modastack.subagent.launch_agent")
    def test_dispatches_on_review_comment(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event = self._make_review_comment_event()

        result = reactor.process(event)

        assert result == "dispatched"
        mock_launch.assert_called_once()

    @patch("modastack.subagent.launch_agent")
    def test_dedup_prevents_rapid_double_dispatch(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event = self._make_review_event()

        assert reactor.process(event) == "dispatched"
        assert reactor.process(event) is None
        assert mock_launch.call_count == 1

    @patch("modastack.subagent.launch_agent")
    def test_dedup_allows_dispatch_after_cooldown(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor(cooldown=0)  # zero cooldown
        event = self._make_review_event()

        assert reactor.process(event) == "dispatched"
        assert reactor.process(event) == "dispatched"
        assert mock_launch.call_count == 2

    @patch("modastack.subagent.launch_agent")
    def test_different_prs_dispatch_independently(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event1 = self._make_review_event(number=42)
        event2 = self._make_review_event(number=43)

        assert reactor.process(event1) == "dispatched"
        assert reactor.process(event2) == "dispatched"
        assert mock_launch.call_count == 2

    @patch("modastack.subagent.launch_agent")
    def test_graceful_on_launch_failure_active_session(self, mock_launch):
        """If launch_agent raises because session is already active, handle gracefully."""
        mock_launch.side_effect = RuntimeError("A run is already active")
        reactor = self._make_reactor()
        event = self._make_review_event()

        # Should not raise, should return "dispatched" (handled)
        result = reactor.process(event)
        assert result == "dispatched"

    @patch("modastack.subagent.launch_agent")
    def test_task_includes_pr_context(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event = self._make_review_event()

        reactor.process(event)

        task = mock_launch.call_args[1]["task"]
        assert "#42" in task
        assert "moda-labs/test" in task

    @patch("modastack.subagent.launch_agent")
    def test_empty_rules_never_dispatches(self, mock_launch):
        reactor = EventReactor(rules=[], cwd="/tmp")
        event = self._make_review_event()

        assert reactor.process(event) is None
        mock_launch.assert_not_called()

    @patch("modastack.subagent.launch_agent")
    def test_suppress_rule_returns_suppressed_without_dispatch(self, mock_launch):
        """Suppress rules match the event but don't launch a workflow."""
        rules = [
            AutoDispatchRule(
                event="github.pull_request",
                workflow="",
                match={"action": "review_requested"},
                suppress=True,
            ),
            AutoDispatchRule(
                event="github.pull_request_review",
                workflow="pr-feedback",
                match={"review_state": "changes_requested"},
            ),
        ]
        reactor = EventReactor(rules=rules, cwd="/tmp")
        event = {
            "type": "github.pull_request",
            "source": "github",
            "topics": ["github:moda-labs/test"],
            "fields": {
                "action": "review_requested",
                "number": 99,
                "title": "Some PR",
                "sender": "someuser",
            },
        }

        result = reactor.process(event)

        assert result == "suppressed"
        mock_launch.assert_not_called()

    @patch("modastack.subagent.launch_agent")
    def test_suppress_rule_respects_cooldown(self, mock_launch):
        """Suppress rules use cooldown to prevent re-suppressing the same event."""
        rules = [
            AutoDispatchRule(
                event="github.pull_request",
                workflow="",
                match={"action": "review_requested"},
                suppress=True,
                cooldown=1800,
            ),
        ]
        reactor = EventReactor(rules=rules, cwd="/tmp")
        event = {
            "type": "github.pull_request",
            "source": "github",
            "topics": ["github:moda-labs/test"],
            "fields": {"action": "review_requested", "number": 99},
        }

        assert reactor.process(event) == "suppressed"
        # Second call within cooldown returns None (already handled)
        assert reactor.process(event) is None
        mock_launch.assert_not_called()

    @patch("modastack.subagent.launch_agent")
    def test_suppress_does_not_block_other_pr_events(self, mock_launch):
        """Suppress rule for review_requested doesn't affect other pull_request actions."""
        mock_launch.return_value = "wf-test"
        rules = [
            AutoDispatchRule(
                event="github.pull_request",
                workflow="",
                match={"action": "review_requested"},
                suppress=True,
            ),
            AutoDispatchRule(
                event="github.pull_request",
                workflow="pr-closed",
                match={"action": "closed"},
            ),
        ]
        reactor = EventReactor(rules=rules, cwd="/tmp")

        # review_requested → suppressed
        event_rr = {
            "type": "github.pull_request",
            "topics": ["github:moda-labs/test"],
            "fields": {"action": "review_requested", "number": 99},
        }
        assert reactor.process(event_rr) == "suppressed"

        # closed → dispatched
        event_closed = {
            "type": "github.pull_request",
            "topics": ["github:moda-labs/test"],
            "fields": {"action": "closed", "number": 100},
        }
        assert reactor.process(event_closed) == "dispatched"
        mock_launch.assert_called_once()


class TestEventReactorFromConfig:
    """EventReactor.from_config builds a reactor from auto_dispatch config."""

    def test_from_config_list(self):
        config = [
            {
                "event": "github.pull_request_review",
                "match": {"review_state": "changes_requested"},
                "workflow": "pr-feedback",
                "cooldown": 900,
            },
            {
                "event": "github.pull_request_review_comment",
                "workflow": "pr-feedback",
            },
        ]
        reactor = EventReactor.from_config(config, cwd="/tmp/project")
        assert len(reactor.rules) == 2
        assert reactor.rules[0].workflow == "pr-feedback"
        assert reactor.rules[0].cooldown == 900
        assert reactor.rules[1].match == {}

    def test_from_config_empty(self):
        reactor = EventReactor.from_config([], cwd="/tmp/project")
        assert len(reactor.rules) == 0

    def test_from_config_default_cooldown(self):
        config = [
            {"event": "github.pull_request_review_comment", "workflow": "pr-feedback"},
        ]
        reactor = EventReactor.from_config(config, cwd="/tmp/project")
        assert reactor.rules[0].cooldown == 1800  # default 30 min

    def test_from_config_suppress_rule(self):
        config = [
            {
                "event": "github.pull_request",
                "match": {"action": "review_requested"},
                "suppress": True,
            },
        ]
        reactor = EventReactor.from_config(config, cwd="/tmp/project")
        assert len(reactor.rules) == 1
        assert reactor.rules[0].suppress is True
        assert reactor.rules[0].workflow == ""


class TestConfigAutoDispatch:
    """Config.load parses auto_dispatch rules from agent.yaml."""

    def test_auto_dispatch_parsed_from_yaml(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(
            "agent: test\n"
            "auto_dispatch:\n"
            "  - event: github.pull_request_review\n"
            "    match:\n"
            "      review_state: changes_requested\n"
            "    workflow: pr-feedback\n"
            "    cooldown: 900\n"
            "  - event: github.pull_request_review_comment\n"
            "    workflow: pr-feedback\n"
        )
        from modastack.config import Config
        cfg = Config.load(tmp_path)
        assert len(cfg.auto_dispatch) == 2
        assert cfg.auto_dispatch[0]["event"] == "github.pull_request_review"
        assert cfg.auto_dispatch[0]["workflow"] == "pr-feedback"

    def test_auto_dispatch_defaults_to_empty(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text("agent: test\n")
        from modastack.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.auto_dispatch == []

    def test_auto_dispatch_missing_config(self, tmp_path):
        """No agent.yaml → empty auto_dispatch."""
        from modastack.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.auto_dispatch == []
