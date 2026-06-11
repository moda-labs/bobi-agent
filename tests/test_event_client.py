"""Tests for event client — v2 formatting and queue ingestion."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from modastack.events.client import (
    format_event_for_manager,
    event_queue,
    _log_event,
)


class TestFormatEventForManager:
    """format_event_for_manager renders v2 events (text + fields)."""

    def test_v2_github_event_renders_text_and_fields(self):
        event = {
            "v": 2, "source": "github", "type": "github.issues",
            "topics": ["github:moda-labs/test"],
            "delivery": "bulk",
            "text": "[moda-labs/test] opened issue #42 Bug",
            "fields": {
                "action": "opened", "number": 42, "title": "Bug",
                "state": "open", "sender": "testuser",
            },
            "payload": {
                "action": "opened",
                "issue": {"number": 42, "title": "Bug"},
                "repository": {"full_name": "moda-labs/test"},
            },
        }
        text = format_event_for_manager(event)
        assert "Event: github/github.issues" in text
        assert "[moda-labs/test] opened issue #42 Bug" in text
        assert "action: opened" in text
        assert "number: 42" in text
        assert "sender: testuser" in text

    def test_v2_slack_event_renders_text_and_fields(self):
        event = {
            "v": 2, "source": "slack", "type": "slack.dm",
            "topics": ["slack:T123"],
            "delivery": "chat",
            "text": "hello",
            "fields": {
                "user_id": "U123", "channel": "D456",
                "channel_type": "im", "ts": "123.456",
            },
            "payload": {
                "user_id": "U123", "text": "hello",
                "channel": "D456", "ts": "123.456",
            },
        }
        text = format_event_for_manager(event)
        assert "Event: slack/slack.dm" in text
        assert "hello" in text
        assert "user_id: U123" in text
        assert "channel: D456" in text

    def test_v2_linear_event_renders_text_and_fields(self):
        event = {
            "v": 2, "source": "linear", "type": "linear.Issue.update",
            "topics": ["linear:ENG"],
            "delivery": "bulk",
            "text": "[Linear] update Issue ENG-42 Add caching",
            "fields": {
                "action": "update", "identifier": "ENG-42",
                "title": "Add caching", "state": "In Progress",
            },
            "payload": {
                "data": {"identifier": "ENG-42", "title": "Add caching"},
            },
        }
        text = format_event_for_manager(event)
        assert "Event: linear/linear.Issue.update" in text
        assert "[Linear] update Issue ENG-42 Add caching" in text
        assert "identifier: ENG-42" in text
        assert "state: In Progress" in text

    def test_scalar_fallback_when_no_fields(self):
        """Unknown-source events with no fields render payload scalars."""
        event = {
            "v": 2, "source": "ci", "type": "deploy.complete",
            "topics": ["deploy.complete"],
            "delivery": "bulk",
            "text": "",
            "payload": {"status": "success", "sha": "abc123", "nested": {"skip": True}},
        }
        text = format_event_for_manager(event)
        assert "Event: ci/deploy.complete" in text
        assert "status: success" in text
        assert "sha: abc123" in text
        # Nested objects are not rendered
        assert "skip" not in text

    def test_scalar_fallback_truncates_long_values(self):
        event = {
            "v": 2, "source": "custom", "type": "test",
            "topics": ["test"], "delivery": "bulk", "text": "",
            "payload": {"long_val": "x" * 300},
        }
        text = format_event_for_manager(event)
        assert "long_val: " in text
        assert "..." in text

    def test_scalar_fallback_caps_at_20_entries(self):
        payload = {f"key_{i}": f"val_{i}" for i in range(30)}
        event = {
            "v": 2, "source": "custom", "type": "test",
            "topics": ["test"], "delivery": "bulk", "text": "",
            "payload": payload,
        }
        text = format_event_for_manager(event)
        # Should have header line + at most 20 scalar lines
        lines = [l for l in text.split("\n") if l.strip().startswith("key_")]
        assert len(lines) <= 20

    def test_empty_fields_omitted(self):
        event = {
            "v": 2, "source": "slack", "type": "slack.dm",
            "topics": ["slack:T1"], "delivery": "chat",
            "text": "hi",
            "fields": {"user_id": "U1", "thread_ts": ""},
            "payload": {"text": "hi"},
        }
        text = format_event_for_manager(event)
        assert "thread_ts" not in text

    def test_renders_requested_by_from_data(self):
        event = {
            "v": 2, "type": "agent/session.completed", "source": "engineer",
            "topics": ["agent/session.completed"], "delivery": "bulk",
            "text": "engineer completed session",
            "data": {
                "issue_id": "adhoc-x", "summary": "PR up",
                "requested_by": {"from": "Alice", "user_id": "U0ABC",
                                 "channel": "C0SHARED", "thread_ts": "171.42"},
            },
        }
        text = format_event_for_manager(event)
        assert "requested_by: Alice" in text
        assert "channel C0SHARED" in text

    def test_legacy_event_scalar_fallback(self):
        """v1 events without text/fields get scalar fallback from payload."""
        event = {
            "source": "github", "type": "github.issues",
            "payload": {
                "action": "opened",
            },
        }
        text = format_event_for_manager(event)
        assert "Event: github/github.issues" in text
        assert "action: opened" in text


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
