"""Integration tests for the unified messaging infrastructure.

These tests start real Claude Code sessions via the Session class
and verify that messages can be delivered in both blocking and
non-blocking modes. They require the claude CLI to be installed.
"""

import time

import pytest

from tests.integration.conftest import requires_claude

pytestmark = pytest.mark.claude


@requires_claude
class TestSessionMessaging:
    """Test messaging between live Claude Code sessions."""

    def test_nonblocking_message(self, bobi_env):
        """Send a non-blocking message to a session."""
        from bobi.session import Session
        from bobi.inbox import deliver

        session = Session("test-nb-msg", cwd=str(bobi_env.project_path))
        try:
            assert session.start("You are a test agent. Respond briefly to any message.")

            ok, _ = deliver("test-nb-msg", "Say hello.", wait=False)
            assert ok

            time.sleep(10)
            assert session.is_alive()
        finally:
            session.stop()

    def test_blocking_message(self, bobi_env):
        """Send a blocking message and get the response."""
        from bobi.session import Session
        from bobi.inbox import deliver

        session = Session("test-block-msg", cwd=str(bobi_env.project_path))
        try:
            assert session.start(
                "You are a test agent. When asked a math question, "
                "respond with ONLY the numeric answer, nothing else."
            )

            ok, response = deliver(
                "test-block-msg", "What is 2+2?", wait=True, timeout=30,
            )
            assert ok
            assert "4" in response
        finally:
            session.stop()

    def test_multiple_messages(self, bobi_env):
        """Send several blocking messages in sequence."""
        from bobi.session import Session
        from bobi.inbox import deliver

        session = Session("test-multi-msg", cwd=str(bobi_env.project_path))
        try:
            assert session.start("You are a test agent. Respond briefly to any message.")

            for i in range(3):
                ok, response = deliver(
                    "test-multi-msg",
                    f"Say OK-{i}",
                    wait=True,
                    timeout=30,
                )
                assert ok, f"Failed on message {i}: {response}"
                assert response, f"Empty response on message {i}"
        finally:
            session.stop()

    def test_session_to_session(self, bobi_env):
        """One session messages another session."""
        from bobi.session import Session
        from bobi.inbox import deliver

        agent_a = Session("test-agent-a", cwd=str(bobi_env.project_path))
        agent_b = Session("test-agent-b", cwd=str(bobi_env.project_path))

        try:
            assert agent_a.start(
                "You are agent A. You will receive instructions."
            )
            assert agent_b.start(
                "You are agent B. When asked a question, "
                "respond with ONLY 'PONG', nothing else."
            )

            ok, response = deliver(
                "test-agent-b",
                "PING",
                sender="test-agent-a",
                wait=True,
                timeout=30,
            )
            assert ok
            assert "PONG" in response.upper()
        finally:
            agent_a.stop()
            agent_b.stop()

    def test_blocking_timeout(self, bobi_env):
        """Blocking deliver times out when session is stopped."""
        from bobi.session import Session
        from bobi.inbox import deliver

        session = Session("test-timeout-msg", cwd=str(bobi_env.project_path))
        try:
            assert session.start("You are a test agent.")

            session.stop()
            time.sleep(1)

            ok, response = deliver(
                "test-timeout-msg", "hello?", wait=True, timeout=3,
            )
            assert not ok
        finally:
            session.stop()

    def test_on_response_callback(self, bobi_env):
        """The on_response callback fires for every response."""
        from bobi.session import Session
        from bobi.inbox import deliver

        responses = []
        session = Session(
            "test-callback-msg",
            cwd=str(bobi_env.project_path),
            on_response=lambda text: responses.append(text),
        )
        try:
            assert session.start("You are a test agent. Respond briefly.")

            ok, _ = deliver(
                "test-callback-msg", "Say OK.", wait=True, timeout=30,
            )
            assert ok
            assert len(responses) >= 1
        finally:
            session.stop()
