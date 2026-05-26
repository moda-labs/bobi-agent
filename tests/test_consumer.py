"""Tests for event consumer — write_events_file and log_batch."""

import json
import time

from modastack.manager.events.consumer import _write_events_file, _log_batch


class TestWriteEventsFile:

    def test_writes_event_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.events.consumer.PENDING_EVENTS_PATH", tmp_path / "pending.md")

        events = [
            {
                "type": "task.created",
                "source": "linear",
                "timestamp": "2025-01-01T00:00:00",
                "data": {
                    "issue_id": "BET-42",
                    "title": "Add rate limiting",
                    "state": "Todo",
                    "labels": ["agent", "backend"],
                },
            }
        ]
        _write_events_file(events)

        content = (tmp_path / "pending.md").read_text()
        assert "1 new events" in content
        assert "## linear/task.created" in content
        assert "BET-42" in content
        assert "Todo" in content
        assert "agent, backend" in content

    def test_truncates_long_detail(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.events.consumer.PENDING_EVENTS_PATH", tmp_path / "pending.md")

        events = [
            {
                "type": "slack.message",
                "source": "slack",
                "timestamp": "2025-01-01T00:00:00",
                "data": {"text": "x" * 500, "from": "Alice", "channel_id": "C123"},
            }
        ]
        _write_events_file(events)

        content = (tmp_path / "pending.md").read_text()
        assert "..." in content
        # Detail line should be truncated
        detail_line = [l for l in content.splitlines() if "detail:" in l][0]
        assert len(detail_line) < 400

    def test_multiple_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.events.consumer.PENDING_EVENTS_PATH", tmp_path / "pending.md")

        events = [
            {"type": "a", "source": "s1", "data": {"issue_id": "X-1"}},
            {"type": "b", "source": "s2", "data": {"title": "Thing"}},
        ]
        _write_events_file(events)

        content = (tmp_path / "pending.md").read_text()
        assert "2 new events" in content
        assert "## s1/a" in content
        assert "## s2/b" in content

    def test_all_optional_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.events.consumer.PENDING_EVENTS_PATH", tmp_path / "pending.md")

        events = [
            {
                "type": "test",
                "source": "src",
                "data": {
                    "issue_id": "T-1",
                    "task_id": "tid",
                    "from": "Bob",
                    "channel_id": "C1",
                    "repo": "org/repo",
                    "state": "In Progress",
                    "labels": ["a", "b"],
                    "phase": "spec",
                    "spec_pr": "https://pr/1",
                    "pr_url": "https://pr/2",
                    "current_version": "1.0",
                    "new_version": "2.0",
                    "changelog": "stuff changed",
                    "text": "hello",
                },
            }
        ]
        _write_events_file(events)

        content = (tmp_path / "pending.md").read_text()
        for field in ["T-1", "tid", "Bob", "C1", "org/repo", "In Progress",
                       "a, b", "spec", "https://pr/1", "https://pr/2",
                       "1.0", "2.0", "stuff changed", "hello"]:
            assert field in content


class TestLogBatch:

    def test_logs_batch_to_decisions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.events.consumer.DECISIONS_LOG", tmp_path / "decisions.jsonl")

        events = [
            {"type": "task.created", "source": "linear"},
            {"type": "slack.message", "source": "slack"},
        ]
        _log_batch(events)

        content = (tmp_path / "decisions.jsonl").read_text().strip()
        entry = json.loads(content)
        assert entry["events"] == 2
        assert set(entry["event_types"]) == {"task.created", "slack.message"}
        assert "timestamp" in entry

    def test_appends_multiple_batches(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.events.consumer.DECISIONS_LOG", tmp_path / "decisions.jsonl")

        _log_batch([{"type": "a", "source": "s"}])
        _log_batch([{"type": "b", "source": "s"}])

        lines = (tmp_path / "decisions.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
