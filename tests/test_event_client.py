"""Tests for event client — formatting, queue ingestion, and event logging."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from modastack.events.client import (
    format_event_for_manager,
    event_queue,
    _log_event,
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

    def test_internal_data_format(self):
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


class TestLogEvent:
    """_log_event appends to events.jsonl without corrupting existing lines."""

    def test_appends_on_fresh_line_when_file_missing_trailing_newline(self, tmp_path):
        """If events.jsonl doesn't end with a newline (e.g. truncated write),
        _log_event must start a new line so it doesn't merge with the last entry."""
        jsonl = tmp_path / "events.jsonl"
        # Simulate a prior write that lost its trailing newline
        first = {"timestamp": "2026-01-01T00:00:00", "type": "a", "source": "x", "payload": {}}
        jsonl.write_text(json.dumps(first))  # no trailing \n

        with patch("modastack.events.client._state_path", return_value=jsonl):
            _log_event({"type": "b", "source": "y"})

        lines = jsonl.read_text().splitlines()
        assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}: {lines}"
        # Both lines must be valid JSON
        json.loads(lines[0])
        json.loads(lines[1])

    def test_normal_append_no_blank_line(self, tmp_path):
        """When file ends with newline (normal case), no extra blank line inserted."""
        jsonl = tmp_path / "events.jsonl"
        first = {"timestamp": "2026-01-01T00:00:00", "type": "a", "source": "x", "payload": {}}
        jsonl.write_text(json.dumps(first) + "\n")

        with patch("modastack.events.client._state_path", return_value=jsonl):
            _log_event({"type": "b", "source": "y"})

        content = jsonl.read_text()
        lines = content.splitlines()
        assert len(lines) == 2
        assert content.endswith("\n")


class TestEventQueue:

    def test_queue_starts_empty(self):
        while not event_queue.empty():
            event_queue.get_nowait()
        assert event_queue.empty()
