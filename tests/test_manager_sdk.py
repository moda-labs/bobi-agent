"""Integration tests for the SDK-based manager session.

These tests drive real Claude Code sessions via ClaudeSDKClient.
Requires the `claude` CLI to be installed.
"""

import asyncio
import shutil
import time

import pytest

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)


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
        from modastack.sdk import get_cli_path

        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt="You are a test assistant. Reply concisely.",
        )

        client = ClaudeSDKClient(options)
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

        await client.disconnect()
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
        from modastack.sdk import get_cli_path

        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt="You are a test assistant. Reply concisely.",
        )

        client = ClaudeSDKClient(options)
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

        await client.disconnect()
        assert found_banana, "Manager did not remember the code word across turns"

    async def test_session_resume(self):
        """Test that session_id is returned and can be used for resume."""
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
        )
        from modastack.sdk import get_cli_path

        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt="You are a test assistant.",
        )

        client = ClaudeSDKClient(options)
        await client.connect("Say hello.")

        session_id = ""
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                session_id = msg.session_id
                break

        await client.disconnect()
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
        from modastack.sdk import get_cli_path

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

        await client.disconnect()
        assert "ACKNOWLEDGED" in response_text
