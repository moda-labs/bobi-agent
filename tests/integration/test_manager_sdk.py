"""Integration tests for the SDK-based manager session.

These tests drive real Claude Code sessions via ClaudeSDKClient and require the
`claude` CLI. They run in CI as a step of the `integration-claude` job
(self-hosted EC2, gated to nightly / workflow_dispatch / the `ci:claude` label),
alongside the other real-Claude integration tests — so they exercise on a real
schedule without gating every PR on real-session latency.

Keep this file under `tests/integration/`, NOT `tests/` root: the PR unit job
globs `tests/` (minus integration/e2e) and a dev machine with `claude` installed
would run these there, where real-session hangs would flake the unit suite.
"""

import asyncio
import time

import pytest

from .conftest import requires_claude

pytestmark = pytest.mark.claude


async def _safe_disconnect(client):
    """Best-effort disconnect — suppresses RuntimeError from a closed event loop."""
    try:
        await client.disconnect()
    except RuntimeError:
        pass


@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(120)
class TestManagerSDKDirect:
    """Test the manager's ClaudeSDKClient usage directly."""

    async def test_client_connects_and_responds(self):
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
        )
        from bobi.sdk import get_cli_path

        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt="You are a test assistant. Reply concisely.",
        )

        client = ClaudeSDKClient(options)
        try:
            await client.connect("Reply with just: MANAGER_OK")

            got_text = False
            got_result = False

            async for msg in client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and "MANAGER_OK" in block.text:
                            got_text = True
                elif isinstance(msg, ResultMessage):
                    got_result = True
                    assert not msg.is_error
                    assert msg.session_id != ""
                    break
        finally:
            await _safe_disconnect(client)
        assert got_text
        assert got_result

    async def test_multi_turn_conversation(self):
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
        )
        from bobi.sdk import get_cli_path

        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt="You are a test assistant. Reply concisely.",
        )

        client = ClaudeSDKClient(options)
        try:
            await client.connect("Remember the code word: BANANA")

            # Wait for first response
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

            # Second turn -- ask for the code word
            await client.query("What was the code word? Reply with just the word.")

            found_banana = False
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and "BANANA" in block.text.upper():
                            found_banana = True
                elif isinstance(msg, ResultMessage):
                    break
        finally:
            await _safe_disconnect(client)
        assert found_banana, "Manager did not remember the code word across turns"

    async def test_session_resume(self):
        """Test that session_id is returned and can be used for resume."""
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
        )
        from bobi.sdk import get_cli_path

        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt="You are a test assistant.",
        )

        client = ClaudeSDKClient(options)
        try:
            await client.connect("Say hello.")

            session_id = ""
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    session_id = msg.session_id
                    break
        finally:
            await _safe_disconnect(client)
        assert session_id != "", "No session_id returned"
        assert len(session_id) > 8


@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(60)
class TestManagerSessionModule:
    """Test the manager session module's inject/response pattern.

    Uses the SDK directly (same as the module does internally) to avoid
    the 60s+ startup from loading the full manager prompt.
    """

    async def test_inject_and_read_response(self):
        """Simulate the manager's inject -> receive_response cycle."""
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
        )
        from bobi.sdk import get_cli_path

        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt=(
                "You are a manager agent. When you receive events, "
                "decide what to do and reply with your decision."
            ),
        )

        client = ClaudeSDKClient(options)
        try:
            await client.connect("You are online. Say: READY")

            # Wait for startup
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break

            # Simulate event injection (same as manager.inject() does)
            await client.query(
                "Event: github/task.assigned\n"
                "  issue_id: 99\n"
                "  title: Add rate limiting\n"
                "  repo: moda-labs/bettertab\n"
                "Reply with just: ACKNOWLEDGED"
            )

            response_text = ""
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            response_text += block.text
                elif isinstance(msg, ResultMessage):
                    assert not msg.is_error
                    break
        finally:
            await _safe_disconnect(client)
        assert "ACKNOWLEDGED" in response_text
