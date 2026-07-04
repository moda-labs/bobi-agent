"""Tests for event client — v2 formatting and queue ingestion."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from bobi.events.client import (
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

    def test_conversation_ref_renders_for_bobi_reply(self):
        """#618: the channel-agnostic reply address must reach the agent."""
        event = {
            "v": 2, "source": "slack", "type": "slack.dm",
            "topics": ["slack:T123"],
            "delivery": "chat",
            "text": "hello",
            "conversation": "slack:T123:dm:D456:thread:123.456",
            "fields": {"user_id": "U123", "channel": "D456"},
            "payload": {},
        }
        text = format_event_for_manager(event)
        assert "conversation: slack:T123:dm:D456:thread:123.456" in text

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

    def test_v2_pr_review_event_renders_review_state(self):
        """pull_request_review events include review_state so the lead
        can distinguish changes_requested from approved/commented."""
        event = {
            "v": 2, "source": "github", "type": "github.pull_request_review",
            "topics": ["github:moda-labs/test"],
            "delivery": "bulk",
            "text": "[moda-labs/test] submitted PR #10 Fix bug (changes_requested)",
            "fields": {
                "action": "submitted", "number": 10, "title": "Fix bug",
                "state": "open", "sender": "reviewer1",
                "review_state": "changes_requested",
                "review_body": "Please fix the null check on line 42.",
            },
            "payload": {},
        }
        text = format_event_for_manager(event)
        assert "Event: github/github.pull_request_review" in text
        assert "review_state: changes_requested" in text
        assert "review_body: Please fix the null check" in text

    def test_v2_pr_review_comment_event_renders_comment_fields(self):
        """pull_request_review_comment events include comment body and path."""
        event = {
            "v": 2, "source": "github",
            "type": "github.pull_request_review_comment",
            "topics": ["github:moda-labs/test"],
            "delivery": "bulk",
            "text": "[moda-labs/test] created PR #10 Fix bug",
            "fields": {
                "action": "created", "number": 10, "title": "Fix bug",
                "state": "open", "sender": "reviewer1",
                "comment_body": "This should use a guard clause.",
                "comment_path": "src/handler.ts",
            },
            "payload": {},
        }
        text = format_event_for_manager(event)
        assert "Event: github/github.pull_request_review_comment" in text
        assert "comment_body: This should use a guard clause." in text
        assert "comment_path: src/handler.ts" in text

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
    """_log_event appends to per-session event files without corrupting existing lines."""

    def test_appends_on_fresh_line_when_file_missing_trailing_newline(self, tmp_path):
        """If the event file doesn't end with a newline (e.g. truncated write),
        _log_event must start a new line so it doesn't merge with the last entry."""
        jsonl = tmp_path / "events-default.jsonl"
        # Simulate a prior write that lost its trailing newline
        first = {"timestamp": "2026-01-01T00:00:00", "type": "a", "source": "x", "payload": {}}
        jsonl.write_text(json.dumps(first))  # no trailing \n

        with patch("bobi.events.client._state_path",
                   side_effect=lambda name: tmp_path / name):
            _log_event({"type": "b", "source": "y"})

        lines = jsonl.read_text().splitlines()
        assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}: {lines}"
        # Both lines must be valid JSON
        json.loads(lines[0])
        json.loads(lines[1])

    def test_normal_append_no_blank_line(self, tmp_path):
        """When file ends with newline (normal case), no extra blank line inserted."""
        jsonl = tmp_path / "events-default.jsonl"
        first = {"timestamp": "2026-01-01T00:00:00", "type": "a", "source": "x", "payload": {}}
        jsonl.write_text(json.dumps(first) + "\n")

        with patch("bobi.events.client._state_path",
                   side_effect=lambda name: tmp_path / name):
            _log_event({"type": "b", "source": "y"})

        content = jsonl.read_text()
        lines = content.splitlines()
        assert len(lines) == 2
        assert content.endswith("\n")

    def test_per_session_writes_to_session_file(self, tmp_path):
        """When session_id is provided, _log_event writes to events-<session>.jsonl."""
        with patch("bobi.events.client._state_path",
                   side_effect=lambda name: tmp_path / name):
            _log_event({"type": "push", "source": "github", "seq": 5}, session_id="lead-abc")

        session_file = tmp_path / "events-lead-abc.jsonl"
        assert session_file.exists()
        entry = json.loads(session_file.read_text().strip())
        assert entry["type"] == "push"
        assert entry["seq"] == 5

    def test_per_session_includes_seq_and_deployment(self, tmp_path):
        """Per-session log entries include seq and deployment_id for dedup."""
        with patch("bobi.events.client._state_path",
                   side_effect=lambda name: tmp_path / name):
            _log_event({"type": "a", "source": "x", "seq": 10, "deployment_id": "dep-1"},
                       session_id="sess1")

        session_file = tmp_path / "events-sess1.jsonl"
        entry = json.loads(session_file.read_text().strip())
        assert entry["seq"] == 10
        assert entry["deployment_id"] == "dep-1"

    def test_no_session_id_writes_to_default_file(self, tmp_path):
        """Without session_id, _log_event writes to events-default.jsonl."""
        with patch("bobi.events.client._state_path",
                   side_effect=lambda name: tmp_path / name):
            _log_event({"type": "a", "source": "x"})

        assert (tmp_path / "events-default.jsonl").exists()

    def test_two_sessions_write_separate_files(self, tmp_path):
        """Two sessions writing concurrently produce separate files."""
        with patch("bobi.events.client._state_path",
                   side_effect=lambda name: tmp_path / name):
            _log_event({"type": "push", "source": "github", "seq": 1}, session_id="sess-a")
            _log_event({"type": "pr", "source": "github", "seq": 1}, session_id="sess-b")

        file_a = tmp_path / "events-sess-a.jsonl"
        file_b = tmp_path / "events-sess-b.jsonl"
        assert file_a.exists()
        assert file_b.exists()
        entry_a = json.loads(file_a.read_text().strip())
        entry_b = json.loads(file_b.read_text().strip())
        assert entry_a["type"] == "push"
        assert entry_b["type"] == "pr"


