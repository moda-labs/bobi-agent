"""Tests for modastack/session.py — skill loading, session listing, state detection.

Tmux-specific tests have been removed. Session listing and state detection
now test the sub-agent/SDK registry path.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock

from modastack.session import (
    load_skill,
    inject_skill,
    list_sessions,
    detect_state,
)


class TestLoadSkill:

    def test_returns_empty_always(self):
        assert load_skill("pickup") == ""
        assert load_skill("nonexistent") == ""


class TestListSessionsSubagents:
    """list_sessions should include running sub-agents from the SDK executor."""

    @patch("modastack.session.subprocess.run")
    def test_includes_running_subagents(self, mock_tmux_run):
        mock_tmux_run.return_value = MagicMock(returncode=1, stdout="")

        from modastack.subagent import RunningAgent, _running
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future, loop=loop)

        _running["bet-42"] = RunningAgent(
            issue_id="BET-42", phase="implement",
            session_id="s1", task=task, cwd="/tmp",
        )
        try:
            result = list_sessions()
            assert "BET-42" in result
        finally:
            task.cancel()
            del _running["bet-42"]
            loop.close()

    @patch("modastack.session.subprocess.run")
    def test_empty_when_no_agents(self, mock_tmux_run):
        mock_tmux_run.return_value = MagicMock(returncode=1, stdout="")

        from modastack.subagent import _running
        _running.clear()
        result = list_sessions()
        assert result == []


class TestDetectStateSubagent:
    """detect_state should check sub-agents via the SDK registry."""

    def test_running_subagent(self):
        from modastack.subagent import RunningAgent, _running
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future, loop=loop)

        _running["bet-10"] = RunningAgent(
            issue_id="BET-10", phase="spec",
            session_id="s1", task=task, cwd="/tmp",
        )
        try:
            state = detect_state("BET-10")
            assert state["state"] == "running"
            assert state["type"] == "subagent"
        finally:
            task.cancel()
            del _running["bet-10"]
            loop.close()

    def test_completed_subagent(self):
        from modastack.subagent import AgentResult, RunningAgent, _running
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        future.set_result(AgentResult(
            session_id="s2", issue_id="BET-11", phase="implement",
            success=True, duration_ms=5000, total_cost_usd=0.10,
        ))
        task = asyncio.ensure_future(future, loop=loop)

        _running["bet-11"] = RunningAgent(
            issue_id="BET-11", phase="implement",
            session_id="s2", task=task, cwd="/tmp",
        )
        try:
            state = detect_state("BET-11")
            assert state["state"] == "completed"
            assert state["type"] == "subagent"
        finally:
            if "bet-11" in _running:
                del _running["bet-11"]
            loop.close()

    def test_failed_subagent(self):
        from modastack.subagent import AgentResult, RunningAgent, _running
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        future.set_result(AgentResult(
            session_id="s3", issue_id="BET-12", phase="implement",
            success=False, error="timeout",
        ))
        task = asyncio.ensure_future(future, loop=loop)

        _running["bet-12"] = RunningAgent(
            issue_id="BET-12", phase="implement",
            session_id="s3", task=task, cwd="/tmp",
        )
        try:
            state = detect_state("BET-12")
            assert state["state"] == "failed"
            assert "timeout" in state["error"]
        finally:
            if "bet-12" in _running:
                del _running["bet-12"]
            loop.close()

    @patch("modastack.session.has_session", return_value=False)
    def test_no_agent_no_session_returns_exited(self, _):
        from modastack.subagent import _running
        _running.pop("bet-99", None)
        state = detect_state("BET-99")
        assert state["state"] == "exited"
