"""Tests for event client — formatting, filtering, queue ingestion."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from modastack.events.client import (
    format_event_for_manager,
    event_queue,
    _should_filter,
)


class TestFormatEventForManager:
    """format_event_for_manager works with raw server events (type/source/payload)."""

    def test_github_event(self):
        event = {
            "source": "github", "type": "github.issues",
            "repo": "moda-labs/test",
            "payload": {
                "action": "opened",
                "issue": {"number": 42, "title": "Bug"},
                "repository": {"full_name": "moda-labs/test"},
            },
        }
        text = format_event_for_manager(event)
        assert "Event: github/github.issues" in text
        assert "repo: moda-labs/test" in text
        assert "action: opened" in text

    def test_slack_event(self):
        event = {
            "source": "slack", "type": "slack.dm",
            "workspace": "T123", "channel": "D456",
            "payload": {
                "user_id": "U123", "text": "hello",
                "channel": "D456", "ts": "123.456",
            },
        }
        text = format_event_for_manager(event)
        assert "Event: slack/slack.dm" in text
        assert "workspace: T123" in text
        assert "channel: D456" in text
        assert "text: hello" in text
        assert "user_id: U123" in text

    def test_linear_event(self):
        event = {
            "source": "linear", "type": "linear.Issue.update",
            "team_key": "ENG",
            "payload": {
                "data": {"identifier": "ENG-42", "title": "Add caching"},
            },
        }
        text = format_event_for_manager(event)
        assert "Event: linear/linear.Issue.update" in text
        assert "team_key: ENG" in text

    def test_generic_topic_event(self):
        event = {
            "source": "ci", "type": "deploy.complete",
            "repo": "org/repo",
            "payload": {"status": "success", "sha": "abc123"},
        }
        text = format_event_for_manager(event)
        assert "Event: ci/deploy.complete" in text
        assert "repo: org/repo" in text

    def test_empty_fields_omitted(self):
        event = {
            "source": "slack", "type": "slack.dm",
            "payload": {"text": "hi", "thread_ts": ""},
        }
        text = format_event_for_manager(event)
        assert "thread_ts" not in text

    def test_legacy_data_format_still_works(self):
        event = {
            "type": "task.opened", "source": "github",
            "data": {"issue_id": "42", "title": "Bug", "repo": "moda-labs/test"},
        }
        text = format_event_for_manager(event)
        assert "Event: github/task.opened" in text
        assert "issue_id: 42" in text

    def test_renders_requested_by(self):
        event = {
            "type": "engineer/session.completed", "source": "engineer",
            "data": {
                "issue_id": "adhoc-x", "summary": "PR up",
                "requested_by": {"from": "Alice", "user_id": "U0ABC",
                                 "channel": "C0SHARED", "thread_ts": "171.42"},
            },
        }
        text = format_event_for_manager(event)
        assert "requested_by: Alice" in text
        assert "channel C0SHARED" in text


class TestShouldFilter:

    @patch("modastack.sdk.get_project_root", return_value=Path("/tmp/repo"))
    @patch("modastack.config.ProjectConfig.from_file")
    def test_filters_linear_by_project(self, mock_config, mock_root):
        mock_config.return_value = MagicMock(linear_project="MyProject")
        event = {
            "source": "linear", "type": "linear.Issue.update",
            "payload": {
                "data": {"project": {"name": "OtherProject"}},
            },
        }
        assert _should_filter(event) is True

    @patch("modastack.sdk.get_project_root", return_value=Path("/tmp/repo"))
    @patch("modastack.config.ProjectConfig.from_file")
    def test_passes_matching_project(self, mock_config, mock_root):
        mock_config.return_value = MagicMock(linear_project="MyProject")
        event = {
            "source": "linear", "type": "linear.Issue.update",
            "payload": {
                "data": {"project": {"name": "MyProject"}},
            },
        }
        assert _should_filter(event) is False

    def test_passes_non_linear_events(self):
        event = {"source": "github", "type": "github.issues", "payload": {}}
        assert _should_filter(event) is False


class TestEventQueue:

    def test_queue_starts_empty(self):
        while not event_queue.empty():
            event_queue.get_nowait()
        assert event_queue.empty()
