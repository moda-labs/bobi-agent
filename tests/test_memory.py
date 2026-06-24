"""Tests for the legacy decision-log reader, retained for the policy seed (#456).

The per-session decision log is no longer injected into prompts — the team
policy (test_policy.py) replaced it. ``load_memory`` / ``memory_dir_for_session``
survive only so the one-time seed can distill existing INDEX.md journals into
the first policy.md.
"""

from pathlib import Path

import yaml


def _memory_dir(state_dir: Path, session_name: str) -> Path:
    return state_dir / "memory" / session_name


def _write_index(mem_dir: Path, yaml_block: dict, notes: str = "") -> Path:
    mem_dir.mkdir(parents=True, exist_ok=True)
    content = "---\n" + yaml.dump(yaml_block, default_flow_style=False).strip() + "\n---\n"
    if notes:
        content += "\n" + notes + "\n"
    index = mem_dir / "INDEX.md"
    index.write_text(content)
    return index


def _write_note(mem_dir: Path, filename: str, text: str) -> Path:
    mem_dir.mkdir(parents=True, exist_ok=True)
    p = mem_dir / filename
    p.write_text(text)
    return p


class TestLoadMemory:
    """Raw read of a legacy journal — used by the policy seed only."""

    def test_returns_empty_when_no_memory_dir(self, tmp_path):
        from modastack.memory import load_memory
        assert load_memory(tmp_path / "state", "moda-director-myproject") == ""

    def test_returns_empty_when_dir_exists_but_no_index(self, tmp_path):
        from modastack.memory import load_memory
        mem_dir = _memory_dir(tmp_path / "state", "moda-director-myproject")
        mem_dir.mkdir(parents=True)
        assert load_memory(tmp_path / "state", "moda-director-myproject") == ""

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
        huge_notes = "x" * (MAX_MEMORY_CHARS + 1000)
        _write_index(mem_dir, {"big": True}, huge_notes)
        result = load_memory(state, "moda-director-myproject")
        assert len(result) <= MAX_MEMORY_CHARS + 200


class TestMemoryDir:
    def test_returns_correct_path(self, tmp_path):
        from modastack.memory import memory_dir_for_session
        result = memory_dir_for_session(tmp_path / "state", "moda-director-proj")
        assert result == tmp_path / "state" / "memory" / "moda-director-proj"


class TestMemorySurvivesFresh:
    """Journals are NOT cleared by --fresh (which only wipes session ID)."""

    def test_fresh_does_not_clear_memory(self, tmp_path):
        state_dir = tmp_path / ".modastack" / "state"
        mem_dir = state_dir / "memory" / "moda-director-proj"
        _write_index(mem_dir, {"repos": ["org/app"]}, "Important decision.")
        from modastack.memory import load_memory
        assert "Important decision" in load_memory(state_dir, "moda-director-proj")
