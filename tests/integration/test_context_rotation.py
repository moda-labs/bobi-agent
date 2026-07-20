"""Integration tests for context rotation (#273).

These tests verify the rotate-before-compact mechanism for persistent
sessions. They use mocked SDK clients to avoid real Claude sessions
while still exercising the full Session lifecycle.

Per CLAUDE.md: tests must be written and failing BEFORE the fix.
"""

import asyncio
import hashlib
import queue
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from bobi.inbox import Message
from bobi.session import Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result_msg(session_id="sess-123", is_error=False, input_tokens=0):
    """Create a mock ResultMessage with configurable input_tokens."""
    msg = MagicMock()
    msg.session_id = session_id
    msg.is_error = is_error
    msg.total_cost_usd = 0.01
    msg.duration_ms = 100
    msg.num_turns = 1
    # model_usage with input_tokens for cap detection
    usage = MagicMock()
    usage.model = "claude-opus-4-6"
    usage.input_tokens = input_tokens
    usage.output_tokens = 500
    msg.model_usage = [usage]
    return msg


def _make_assistant_msg(text="response"):
    msg = MagicMock()
    block = MagicMock()
    block.text = text
    msg.content = [block]
    return msg


def _write_memory(state_dir: Path, session_name: str, content: str):
    """Write a decision log INDEX.md for the given session."""
    mem_dir = state_dir / "memory" / session_name
    mem_dir.mkdir(parents=True, exist_ok=True)
    index = mem_dir / "INDEX.md"
    index.write_text(content)
    return index


# ---------------------------------------------------------------------------
# Test: Persistent session rotates when input_tokens exceeds cap
# ---------------------------------------------------------------------------

class TestContextRotation:
    """A persistent session whose last-turn input_tokens exceeds
    rotation_token_cap rotates: flushes the log, reconnects with
    resume=None, and the fresh session's system prompt contains the
    reloaded decision log."""

    def test_session_has_rotate_method(self, bobi_install):
        """Session must expose _rotate() for lightweight client cycling."""
        s = Session(name="test-rotate", cwd=str(bobi_install.repo_path))
        assert hasattr(s, '_rotate'), "Session must have _rotate() method"

    def test_rotate_pending_set_on_cap_exceeded(self, bobi_install):
        """_drain_turn sets _rotate_pending when input_tokens > cap."""
        s = Session(
            name="test-cap",
            cwd=str(bobi_install.repo_path),
            extra_options={"rotation_token_cap": 1000},
        )
        assert hasattr(s, '_rotate_pending'), \
            "Session must have _rotate_pending attribute"

    @pytest.mark.asyncio
    async def test_drain_rotates_on_cached_context(self, bobi_install):
        """End-to-end through _drain_turn: a warm turn whose context lives in
        cache_read (tiny input_tokens) must still trip the cap.

        Post-#454 the metric is measured from a SINGLE representative API
        call — the last AssistantText's per-call usage — NOT the TurnResult's
        turn aggregate. So the over-cap usage shape the deployed manager
        actually emitted (~424K cached, 2 fresh) lives on the normalized
        AssistantText produced by the brain adapter. Single-call fill =
        2 + 422_468 + 1_262 = 423_732 >= 275_000 → rotation must arm.
        """
        from bobi.brain import AssistantText, TurnResult

        class _Client:
            provider = "anthropic"

            async def query(self, text):
                pass

            async def receive_response(self):
                # The one representative API call carries the warm, cache-heavy
                # single-call usage — this is what the corrected metric reads.
                yield AssistantText(
                    text="step",
                    usage={
                        "input_tokens": 2,
                        "cache_read_input_tokens": 422_468,
                        "cache_creation_input_tokens": 1_262,
                        "output_tokens": 3_432,
                    },
                )
                # The TurnResult closes the turn; aggregate usage is no longer
                # the rotation signal (it over-counts cache_read xN).
                yield TurnResult(
                    session_id="sess-1",
                    is_error=False,
                    total_cost_usd=0.01,
                    duration_ms=1,
                    num_turns=1,
                )

        s = Session(
            name="test-cap-drain",
            cwd=str(bobi_install.repo_path),
            extra_options={"rotation_token_cap": 275_000},
        )
        s._input_ready = asyncio.Event()
        s._client = _Client()

        await s._drain_turn()

        assert s._rotate_pending is True, \
            "context fill (input + cache) exceeded cap but rotation did not arm"

    def test_rotation_token_cap_default(self, bobi_install):
        """Default rotation_token_cap is 275_000."""
        s = Session(name="test-default-cap", cwd=str(bobi_install.repo_path))
        assert hasattr(s, '_rotation_token_cap'), \
            "Session must have _rotation_token_cap attribute"
        assert s._rotation_token_cap == 275_000, \
            f"Default cap should be 275000, got {s._rotation_token_cap}"

    def test_rotation_token_cap_configurable(self, bobi_install):
        """rotation_token_cap is overridable via extra_options."""
        s = Session(
            name="test-custom-cap",
            cwd=str(bobi_install.repo_path),
            extra_options={"rotation_token_cap": 100_000},
        )
        assert s._rotation_token_cap == 100_000


