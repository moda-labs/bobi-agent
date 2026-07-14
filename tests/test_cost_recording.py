"""Tests for cost recording on SessionRegistry."""

import json
from pathlib import Path

import pytest


class TestSessionEntryNewFields:
    def test_session_entry_has_cost_fields(self):
        from bobi.sdk import SessionEntry
        entry = SessionEntry(name="test")
        assert entry.model == ""
        assert entry.provider == ""
        assert entry.total_cost_usd == 0.0
        assert entry.model_usage == {}

    def test_session_entry_serializes_cost_fields(self):
        from dataclasses import asdict
        from bobi.sdk import SessionEntry
        entry = SessionEntry(
            name="test", model="claude-sonnet-4-20250514",
            provider="anthropic", total_cost_usd=0.50,
            model_usage={"anthropic:claude-sonnet-4-20250514": {"cost_usd": 0.50}},
        )
        d = asdict(entry)
        assert d["model"] == "claude-sonnet-4-20250514"
        assert d["provider"] == "anthropic"
        assert d["total_cost_usd"] == 0.50

    def test_session_entry_roundtrip_with_new_fields(self):
        """SessionEntry can be deserialized from JSON that includes new fields."""
        from bobi.sdk import SessionEntry
        data = {
            "name": "test",
            "session_id": "",
            "role": "engineer",
            "run_key": "",
            "title": "",
            "phase": "",
            "project": "",
            "cwd": "",
            "status": "running",
            "pid": 0,
            "inbox_port": 0,
            "image_hash": "",
            "model": "claude-sonnet-4-20250514",
            "provider": "anthropic",
            "total_cost_usd": 1.23,
            "model_usage": {"anthropic:claude-sonnet-4-20250514": {"cost_usd": 1.23}},
            "started_at": 1000.0,
            "last_activity": 1000.0,
            "requested_by": {},
        }
        entry = SessionEntry.from_dict(data)
        assert entry.model == "claude-sonnet-4-20250514"
        assert entry.total_cost_usd == 1.23

    def test_session_entry_backward_compat(self):
        """Old state.json without new fields still deserializes."""
        from bobi.sdk import SessionEntry
        # Simulate old format — no model/provider/total_cost_usd/model_usage
        data = {
            "name": "old-session",
            "session_id": "abc",
            "role": "engineer",
            "run_key": "42",
            "title": "fix bug",
            "phase": "implement",
            "project": "myapp",
            "cwd": "/tmp",
            "status": "done",
            "pid": 0,
            "inbox_port": 0,
            "image_hash": "",
            "started_at": 1000.0,
            "last_activity": 1000.0,
            "requested_by": {},
        }
        # This should work — new fields have defaults
        entry = SessionEntry.from_dict(data)
        assert entry.model == ""
        assert entry.total_cost_usd == 0.0

    def test_from_dict_drops_retired_keys(self):
        """from_dict ignores keys no longer in the schema (e.g. inbox_port)
        so state.json written by pre-#268 code still loads after upgrade."""
        from bobi.sdk import SessionEntry
        entry = SessionEntry.from_dict({
            "name": "n", "cwd": "/tmp", "inbox_port": 5555,
            "some_future_field": "x",
        })
        assert entry.name == "n"
        assert not hasattr(entry, "inbox_port")


class TestRecordCost:
    def test_record_cost_accumulates(self, bobi_install):
        from bobi.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(name="cost-test", role="engineer"))

        registry.record_cost("cost-test", 0.10, model="claude-sonnet-4-20250514",
                             provider="anthropic", input_tokens=5000, output_tokens=1000)
        registry.record_cost("cost-test", 0.05, model="claude-sonnet-4-20250514",
                             provider="anthropic", input_tokens=3000, output_tokens=500)

        entry = registry.get("cost-test")
        assert entry is not None
        assert abs(entry.total_cost_usd - 0.15) < 0.001
        assert entry.model == "claude-sonnet-4-20250514"
        assert entry.provider == "anthropic"
        usage = entry.model_usage
        assert "anthropic:claude-sonnet-4-20250514" in usage
        u = usage["anthropic:claude-sonnet-4-20250514"]
        assert abs(u["cost_usd"] - 0.15) < 0.001
        assert u["input_tokens"] == 8000
        assert u["output_tokens"] == 1500

    def test_record_cost_multi_model(self, bobi_install):
        from bobi.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(name="multi-model-test", role="engineer"))

        registry.record_cost("multi-model-test", 0.10,
                             model="claude-sonnet-4-20250514", provider="anthropic",
                             input_tokens=5000, output_tokens=1000)
        registry.record_cost("multi-model-test", 0.04,
                             model="gpt-image-1", provider="openai")

        entry = registry.get("multi-model-test")
        assert abs(entry.total_cost_usd - 0.14) < 0.001
        assert "anthropic:claude-sonnet-4-20250514" in entry.model_usage
        assert "openai:gpt-image-1" in entry.model_usage

    def test_record_cost_nonexistent_session(self, bobi_install):
        """recording cost on a nonexistent session is a silent no-op."""
        from bobi.sdk import get_registry
        registry = get_registry()
        # Should not raise
        registry.record_cost("nonexistent", 0.10, model="test", provider="test")

    def test_record_cost_cached_split(self, bobi_install):
        """The cached subset accumulates, and the key is ALWAYS written (even
        when 0) — its presence marks the entry as post-split, which is what
        licenses the fold-time dollar estimator to price it (#760)."""
        from bobi.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(name="codex-test", role="dev"))

        registry.record_cost("codex-test", 0.0, model="gpt-5.6",
                             provider="openai", input_tokens=1000,
                             output_tokens=50, cached_input_tokens=900)
        registry.record_cost("codex-test", 0.0, model="gpt-5.6",
                             provider="openai", input_tokens=2000,
                             output_tokens=100, cached_input_tokens=1800)

        u = registry.get("codex-test").model_usage["openai:gpt-5.6"]
        assert u["input_tokens"] == 3000
        assert u["cached_input_tokens"] == 2700
        assert u["output_tokens"] == 150

        # Default (no cached kwarg) still writes the marker key.
        registry.register(SessionEntry(name="claude-test", role="dev"))
        registry.record_cost("claude-test", 0.10, model="claude-sonnet-4-20250514",
                             provider="anthropic", input_tokens=5000,
                             output_tokens=1000)
        u = registry.get("claude-test").model_usage[
            "anthropic:claude-sonnet-4-20250514"]
        assert u["cached_input_tokens"] == 0
