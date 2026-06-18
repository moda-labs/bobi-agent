"""Integration tests for session lifecycle.

Exercises Session construction, state transitions, inbox wiring, and
registry tracking — all without requiring the claude CLI. These tests
use the real Session class but mock the SDK client layer so they run
in integration-fast.
"""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_session(name, project_path, role="engineer"):
    """Create a Session with the given name, wired to a real project path."""
    from modastack.session import Session
    return Session(
        name=name,
        cwd=str(project_path),
        role=role,
        system_prompt={"type": "preset", "preset": "claude_code"},
    )


class TestSessionConstruction:
    """Session object creation and initial state."""

    def test_initial_state_is_stopped(self, modastack_env):
        session = _make_session("test-init", modastack_env.project_path)
        assert session.detect_state() == "stopped"
        assert session.is_alive() is False

    def test_inbox_created(self, modastack_env):
        session = _make_session("test-inbox", modastack_env.project_path)
        assert session.inbox is not None
        assert session.inbox.session_name == "test-inbox"

    def test_rotation_defaults(self, modastack_env):
        from modastack.session import DEFAULT_ROTATION_TOKEN_CAP
        session = _make_session("test-rot", modastack_env.project_path)
        assert session._rotation_token_cap == DEFAULT_ROTATION_TOKEN_CAP
        assert session._rotate_pending is False
        assert session._rotation_count == 0

    def test_custom_rotation_cap(self, modastack_env):
        from modastack.session import Session
        session = Session(
            name="test-cap",
            cwd=str(modastack_env.project_path),
            extra_options={"rotation_token_cap": 100_000},
        )
        assert session._rotation_token_cap == 100_000


class TestStateTransitions:
    """_set_state and input_ready event signaling."""

    def test_waiting_input_signals_event(self, modastack_env):
        import asyncio
        session = _make_session("test-signal", modastack_env.project_path)
        loop = asyncio.new_event_loop()
        session._input_ready = asyncio.Event()

        # Simulate state transition
        session._set_state("waiting_input")

        assert session._input_ready.is_set()
        assert session.detect_state() == "waiting_input"
        loop.close()

    def test_error_signals_event(self, modastack_env):
        import asyncio
        session = _make_session("test-error", modastack_env.project_path)
        loop = asyncio.new_event_loop()
        session._input_ready = asyncio.Event()

        session._set_state("error")

        assert session._input_ready.is_set()
        assert session.detect_state() == "error"
        loop.close()

    def test_working_does_not_signal(self, modastack_env):
        import asyncio
        session = _make_session("test-working", modastack_env.project_path)
        loop = asyncio.new_event_loop()
        session._input_ready = asyncio.Event()

        session._set_state("working")

        assert not session._input_ready.is_set()
        assert session.detect_state() == "working"
        loop.close()


class TestRegistryTracking:
    """Session registers itself in the SessionRegistry on start."""

    def test_registry_entry_fields(self, modastack_env):
        """SessionEntry has expected fields after construction."""
        from modastack.sdk import SessionEntry
        entry = SessionEntry(
            name="test-entry",
            session_id="",
            role="engineer",
            cwd=str(modastack_env.project_path),
            status="starting",
            pid=12345,
        )
        assert entry.name == "test-entry"
        assert entry.role == "engineer"
        assert entry.status == "starting"

    def test_registry_register_and_lookup(self, modastack_env):
        """Register a session and find it by name."""
        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()

        entry = SessionEntry(
            name="integ-lookup",
            session_id="sid-1",
            role="engineer",
            cwd=str(modastack_env.project_path),
            status="idle",
            pid=12345,
        )
        registry.register(entry)

        found = registry.get("integ-lookup")
        assert found is not None
        assert found.session_id == "sid-1"

        # Cleanup
        registry.mark_done("integ-lookup")

    def test_registry_update_status(self, modastack_env):
        """Update a session's status in the registry."""
        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()

        entry = SessionEntry(
            name="integ-update",
            session_id="",
            role="engineer",
            cwd=str(modastack_env.project_path),
            status="starting",
            pid=12345,
        )
        registry.register(entry)
        registry.update("integ-update", status="idle")

        found = registry.get("integ-update")
        assert found.status == "idle"

        registry.mark_done("integ-update")