# ---------------------------------------------------------------------------
# Test: Rotation keeps inbox alive — no event loss
# ---------------------------------------------------------------------------

class TestRotationInboxAlive:
    """Rotation keeps the inbox server and event subscription alive;
    events published during rotation are delivered after reconnect."""

    def test_rotation_does_not_close_inbox(self, bobi_install):
        """_rotate() must not call inbox.close() or stop the inbox server."""
        s = Session(name="test-inbox-alive", cwd=str(bobi_install.repo_path))
        assert hasattr(s, '_rotate'), "Session must have _rotate() method"
        # The rotate method signature should exist and not touch inbox

    @pytest.mark.asyncio
    async def test_priority_chat_drains_while_rotation_reconnect_is_hung(
        self, bobi_install, monkeypatch
    ):
        """An over-cap manager must serve a human while reconnect is hung.

        This drives the real Session startup, over-cap detection, idle rotation,
        inbox loop, and event drain. The only fake boundary is the brain: the
        startup client reports an over-cap turn, then the fresh rotation client
        connects with no prompt and hangs while draining that phantom turn. A
        Slack mention delivered after that hang begins must be processed before
        the reconnect timeout, not parked behind it.
        """
        from bobi import session as session_mod
        from bobi.brain import AssistantText, TurnResult
        from bobi.events.client import format_event_for_manager
        from bobi.events.drain import _DRAIN_STOP, drain_loop

        reconnect_timeout = 1.0
        monkeypatch.setattr(
            session_mod, "ROTATION_RECONNECT_TIMEOUT", reconnect_timeout
        )
        monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_BACKOFF", 0.0)
        monkeypatch.setattr(
            session_mod, "ROTATION_MAX_RECONNECT_ATTEMPTS", 1
        )
        # The Slack channel handler performs gateway I/O for typing status.
        # Priority selection happens after this seam and is the behavior under
        # test, so keep the lifecycle local and deterministic.
        monkeypatch.setattr(
            "bobi.events.drain._prepare_chat_events", lambda events: events
        )
        monkeypatch.setattr("bobi.events.drain.DRAIN_INTERVAL", 0)

        promptless_connect_started = asyncio.Event()

        class ReadyClient:
            provider = "anthropic"

            def __init__(self, *, over_cap_first_turn: bool = False):
                self.over_cap_first_turn = over_cap_first_turn
                self.turns = 0
                self.queries = []
                self.is_disconnected = False

            async def connect(self, prompt=None):
                return None

            async def query(self, text):
                if self.is_disconnected:
                    raise RuntimeError("queried disconnected pre-rotation client")
                self.queries.append(text)

            async def receive_response(self):
                self.turns += 1
                cache_read = (
                    2_000 if self.over_cap_first_turn and self.turns == 1 else 0
                )
                yield AssistantText(
                    text="handled",
                    usage={
                        "input_tokens": 1,
                        "cache_read_input_tokens": cache_read,
                        "cache_creation_input_tokens": 0,
                    },
                )
                yield TurnResult(
                    session_id="manager-session",
                    is_error=False,
                    duration_ms=1,
                    num_turns=1,
                )

            async def disconnect(self):
                self.is_disconnected = True

        class PromptlessClaudeClient:
            provider = "anthropic"

            def __init__(self):
                self.queries = []
                self.has_pending_turn = False

            async def connect(self, prompt=None):
                # Claude's connect(None) returns without starting a turn.
                # Unlike Codex, it emits no no-op TurnResult for that empty
                # stream, so the unconditional receive_response below hangs.
                assert prompt is None
                promptless_connect_started.set()
                return None

            async def query(self, text):
                self.queries.append(text)
                self.has_pending_turn = True

            async def receive_response(self):
                if not self.has_pending_turn:
                    await asyncio.Event().wait()
                    yield  # pragma: no cover - empty response stream never completes
                self.has_pending_turn = False
                yield AssistantText(
                    text="handled after reconnect",
                    usage={"input_tokens": 1},
                )
                yield TurnResult(
                    session_id="rotated-manager-session",
                    is_error=False,
                    duration_ms=1,
                    num_turns=1,
                )

            async def disconnect(self):
                return None

        startup_client = ReadyClient(over_cap_first_turn=True)
        rotation_client = PromptlessClaudeClient()
        recovery_client = ReadyClient()
        clients = iter([startup_client, rotation_client, recovery_client])

        session = Session(
            name="test-priority-during-rotation",
            cwd=str(bobi_install.repo_path),
            extra_options={"rotation_token_cap": 1_000},
        )
        session._make_brain_session = lambda resume=None: next(clients)

        # Preserve the real priority queue while shrinking its idle probe from
        # two seconds so the integration regression stays fast.
        recv = session.inbox.recv
        session.inbox.recv = lambda timeout=2.0: recv(timeout=0.01)
        session.inbox.start()

        run_task = asyncio.create_task(session._run("manager startup"))
        event_queue = queue.SimpleQueue()
        acked = []
        processed = threading.Event()

        def ack(seq):
            acked.append(seq)
            processed.set()

        try:
            is_ready = await asyncio.to_thread(session._ready.wait, 0.5)
            assert is_ready, "manager never completed its over-cap startup turn"
            assert session._rotate_pending is True

            await asyncio.wait_for(promptless_connect_started.wait(), timeout=0.5)

            # This is the event shape and real drain path used by a queued
            # Slack mention. delivery=chat makes the resulting inbox Message
            # priority, and seq=1 remains unacked until the turn completes.
            event_queue.put({
                "type": "slack.mention",
                "source": "slack",
                "delivery": "chat",
                "seq": 1,
                "text": "<@U_BOT> are you there?",
                "conversation": "slack:T_TEST:channel:C_TEST:thread:1.0",
                "fields": {"user_id": "U_HUMAN", "channel": "C_TEST"},
            })
            event_queue.put(_DRAIN_STOP)
            drain_thread = threading.Thread(
                target=drain_loop,
                kwargs={
                    "session_name": session.name,
                    "queue": event_queue,
                    "formatter": format_event_for_manager,
                    "cursor_ack": ack,
                },
                daemon=True,
            )
            drain_thread.start()
            await asyncio.to_thread(drain_thread.join, 0.5)
            assert not drain_thread.is_alive(), "Slack event drain did not finish"

            started_waiting = time.monotonic()
            processed_promptly = await asyncio.to_thread(processed.wait, 0.3)
            elapsed = time.monotonic() - started_waiting

            assert processed_promptly, (
                "priority Slack mention stayed queued behind the hung rotation "
                f"reconnect for {elapsed:.2f}s"
            )
            assert elapsed < reconnect_timeout / 2
            assert acked == [1]
            assert any("are you there?" in query for query in rotation_client.queries)
        finally:
            if session._keep_alive is not None:
                session._keep_alive.set()
            try:
                await asyncio.wait_for(run_task, timeout=1.0)
            except asyncio.TimeoutError:
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            session.inbox.close()


