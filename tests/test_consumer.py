"""Tests for event consumer — append_batch, format_batch, and log_batch."""

import json
import time

from modastack.manager.events.consumer import (
    _append_batch, _format_batch, _log_batch,
    _read_checkpoint, _has_unread_events, _truncate_processed,
)


class TestFormatBatch:

    def test_formats_event_summary(self):
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
        content = _format_batch(1, events)
        assert "<!-- batch:1 -->" in content
        assert "1 events" in content
        assert "## linear/task.created" in content
        assert "BET-42" in content
        assert "Todo" in content
        assert "agent, backend" in content

    def test_truncates_long_detail(self):
        events = [
            {
                "type": "slack.message",
                "source": "slack",
                "timestamp": "2025-01-01T00:00:00",
                "data": {"text": "x" * 500, "from": "Alice", "channel_id": "C123"},
            }
        ]
        content = _format_batch(1, events)
        assert "..." in content
        detail_line = [l for l in content.splitlines() if "detail:" in l][0]
        assert len(detail_line) < 400

    def test_multiple_events(self):
        events = [
            {"type": "a", "source": "s1", "data": {"issue_id": "X-1"}},
            {"type": "b", "source": "s2", "data": {"title": "Thing"}},
        ]
        content = _format_batch(5, events)
        assert "<!-- batch:5 -->" in content
        assert "2 events" in content
        assert "## s1/a" in content
        assert "## s2/b" in content

    def test_all_optional_fields(self):
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
        content = _format_batch(1, events)
        for field in ["T-1", "tid", "Bob", "C1", "org/repo", "In Progress",
                       "a, b", "spec", "https://pr/1", "https://pr/2",
                       "1.0", "2.0", "stuff changed", "hello"]:
            assert field in content


class TestAppendBatch:

    def test_appends_to_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.events.consumer.PENDING_EVENTS_PATH", tmp_path / "pending.md")
        events = [{"type": "a", "source": "s", "data": {"issue_id": "1"}}]

        _append_batch(1, events)
        _append_batch(2, events)

        content = (tmp_path / "pending.md").read_text()
        assert "<!-- batch:1 -->" in content
        assert "<!-- batch:2 -->" in content


class TestCheckpoint:

    def test_read_checkpoint_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.events.consumer.CHECKPOINT_PATH", tmp_path / "cp")
        assert _read_checkpoint() == 0

    def test_read_checkpoint_value(self, tmp_path, monkeypatch):
        cp = tmp_path / "cp"
        cp.write_text("7")
        monkeypatch.setattr("modastack.manager.events.consumer.CHECKPOINT_PATH", cp)
        assert _read_checkpoint() == 7

    def test_has_unread_events(self, tmp_path, monkeypatch):
        events_file = tmp_path / "pending.md"
        monkeypatch.setattr("modastack.manager.events.consumer.PENDING_EVENTS_PATH", events_file)
        monkeypatch.setattr("modastack.manager.events.consumer.CHECKPOINT_PATH", tmp_path / "cp")

        assert not _has_unread_events()

        events_file.write_text("<!-- batch:1 -->\n# Batch 1\n\n<!-- batch:2 -->\n# Batch 2\n")
        assert _has_unread_events()

        (tmp_path / "cp").write_text("2")
        assert not _has_unread_events()

        (tmp_path / "cp").write_text("1")
        assert _has_unread_events()

    def test_truncate_processed(self, tmp_path, monkeypatch):
        events_file = tmp_path / "pending.md"
        cp = tmp_path / "cp"
        monkeypatch.setattr("modastack.manager.events.consumer.PENDING_EVENTS_PATH", events_file)
        monkeypatch.setattr("modastack.manager.events.consumer.CHECKPOINT_PATH", cp)

        events_file.write_text(
            "<!-- batch:1 -->\n# Batch 1\n\n<!-- batch:2 -->\n# Batch 2\n\n<!-- batch:3 -->\n# Batch 3\n"
        )
        cp.write_text("2")

        _truncate_processed()

        content = events_file.read_text()
        assert "<!-- batch:1 -->" not in content
        assert "<!-- batch:2 -->" not in content
        assert "<!-- batch:3 -->" in content

    def test_truncate_all_processed(self, tmp_path, monkeypatch):
        events_file = tmp_path / "pending.md"
        cp = tmp_path / "cp"
        monkeypatch.setattr("modastack.manager.events.consumer.PENDING_EVENTS_PATH", events_file)
        monkeypatch.setattr("modastack.manager.events.consumer.CHECKPOINT_PATH", cp)

        events_file.write_text("<!-- batch:3 -->\n# Batch 3\n")
        cp.write_text("3")

        _truncate_processed()
        assert events_file.read_text().strip() == ""


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