class TestAckThrough:
    """EventServerClient.ack_through saves cursor and sends ACK (#278)."""

    def test_ack_through_saves_cursor(self, tmp_path):
        from bobi.events.client import EventServerClient, _load_cursor
        cursor_path = tmp_path / "cursor.json"
        client = EventServerClient(
            server_url="http://localhost:9999",
            deployment_id="dep-1",
            api_key="key-1",
            cursor_path=cursor_path,
        )
        # No WS connected — ack_through should still save cursor.
        client.ack_through(42)
        assert _load_cursor(cursor_path) == 42

    def test_ack_through_sends_ws_ack(self, tmp_path):
        from bobi.events.client import EventServerClient
        cursor_path = tmp_path / "cursor.json"
        client = EventServerClient(
            server_url="http://localhost:9999",
            deployment_id="dep-1",
            api_key="key-1",
            cursor_path=cursor_path,
        )
        sent = []
        client._ws = MagicMock()
        client._ws.send = lambda msg: sent.append(json.loads(msg))
        client.ack_through(10)
        assert len(sent) == 1
        assert sent[0] == {"type": "ack", "seq": 10}

    def test_ack_through_ignores_zero_seq(self, tmp_path):
        from bobi.events.client import EventServerClient, _load_cursor
        cursor_path = tmp_path / "cursor.json"
        client = EventServerClient(
            server_url="http://localhost:9999",
            deployment_id="dep-1",
            api_key="key-1",
            cursor_path=cursor_path,
        )
        client.ack_through(0)
        assert _load_cursor(cursor_path) == 0  # unchanged


class TestRecordDisconnect:
    """_record_disconnect keeps routine CF-cycle reconnects quiet but flags
    genuine flapping. A long-lived connection that CF cycles (hibernation /
    DO eviction) reconnects losslessly via replay — that's 'routine'. A
    connection that never stays up is 'flapping' and must surface."""

    def _client(self, tmp_path):
        from bobi.events.client import EventServerClient
        return EventServerClient(
            server_url="http://localhost:9999",
            deployment_id="dep-1",
            api_key="key-1",
            cursor_path=tmp_path / "cursor.json",
        )

    def test_stable_connection_drop_is_routine(self, tmp_path):
        c = self._client(tmp_path)
        # Up well past the stability threshold, then dropped (CF cycled the DO).
        assert c._record_disconnect(c._STABLE_AFTER_S + 10) == "routine"
        assert c._short_drop_streak == 0

    def test_short_drops_accumulate_then_flag_flapping(self, tmp_path):
        c = self._client(tmp_path)
        results = [c._record_disconnect(1.0) for _ in range(c._FLAP_WARN_STREAK)]
        # Early short drops are quiet reconnects; the streak threshold flips it
        # to 'flapping' so a genuinely unstable connection is not silent.
        assert results[:-1] == ["reconnecting"] * (c._FLAP_WARN_STREAK - 1)
        assert results[-1] == "flapping"

    def test_never_connected_counts_as_short(self, tmp_path):
        c = self._client(tmp_path)
        # uptime None = the connect frame never arrived (server down / refused).
        assert c._record_disconnect(None) == "reconnecting"
        assert c._short_drop_streak == 1

    def test_stable_connection_resets_flap_streak(self, tmp_path):
        c = self._client(tmp_path)
        for _ in range(c._FLAP_WARN_STREAK):
            c._record_disconnect(1.0)
        assert c._short_drop_streak == c._FLAP_WARN_STREAK
        # One good long-lived connection clears the streak.
        assert c._record_disconnect(c._STABLE_AFTER_S + 1) == "routine"
        assert c._short_drop_streak == 0


class TestEventQueue:

    def test_queue_starts_empty(self):
        while not event_queue.empty():
            event_queue.get_nowait()
        assert event_queue.empty()
