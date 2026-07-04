"""Tests for event reactor — deterministic auto-dispatch of workflows on event match."""

import time
from unittest.mock import patch

from bobi.events.reactor import AutoDispatchRule, EventReactor


def _wait_calls(mock, n, timeout=2.0):
    """Wait for an async-dispatched launch to land.

    _dispatch offloads launch_agent to a daemon thread (so the drain loop
    never blocks on the concurrency semaphore), so the mock is called shortly
    after process() returns rather than synchronously.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock.call_count >= n:
            return
        time.sleep(0.005)


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

    def test_dedup_key_includes_event_id(self):
        """Distinct deliveries on the same PR get distinct keys (issue #326)."""
        rule = AutoDispatchRule(
            event="github.issue_comment",
            workflow="pr-feedback",
        )
        base = {
            "type": "github.issue_comment",
            "topics": ["github:moda-labs/test"],
            "fields": {"number": 42},
        }
        key_a = rule.dedup_key({**base, "id": "delivery-aaa"})
        key_b = rule.dedup_key({**base, "id": "delivery-bbb"})
        assert key_a == "pr-feedback:github:moda-labs/test:42:delivery-aaa"
        assert key_a != key_b

    def test_dedup_key_stable_for_same_id(self):
        """Redelivery of the same event (same id) yields the same key."""
        rule = AutoDispatchRule(
            event="github.issue_comment",
            workflow="pr-feedback",
        )
        event = {
            "type": "github.issue_comment",
            "id": "delivery-aaa",
            "topics": ["github:moda-labs/test"],
            "fields": {"number": 42},
        }
        assert rule.dedup_key(event) == rule.dedup_key(dict(event))

    def test_dedup_key_prefers_stable_comment_id(self):
        """A stable comment_id keys the dedup, NOT the per-delivery id (#411).

        The same comment can reach the reactor more than once — a webhook plus
        a monitor re-poll, each stamped with a *different* per-delivery id. If
        the dedup key used that id, one comment would fan out into several
        engines (the #411 harm: one comment spawned 3 engines → tickets
        #416/#417/#418). Keying on the stable comment id collapses every
        delivery of one comment onto a single key.
        """
        rule = AutoDispatchRule(
            event="github.issue_comment",
            workflow="pr-feedback",
        )
        base = {
            "type": "github.issue_comment",
            "topics": ["github:moda-labs/test"],
            "fields": {"number": 42, "comment_id": 99887766},
        }
        key_a = rule.dedup_key({**base, "id": "delivery-aaa"})
        key_b = rule.dedup_key({**base, "id": "delivery-bbb"})
        # Same comment via two deliveries → one stable key (no fan-out).
        assert key_a == "pr-feedback:github:moda-labs/test:42:comment:99887766"
        assert key_a == key_b

    def test_dedup_key_distinct_comments_differ(self):
        """Distinct comments on one PR still get distinct keys (preserves #326)."""
        rule = AutoDispatchRule(
            event="github.issue_comment",
            workflow="pr-feedback",
        )
        base = {
            "type": "github.issue_comment",
            "topics": ["github:moda-labs/test"],
            "fields": {"number": 42},
        }
        key_a = rule.dedup_key({**base, "fields": {"number": 42, "comment_id": 1}})
        key_b = rule.dedup_key({**base, "fields": {"number": 42, "comment_id": 2}})
        assert key_a != key_b

    def test_dedup_key_prefers_review_id_for_reviews(self):
        """A review event keys on its stable review id."""
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
        )
        base = {
            "type": "github.pull_request_review",
            "topics": ["github:moda-labs/test"],
            "fields": {"number": 42, "review_id": 555},
        }
        key_a = rule.dedup_key({**base, "id": "delivery-aaa"})
        key_b = rule.dedup_key({**base, "id": "delivery-bbb"})
        assert key_a == "pr-feedback:github:moda-labs/test:42:review:555"
        assert key_a == key_b


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

    @patch("bobi.subagent.launch_agent")
    def test_dispatches_on_matching_event(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event = self._make_review_event()

        result = reactor.process(event)

        assert result == "dispatched"
        _wait_calls(mock_launch, 1)
        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs[1]["workflow_name"] == "pr-feedback"
        assert "PR #42" in call_kwargs[1]["task"]

    @patch("bobi.subagent.launch_agent")
    def test_no_dispatch_on_non_matching_event(self, mock_launch):
        reactor = self._make_reactor()
        event = {"type": "github.issues", "fields": {"action": "opened"}}

        result = reactor.process(event)

        assert result is None
        mock_launch.assert_not_called()

    @patch("bobi.subagent.launch_agent")
    def test_no_dispatch_when_review_state_is_approved(self, mock_launch):
        reactor = self._make_reactor()
        event = self._make_review_event(review_state="approved")

        result = reactor.process(event)

        assert result is None
        mock_launch.assert_not_called()

    @patch("bobi.subagent.launch_agent")
    def test_dispatches_on_review_comment(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event = self._make_review_comment_event()

        result = reactor.process(event)

        assert result == "dispatched"
        _wait_calls(mock_launch, 1)
        mock_launch.assert_called_once()

    @patch("bobi.subagent.launch_agent")
    def test_dedup_prevents_rapid_double_dispatch(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event = self._make_review_event()

        assert reactor.process(event) == "dispatched"
        assert reactor.process(event) is None
        _wait_calls(mock_launch, 1)
        assert mock_launch.call_count == 1

    @patch("bobi.subagent.launch_agent")
    def test_dedup_allows_dispatch_after_cooldown(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor(cooldown=0)  # zero cooldown
        event = self._make_review_event()

        assert reactor.process(event) == "dispatched"
        assert reactor.process(event) == "dispatched"
        _wait_calls(mock_launch, 2)
        assert mock_launch.call_count == 2

    @patch("bobi.subagent.launch_agent")
    def test_distinct_comments_same_pr_each_dispatch_within_cooldown(self, mock_launch):
        """Two distinct comments on one PR both dispatch despite the cooldown.

        Regression for issue #326: the cooldown must dedup redelivery of the
        SAME event, not suppress genuinely new comments on the same PR.
        """
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor(cooldown=1800)  # production cooldown
        first = dict(self._make_review_comment_event(), id="delivery-aaa")
        followup = dict(self._make_review_comment_event(), id="delivery-bbb")

        assert reactor.process(first) == "dispatched"
        assert reactor.process(followup) == "dispatched"
        _wait_calls(mock_launch, 2)
        assert mock_launch.call_count == 2

    @patch("bobi.subagent.launch_agent")
    def test_same_comment_redelivered_dedups_within_cooldown(self, mock_launch):
        """Replay of the identical event (same id) dedups to one dispatch."""
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor(cooldown=1800)
        event = dict(self._make_review_comment_event(), id="delivery-aaa")

        assert reactor.process(event) == "dispatched"
        assert reactor.process(dict(event)) is None
        _wait_calls(mock_launch, 1)
        assert mock_launch.call_count == 1

    @patch("bobi.subagent.launch_agent")
    def test_different_prs_dispatch_independently(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event1 = self._make_review_event(number=42)
        event2 = self._make_review_event(number=43)

        assert reactor.process(event1) == "dispatched"
        assert reactor.process(event2) == "dispatched"
        _wait_calls(mock_launch, 2)
        assert mock_launch.call_count == 2

    @patch("bobi.subagent.launch_agent")
    def test_graceful_on_launch_failure_active_session(self, mock_launch):
        """If launch_agent raises because session is already active, handle gracefully."""
        mock_launch.side_effect = RuntimeError("A run is already active")
        reactor = self._make_reactor()
        event = self._make_review_event()

        # Should not raise, should return "dispatched" (handled)
        result = reactor.process(event)
        assert result == "dispatched"

    @patch("bobi.subagent.launch_agent")
    def test_dispatch_does_not_block_on_slow_launch(self, mock_launch):
        """process() must return promptly even when launch_agent blocks (the
        concurrency-semaphore wait can sleep up to ~120s). The launch runs off
        the single drain thread so the event pipeline never stalls."""
        import threading
        release = threading.Event()
        started = threading.Event()

        def slow_launch(**kwargs):
            started.set()
            release.wait(2.0)
            return "wf-x"

        mock_launch.side_effect = slow_launch
        reactor = self._make_reactor()
        event = self._make_review_event()

        t0 = time.time()
        result = reactor.process(event)
        elapsed = time.time() - t0

        assert result == "dispatched"
        assert elapsed < 0.5, f"process() blocked {elapsed:.2f}s on launch"
        assert started.wait(1.0), "launch did not run on a background thread"
        release.set()

    @patch("bobi.subagent.launch_agent")
    def test_task_includes_pr_context(self, mock_launch):
        mock_launch.return_value = "wf-pr-feedback-test-42"
        reactor = self._make_reactor()
        event = self._make_review_event()

        reactor.process(event)

        _wait_calls(mock_launch, 1)
        task = mock_launch.call_args[1]["task"]
        assert "#42" in task
        assert "moda-labs/test" in task

    @patch("bobi.subagent.launch_agent")
    def test_empty_rules_never_dispatches(self, mock_launch):
        reactor = EventReactor(rules=[], cwd="/tmp")
        event = self._make_review_event()

        assert reactor.process(event) is None
        mock_launch.assert_not_called()

    @patch("bobi.subagent.launch_agent")
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

    @patch("bobi.subagent.launch_agent")
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

    @patch("bobi.subagent.launch_agent")
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
        _wait_calls(mock_launch, 1)
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

    def test_from_config_parses_hygiene_flags(self):
        """allow_self_authored opt-in is parsed from config (#411)."""
        config = [
            {
                "event": "github.pull_request",
                "match": {"action": "closed"},
                "workflow": "pr-closed",
                "allow_self_authored": True,
            },
        ]
        reactor = EventReactor.from_config(config, cwd="/tmp/project")
        assert reactor.rules[0].allow_self_authored is True

    def test_from_config_parses_dedup_only(self):
        """dedup_only tracks duplicate deliveries without dispatching."""
        config = [
            {
                "event": "github.issue_comment",
                "workflow": "pr-comment-event-dedup",
                "dedup_only": True,
            },
        ]
        reactor = EventReactor.from_config(config, cwd="/tmp/project")
        assert reactor.rules[0].dedup_only is True
        assert reactor.rules[0].workflow == "pr-comment-event-dedup"

    def test_from_config_hygiene_flags_default(self):
        """Self-author skip is on by default (allow_self_authored defaults
        False)."""
        config = [
            {"event": "github.issue_comment", "match": {"is_pull_request": True},
             "workflow": "pr-feedback"},
        ]
        reactor = EventReactor.from_config(config, cwd="/tmp/project")
        assert reactor.rules[0].allow_self_authored is False

    def test_from_config_threads_self_login(self):
        """The bot's own GitHub login is threaded into the reactor (#411)."""
        config = [
            {"event": "github.issue_comment", "workflow": "pr-feedback"},
        ]
        reactor = EventReactor.from_config(config, cwd="/tmp/project",
                                           self_login="bobi")
        assert reactor.self_login == "bobi"

    @patch("bobi.subagent.launch_agent")
    def test_dedup_only_records_first_and_dedups_redelivery(self, mock_launch):
        rule = AutoDispatchRule(
            event="github.issue_comment",
            workflow="pr-comment-event-dedup",
            match={"is_pull_request": True},
            cooldown=1800,
            dedup_only=True,
            allow_self_authored=True,
        )
        reactor = EventReactor(rules=[rule], cwd="/tmp/project",
                               self_login="bobi")
        first = {
            "type": "github.issue_comment",
            "id": "delivery-1",
            "topics": ["github:moda-labs/test"],
            "fields": {
                "number": 42,
                "is_pull_request": True,
                "comment_id": 123,
                "sender": "bobi",
            },
        }
        redelivery = {
            **first,
            "id": "delivery-2",
        }

        assert reactor.process(first) is None
        assert reactor.process(redelivery) == "deduped"
        mock_launch.assert_not_called()


class TestConfigAutoDispatch:
    """Config.load parses auto_dispatch rules from agent.yaml."""

    def test_auto_dispatch_parsed_from_yaml(self, tmp_path):
        from bobi import paths
        config_dir = paths.package_dir(tmp_path)
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
        from bobi.config import Config
        cfg = Config.load(tmp_path)
        assert len(cfg.auto_dispatch) == 2
        assert cfg.auto_dispatch[0]["event"] == "github.pull_request_review"
        assert cfg.auto_dispatch[0]["workflow"] == "pr-feedback"

    def test_auto_dispatch_defaults_to_empty(self, tmp_path):
        from bobi import paths
        config_dir = paths.package_dir(tmp_path)
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text("agent: test\n")
        from bobi.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.auto_dispatch == []

    def test_auto_dispatch_missing_config(self, tmp_path):
        """No agent.yaml → empty auto_dispatch."""
        from bobi.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.auto_dispatch == []


class TestPrFeedbackDispatchHygiene:
    """Reactor skips spurious pr-feedback dispatches (#411).

    Two failure modes, both observed on held spec PRs:
      (a) the bot's OWN comments (rendered-spec link, "held" notes) fired
          pr-feedback against a PR with no human feedback to act on,
      (c) a single comment fanned out into multiple engines (one comment →
          ≥3 engines → duplicate tickets #416/#417/#418).

    Draft PRs are intentionally still watchable — a held draft is exactly
    where feedback discussion happens, and the self-author skip (a) already
    stops the only loop that matters (per review, underminedsk 2026-06-22).
    """

    def _pr_feedback_rules(self, cooldown=1800):
        # Self-author skip is the default (no allow_self_authored flag), matching
        # the shipped pr-feedback rules after the #411 review (underminedsk).
        return [
            AutoDispatchRule(
                event="github.issue_comment",
                workflow="pr-feedback",
                match={"is_pull_request": True},
                cooldown=cooldown,
            ),
        ]

    def _issue_comment_event(self, *, sender="reviewer1",
                             comment_id=1, number=410, delivery="d1",
                             comment_body="Please add a test."):
        fields = {
            "action": "created",
            "number": number,
            "title": "spec: something",
            "sender": sender,
            "is_pull_request": True,
            "comment_id": comment_id,
            "comment_body": comment_body,
        }
        return {
            "type": "github.issue_comment",
            "source": "github",
            "id": delivery,
            "topics": ["github:moda-labs/bobi"],
            "fields": fields,
        }

    # --- (a) bot-authored comments ---

    @patch("bobi.subagent.launch_agent")
    def test_skips_bot_authored_comment(self, mock_launch):
        reactor = EventReactor(rules=self._pr_feedback_rules(), cwd="/tmp",
                               self_login="bobi")
        event = self._issue_comment_event(sender="bobi")

        result = reactor.process(event)

        assert result is None
        time.sleep(0.05)
        mock_launch.assert_not_called()

    @patch("bobi.subagent.launch_agent")
    def test_dispatches_on_human_comment(self, mock_launch):
        """A genuine human comment still dispatches pr-feedback."""
        mock_launch.return_value = "wf-x"
        reactor = EventReactor(rules=self._pr_feedback_rules(), cwd="/tmp",
                               self_login="bobi")
        event = self._issue_comment_event(sender="zach")

        result = reactor.process(event)

        assert result == "dispatched"
        _wait_calls(mock_launch, 1)
        mock_launch.assert_called_once()

    @patch("bobi.subagent.launch_agent")
    def test_self_author_skip_inactive_without_self_login(self, mock_launch):
        """No resolved bot identity → fail open (don't silently drop)."""
        mock_launch.return_value = "wf-x"
        reactor = EventReactor(rules=self._pr_feedback_rules(), cwd="/tmp",
                               self_login=None)
        event = self._issue_comment_event(sender="bobi")

        assert reactor.process(event) == "dispatched"
        _wait_calls(mock_launch, 1)
        mock_launch.assert_called_once()

    @patch("bobi.subagent.launch_agent")
    def test_allow_self_authored_opt_in_dispatches(self, mock_launch):
        """The escape hatch: a rule with allow_self_authored=True still
        dispatches on the bot's own event (per review, underminedsk 2026-06-22).

        Self-author skip is the default; rules that legitimately react to the
        bot's own action (e.g. pr-closed cleanup on a bot-merged PR) opt back in.
        """
        mock_launch.return_value = "wf-x"
        rule = AutoDispatchRule(
            event="github.pull_request",
            workflow="pr-closed",
            match={"action": "closed"},
            cooldown=60,
            allow_self_authored=True,
        )
        reactor = EventReactor(rules=[rule], cwd="/tmp", self_login="bobi")
        event = {
            "type": "github.pull_request",
            "source": "github",
            "id": "d1",
            "topics": ["github:moda-labs/bobi"],
            "fields": {"number": 99, "action": "closed", "sender": "bobi"},
        }

        assert reactor.process(event) == "dispatched"
        _wait_calls(mock_launch, 1)
        mock_launch.assert_called_once()

    # --- draft PRs stay watchable (reverted draft-skip, underminedsk 2026-06-22) ---

    @patch("bobi.subagent.launch_agent")
    def test_dispatches_on_draft_pr(self, mock_launch):
        """A human comment on a DRAFT PR still dispatches pr-feedback.

        Draft skip was reverted: a held draft is exactly where feedback
        discussion belongs. The self-author skip stops the only loop that
        matters, so draft is no longer treated as un-watchable.
        """
        mock_launch.return_value = "wf-x"
        reactor = EventReactor(rules=self._pr_feedback_rules(), cwd="/tmp",
                               self_login="bobi")
        event = self._issue_comment_event(sender="zach")
        event["fields"]["draft"] = True

        assert reactor.process(event) == "dispatched"
        _wait_calls(mock_launch, 1)
        mock_launch.assert_called_once()

    @patch("bobi.subagent.launch_agent")
    def test_skips_bot_comment_even_on_draft(self, mock_launch):
        """The loop guard still fires on a draft: the bot's own comment on a
        draft PR must NOT dispatch (self-author skip, not draft skip)."""
        reactor = EventReactor(rules=self._pr_feedback_rules(), cwd="/tmp",
                               self_login="bobi")
        event = self._issue_comment_event(sender="bobi")
        event["fields"]["draft"] = True

        assert reactor.process(event) is None
        time.sleep(0.05)
        mock_launch.assert_not_called()

    # --- (c) per-comment dedup (no fan-out) ---

    @patch("bobi.subagent.launch_agent")
    def test_one_comment_dispatches_at_most_one_engine(self, mock_launch):
        """One comment redelivered with different per-delivery ids → one engine.

        Reproduces the #411 fan-out: the same comment arriving twice (webhook +
        monitor re-poll) must NOT spawn two engines.
        """
        mock_launch.return_value = "wf-x"
        reactor = EventReactor(rules=self._pr_feedback_rules(), cwd="/tmp",
                               self_login="bobi")
        first = self._issue_comment_event(sender="zach", comment_id=777,
                                          delivery="delivery-aaa")
        second = self._issue_comment_event(sender="zach", comment_id=777,
                                           delivery="delivery-bbb")

        assert reactor.process(first) == "dispatched"
        assert reactor.process(second) is None
        _wait_calls(mock_launch, 1)
        assert mock_launch.call_count == 1

    @patch("bobi.subagent.launch_agent")
    def test_distinct_comments_dispatch_independently(self, mock_launch):
        """Two genuinely different comments each dispatch (preserves #326)."""
        mock_launch.return_value = "wf-x"
        reactor = EventReactor(rules=self._pr_feedback_rules(), cwd="/tmp",
                               self_login="bobi")
        first = self._issue_comment_event(sender="zach", comment_id=1,
                                          delivery="delivery-aaa")
        second = self._issue_comment_event(sender="zach", comment_id=2,
                                           delivery="delivery-bbb")

        assert reactor.process(first) == "dispatched"
        assert reactor.process(second) == "dispatched"
        _wait_calls(mock_launch, 2)
        assert mock_launch.call_count == 2

    # --- (c) deterministic run_key → cross-process fan-out guard ---

    @patch("bobi.subagent.launch_agent")
    def test_dispatch_uses_deterministic_run_key_for_comment(self, mock_launch):
        """The launch gets a run_key derived from the stable comment id (#411).

        The in-memory dedup dict only guards a *single* reactor process; the
        real #416/#417/#418 fan-out came from concurrent sessions, each with an
        empty dict. A deterministic run_key makes ``make_session_name``
        identical for two dispatches of the same comment, so launch_agent's
        persisted "A run is already active" guard rejects the duplicate even
        across processes.
        """
        mock_launch.return_value = "wf-x"
        reactor = EventReactor(rules=self._pr_feedback_rules(), cwd="/tmp",
                               self_login="bobi")
        event = self._issue_comment_event(sender="zach", comment_id=777,
                                          number=410)

        reactor.process(event)

        _wait_calls(mock_launch, 1)
        assert mock_launch.call_args[1]["run_key"] == "410-comment-777"

    @patch("bobi.subagent.launch_agent")
    def test_dispatch_run_key_uses_review_id(self, mock_launch):
        """A review event's run_key derives from its stable review id."""
        mock_launch.return_value = "wf-x"
        rule = AutoDispatchRule(
            event="github.pull_request_review",
            workflow="pr-feedback",
            match={"review_state": "changes_requested"},
        )
        reactor = EventReactor(rules=[rule], cwd="/tmp", self_login="bobi")
        event = {
            "type": "github.pull_request_review",
            "source": "github",
            "id": "d1",
            "topics": ["github:moda-labs/bobi"],
            "fields": {
                "number": 7, "sender": "zach",
                "review_state": "changes_requested",
                "review_id": 4242,
            },
        }

        reactor.process(event)

        _wait_calls(mock_launch, 1)
        assert mock_launch.call_args[1]["run_key"] == "7-review-4242"
