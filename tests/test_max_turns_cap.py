"""Tests for max_turns cap on the check path (MDS-53 Part B, step 3).

Ensures the check budget is capped so a single poll can't balloon into a
200-turn run. _run_agent_supervised accepts max_turns, and run_check_blocking
passes a small cap (~8).
"""

from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import json

import pytest

from modastack.subagent import CHECK_TIMEOUT


class TestMaxTurnsCap:
    """max_turns is threaded from run_check_blocking to ClaudeAgentOptions."""

    def test_run_check_blocking_passes_max_turns(self):
        """run_check_blocking passes max_turns to _run_agent_supervised,
        which threads it to ClaudeAgentOptions."""
        from modastack.subagent import run_check_blocking, CHECK_MAX_TURNS

        captured_options = []

        # Mock the entire execution chain
        async def mock_supervised(prompt, cwd, run_key, phase, timeout,
                                  on_input_needed=None, role="",
                                  max_turns=200):
            # Capture the max_turns that was passed
            captured_options.append(max_turns)
            from modastack.subagent import AgentResult
            return AgentResult(
                session_id="test", run_key=run_key, phase=phase,
                success=True, final_text='{"finding": false}',
            )

        with patch("modastack.subagent._run_agent_supervised", mock_supervised), \
             patch("modastack.subagent.get_registry") as mock_reg:
            mock_reg.return_value = MagicMock()
            result = run_check_blocking("test check", "/tmp/repo")

        assert len(captured_options) == 1
        assert captured_options[0] == CHECK_MAX_TURNS

    def test_check_max_turns_is_small(self):
        """CHECK_MAX_TURNS must be small (<=10) to cap poll cost."""
        from modastack.subagent import CHECK_MAX_TURNS
        assert CHECK_MAX_TURNS <= 10
        assert CHECK_MAX_TURNS >= 1

    def test_run_agent_supervised_accepts_max_turns(self):
        """_run_agent_supervised signature includes max_turns parameter."""
        import inspect
        from modastack.subagent import _run_agent_supervised
        sig = inspect.signature(_run_agent_supervised)
        assert "max_turns" in sig.parameters
        # Default should be the original 200 for backward compat
        assert sig.parameters["max_turns"].default == 200
