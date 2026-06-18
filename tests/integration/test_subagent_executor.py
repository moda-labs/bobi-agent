"""Integration tests for the subagent executor module.

Exercises prompt building, session naming, lifecycle event emission,
and requires gating — all without requiring the claude CLI. These test
the mechanics of the executor, not the Claude session itself.
"""

import json
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml


class TestBuildPrompt:
    """_build_prompt assembles a phase prompt with issue and handoff path."""

    def test_includes_phase_and_issue(self, modastack_env):
        from modastack.subagent import _build_prompt
        prompt = _build_prompt("pickup", "TEST-42")
        assert "Phase: pickup" in prompt
        assert "#TEST-42" in prompt

    def test_includes_context(self, modastack_env):
        from modastack.subagent import _build_prompt
        prompt = _build_prompt("implement", "X-1", context="Build the widget")
        assert "Build the widget" in prompt

    def test_handoff_path_in_prompt(self, modastack_env):
        from modastack.subagent import _build_prompt
        prompt = _build_prompt("pickup", "BET-10")
        assert "handoff" in prompt.lower()
        assert "BET-10" in prompt


class TestSessionNaming:
    """_session_name produces deterministic, convention-following names."""

    def test_default_prefix(self):
        from modastack.subagent import _session_name
        name = _session_name("BET-10")
        assert name == "agent-bet-10"

    def test_role_prefix(self):
        from modastack.subagent import _session_name
        name = _session_name("BET-10", role="engineer")
        assert name == "engineer-bet-10"

    def test_phase_suffix(self):
        from modastack.subagent import _session_name
        name = _session_name("BET-10", role="engineer", phase="pickup")
        assert name == "engineer-bet-10-pickup"

    def test_different_issues_different_names(self):
        from modastack.subagent import _session_name
        a = _session_name("BET-10", role="engineer", phase="pickup")
        b = _session_name("BET-11", role="engineer", phase="pickup")
        assert a != b


class TestLifecycleEvents:
    """_emit_lifecycle_event posts to the event bus."""

    def test_emit_calls_post_event(self, modastack_env):
        from modastack.subagent import _emit_lifecycle_event

        with patch("modastack.events.publish.post_event") as mock_post:
            mock_post.return_value = True
            _emit_lifecycle_event(
                "agent.started",
                {"session": "test-sess", "run_key": "X-1"},
                blocking=True,
                timeout=2,
            )
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert call_args[0][0] == "agent.started"

    def test_summarize_output(self):
        from modastack.subagent import _summarize_output
        text = "\n".join(f"line {i}" for i in range(20))
        summary = _summarize_output(text)
        # Default: last 6 non-empty lines
        assert "line 19" in summary
        assert "line 14" in summary


class TestRequiresGating:
    """check_requires validates host dependencies before launch."""

    def test_passing_requires(self, tmp_path):
        """All passing checks return success."""
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            requires:
              - name: echo
                check: "echo ok"
                why: "test"
                fix: "n/a"
        """))

        from modastack.config import Config
        cfg = Config.load(tmp_path)
        from modastack.config import run_requires_checks
        results = run_requires_checks(cfg.requires)

        assert all(passed for _, passed, _ in results)

    def test_failing_requires_blocks(self, tmp_path):
        """A failing check is reported as failed."""
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            requires:
              - name: bad-dep
                check: "exit 1"
                why: "test"
                fix: "install bad-dep"
        """))

        from modastack.config import Config
        cfg = Config.load(tmp_path)
        from modastack.config import run_requires_checks
        results = run_requires_checks(cfg.requires)

        assert not results[0][1]  # failed


class TestAgentResult:
    """AgentResult dataclass fields."""

    def test_fields(self):
        from modastack.subagent import AgentResult
        r = AgentResult(
            session_id="sid",
            run_key="X-1",
            phase="pickup",
            success=True,
            duration_ms=1000,
            total_cost_usd=0.05,
            num_turns=3,
        )
        assert r.success is True
        assert r.phase == "pickup"
        assert r.total_cost_usd == 0.05