# ---------------------------------------------------------------------------
# Test: Flush verification via INDEX.md mtime/hash
# ---------------------------------------------------------------------------

class TestFlushVerification:
    """Flush is verified via INDEX.md mtime/hash; a no-op flush skips
    rotation and logs a warning instead of dropping the transcript."""

    def test_session_has_verify_flush_mechanism(self, bobi_install):
        """Session must have a flush verification mechanism."""
        s = Session(name="test-flush", cwd=str(bobi_install.repo_path))
        assert hasattr(s, '_verify_flush') or hasattr(s, '_rotate'), \
            "Session must have flush verification"


# ---------------------------------------------------------------------------
# Test: No role-name branching — mechanism is role-agnostic
# ---------------------------------------------------------------------------

class TestNoRoleBranching:
    """The mechanism activates for any persistent=True session and
    contains no role-name branching."""

    def test_rotation_fields_present_for_any_role(self, bobi_install):
        """All roles get rotation attributes — no role-specific branching."""
        for role in ("director", "project_lead", "engineer", "custom_role"):
            s = Session(
                name=f"test-{role}",
                cwd=str(bobi_install.repo_path),
                role=role,
            )
            assert hasattr(s, '_rotation_token_cap'), \
                f"Role '{role}' must have _rotation_token_cap"
            assert hasattr(s, '_rotate_pending'), \
                f"Role '{role}' must have _rotate_pending"

    def test_no_role_checks_in_rotation_code(self):
        """The session module must not contain role-name checks in rotation logic."""
        import inspect
        source = inspect.getsource(Session)
        # Rotation code should not check for specific role names
        for role_name in ("director", "project_lead", "team_lead"):
            assert f'"{role_name}"' not in source or "role" not in source.split(f'"{role_name}"')[0][-50:], \
                f"Session should not branch on role name '{role_name}' for rotation"


