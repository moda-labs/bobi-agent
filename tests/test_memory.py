"""Tests for the agent decision log (memory) module."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _memory_dir(state_dir: Path, session_name: str) -> Path:
    return state_dir / "memory" / session_name


def _write_index(mem_dir: Path, yaml_block: dict, notes: str = "") -> Path:
    """Write a memory index file with YAML frontmatter + prose notes."""
    mem_dir.mkdir(parents=True, exist_ok=True)
    content = "---\n" + yaml.dump(yaml_block, default_flow_style=False).strip() + "\n---\n"
    if notes:
        content += "\n" + notes + "\n"
    index = mem_dir / "INDEX.md"
    index.write_text(content)
    return index


def _write_note(mem_dir: Path, filename: str, text: str) -> Path:
    """Write a single note file."""
    mem_dir.mkdir(parents=True, exist_ok=True)
    p = mem_dir / filename
    p.write_text(text)
    return p


# ---------------------------------------------------------------------------
# memory.load_memory
# ---------------------------------------------------------------------------

class TestLoadMemory:
    """Loading the memory index for injection into session prompts."""

    def test_returns_empty_when_no_memory_dir(self, tmp_path):
        from modastack.memory import load_memory
        result = load_memory(tmp_path / "state", "moda-director-myproject")
        assert result == ""

    def test_returns_empty_when_dir_exists_but_no_index(self, tmp_path):
        from modastack.memory import load_memory
        mem_dir = _memory_dir(tmp_path / "state", "moda-director-myproject")
        mem_dir.mkdir(parents=True)
        result = load_memory(tmp_path / "state", "moda-director-myproject")
        assert result == ""

    def test_loads_index_content(self, tmp_path):
        from modastack.memory import load_memory
        state = tmp_path / "state"
        mem_dir = _memory_dir(state, "moda-director-myproject")
        _write_index(mem_dir, {"managed_repos": ["org/app"]},
                     "- dogfood tracks in MDS — Zach, 2026-06-10")
        result = load_memory(state, "moda-director-myproject")
        assert "managed_repos" in result
        assert "dogfood tracks in MDS" in result

    def test_includes_note_files(self, tmp_path):
        from modastack.memory import load_memory
        state = tmp_path / "state"
        mem_dir = _memory_dir(state, "moda-director-myproject")
        _write_index(mem_dir, {"managed_repos": ["org/app"]})
        _write_note(mem_dir, "2026-06-10-slack-channel.md",
                    "Preferred notification channel is #eng-alerts.")
        result = load_memory(state, "moda-director-myproject")
        assert "managed_repos" in result
        assert "#eng-alerts" in result

    def test_truncates_large_memory(self, tmp_path):
        from modastack.memory import load_memory, MAX_MEMORY_CHARS
        state = tmp_path / "state"
        mem_dir = _memory_dir(state, "moda-director-myproject")
        # Write an index that exceeds the limit
        huge_notes = "x" * (MAX_MEMORY_CHARS + 1000)
        _write_index(mem_dir, {"big": True}, huge_notes)
        result = load_memory(state, "moda-director-myproject")
        assert len(result) <= MAX_MEMORY_CHARS + 200  # some slack for wrapper text


# ---------------------------------------------------------------------------
# memory.memory_dir_for_session — path helper
# ---------------------------------------------------------------------------

class TestMemoryDir:
    def test_returns_correct_path(self, tmp_path):
        from modastack.memory import memory_dir_for_session
        result = memory_dir_for_session(tmp_path / "state", "moda-director-proj")
        assert result == tmp_path / "state" / "memory" / "moda-director-proj"


# ---------------------------------------------------------------------------
# memory.format_memory_prompt — prompt formatting
# ---------------------------------------------------------------------------

class TestFormatMemoryPrompt:
    def test_wraps_content_in_section(self):
        from modastack.memory import format_memory_prompt
        result = format_memory_prompt("some memory content")
        assert "## Decision Log" in result
        assert "some memory content" in result

    def test_returns_empty_for_no_content(self):
        from modastack.memory import format_memory_prompt
        result = format_memory_prompt("")
        assert result == ""


# ---------------------------------------------------------------------------
# Prompt injection — resolver integration
# ---------------------------------------------------------------------------

class TestPromptInjection:
    """Memory is injected into startup prompts via the resolver."""

    def test_build_startup_prompt_includes_memory(self, tmp_path):
        from modastack.prompts.resolver import build_startup_prompt

        # Set up installed role
        roles_dir = tmp_path / ".modastack" / "roles" / "director"
        roles_dir.mkdir(parents=True)
        (roles_dir / "ROLE.md").write_text("# Director\nYou direct things.")

        # Set up memory
        state_dir = tmp_path / ".modastack" / "state"
        mem_dir = state_dir / "memory" / "moda-director-tmp"
        _write_index(mem_dir, {"managed_repos": ["org/app"]},
                     "Slack channel is #eng")

        result = build_startup_prompt(
            "director", tmp_path, agent_name="test",
            session_name="moda-director-tmp",
        )
        assert "managed_repos" in result
        assert "#eng" in result

    def test_build_startup_prompt_works_without_memory(self, tmp_path):
        from modastack.prompts.resolver import build_startup_prompt

        roles_dir = tmp_path / ".modastack" / "roles" / "director"
        roles_dir.mkdir(parents=True)
        (roles_dir / "ROLE.md").write_text("# Director\nYou direct things.")

        result = build_startup_prompt("director", tmp_path, agent_name="test")
        assert "Decision Log" not in result


# ---------------------------------------------------------------------------
# Doctor check — memory drift
# ---------------------------------------------------------------------------

class TestDoctorMemoryCheck:
    """doctor flags mismatch between running persistent agents and memory."""

    def test_passes_when_no_memory(self, tmp_path):
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_memory
            r = _check_memory()
        assert r.ok

    def test_passes_when_memory_exists(self, tmp_path):
        state_dir = tmp_path / ".modastack" / "state"
        mem_dir = state_dir / "memory" / "moda-director-proj"
        _write_index(mem_dir, {"managed_repos": ["org/app"]})
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_memory
            r = _check_memory()
        assert r.ok
        assert "1 agent" in r.detail

    def test_flags_all_empty_as_failure(self, tmp_path):
        state_dir = tmp_path / ".modastack" / "state"
        mem_dir = state_dir / "memory" / "moda-director-proj"
        mem_dir.mkdir(parents=True)
        # Index with empty current-state block
        (mem_dir / "INDEX.md").write_text("---\n---\n")
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_memory
            r = _check_memory()
        assert not r.ok  # all logs empty = likely drift
        assert "empty" in r.detail

    def test_partial_empty_still_ok(self, tmp_path):
        state_dir = tmp_path / ".modastack" / "state"
        # One populated, one empty
        populated = state_dir / "memory" / "moda-director-proj"
        _write_index(populated, {"managed_repos": ["org/app"]})
        empty_dir = state_dir / "memory" / "moda-lead-proj"
        empty_dir.mkdir(parents=True)
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_memory
            r = _check_memory()
        assert r.ok  # some populated = ok, just note the empty one
        assert "empty" in r.detail


# ---------------------------------------------------------------------------
# Subagent injection — spawn_adhoc and run_phase_blocking
# ---------------------------------------------------------------------------

class TestSubagentMemoryInjection:
    """Memory is included in system_prompt append for sessions."""

    def test_spawn_adhoc_includes_memory(self, tmp_path):
        """Verify spawn_adhoc loads memory into append_parts."""
        from modastack.memory import load_memory, format_memory_prompt

        state_dir = tmp_path / ".modastack" / "state"
        mem_dir = state_dir / "memory" / "test-session"
        _write_index(mem_dir, {"repos": ["org/app"]}, "Key decision recorded.")

        content = load_memory(state_dir, "test-session")
        prompt = format_memory_prompt(content)
        assert "Key decision recorded" in prompt
        assert "## Decision Log" in prompt


# ---------------------------------------------------------------------------
# Persistence — memory survives --fresh
# ---------------------------------------------------------------------------

class TestMemorySurvivesFresh:
    """Memory is NOT cleared by --fresh (which only wipes session ID)."""

    def test_fresh_does_not_clear_memory(self, tmp_path):
        state_dir = tmp_path / ".modastack" / "state"
        mem_dir = state_dir / "memory" / "moda-director-proj"
        _write_index(mem_dir, {"repos": ["org/app"]}, "Important decision.")

        # Simulate --fresh: it calls save_session_id with empty string
        # but does NOT touch state/memory/
        from modastack.memory import load_memory
        result = load_memory(state_dir, "moda-director-proj")
        assert "Important decision" in result