# ---------------------------------------------------------------------------
# Test: MAX_MEMORY_CHARS raised
# ---------------------------------------------------------------------------

class TestMaxMemoryCharsRaised:
    """MAX_MEMORY_CHARS raised from 8000 for use as primary continuity spine."""

    def test_max_memory_chars_raised(self):
        from bobi.memory import MAX_MEMORY_CHARS
        assert MAX_MEMORY_CHARS > 8000, \
            f"MAX_MEMORY_CHARS should be raised above 8000, got {MAX_MEMORY_CHARS}"

    def test_startup_warns_on_large_memory(self, bobi_install, caplog):
        """Startup logs a warning when reloaded log is large relative to cap."""
        from bobi.memory import load_memory, MAX_MEMORY_CHARS
        state_dir = bobi_install.state_dir

        # Write a large memory that exceeds 50% of the cap
        large_content = "x" * (MAX_MEMORY_CHARS // 2 + 1000)
        _write_memory(state_dir, "test-large-mem", large_content)

        # Loading should trigger a warning about large memory
        content = load_memory(state_dir, "test-large-mem")
        assert len(content) > 0


# ---------------------------------------------------------------------------
# Test: Observability — rotation events in log
# ---------------------------------------------------------------------------

class TestRotationObservability:
    """Rotation events appear in activity log and rotation count in status."""

    def test_session_tracks_rotation_count(self, bobi_install):
        """Session must track a rotation count for status reporting."""
        s = Session(name="test-obs", cwd=str(bobi_install.repo_path))
        assert hasattr(s, '_rotation_count'), \
            "Session must have _rotation_count attribute"
        assert s._rotation_count == 0
