"""Tests for the long-term memory doc primitives (#456).

long_term_memory.md replaces the per-session decision log: a single team-scoped,
curated, capped file with two sections (## Facts / ## Decisions), injected
read-only into every agent prompt.
"""

import json
from pathlib import Path

from unittest.mock import patch

from bobi import paths


def _write_policy(state_dir: Path, body: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "long_term_memory.md"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# memory.load_long_term_memory
# ---------------------------------------------------------------------------

class TestLoadLongTermMemory:
    def test_returns_empty_when_absent(self, tmp_path):
        from bobi.memory import load_long_term_memory
        assert load_long_term_memory(tmp_path / "state") == ""

    def test_returns_empty_when_blank(self, tmp_path):
        from bobi.memory import load_long_term_memory
        state = tmp_path / "state"
        _write_policy(state, "   \n\n")
        assert load_long_term_memory(state) == ""

    def test_loads_two_section_content(self, tmp_path):
        from bobi.memory import load_long_term_memory
        state = tmp_path / "state"
        _write_policy(state, "## Facts\n\nThis repo uses GitHub.\n\n"
                             "## Decisions\n\nChose squash merges over rebase.")
        result = load_long_term_memory(state)
        assert "## Facts" in result
        assert "This repo uses GitHub." in result
        assert "## Decisions" in result
        assert "squash merges" in result

    def test_truncates_oversized_policy(self, tmp_path):
        from bobi.memory import load_long_term_memory, MAX_MEMORY_CHARS
        state = tmp_path / "state"
        _write_policy(state, "## Facts\n\n" + ("x" * (MAX_MEMORY_CHARS + 5000)))
        result = load_long_term_memory(state)
        assert len(result) <= MAX_MEMORY_CHARS + 200
        assert "[memory truncated]" in result

    def test_truncates_oversized_policy_loudly(self, tmp_path, caplog):
        from bobi.memory import load_long_term_memory, MAX_MEMORY_CHARS
        state = tmp_path / "state"
        _write_policy(state, "## Facts\n\n" + ("x" * (MAX_MEMORY_CHARS + 1)))
        with caplog.at_level("WARNING"):
            result = load_long_term_memory(state)
        assert len(result) <= MAX_MEMORY_CHARS
        assert "long_term_memory.md exceeds cap" in caplog.text

    def test_truncates_both_sections_when_possible(self, tmp_path):
        from bobi.memory import load_long_term_memory, MAX_MEMORY_CHARS
        state = tmp_path / "state"
        _write_policy(
            state,
            "## Facts\n\n"
            + ("fact-signal " * 2500)
            + "\n\n## Decisions\n\n"
            + ("decision-signal " * 2500),
        )
        result = load_long_term_memory(state)
        assert len(result) <= MAX_MEMORY_CHARS
        assert "## Facts" in result
        assert "fact-signal" in result
        assert "## Decisions" in result
        assert "decision-signal" in result
        assert result.count("[memory truncated]") == 2

    def test_load_raw_long_term_memory_returns_full_migrated_file(self, tmp_path):
        from bobi.memory import load_raw_long_term_memory, MAX_MEMORY_CHARS
        state = tmp_path / "state"
        state.mkdir(parents=True)
        legacy = state / "policy.md"
        legacy.write_text("## Facts\n\n" + ("x" * (MAX_MEMORY_CHARS + 5000)))
        result = load_raw_long_term_memory(state)
        assert len(result) > MAX_MEMORY_CHARS
        assert not legacy.exists()
        assert (state / "long_term_memory.md").is_file()


# ---------------------------------------------------------------------------
# memory.format_long_term_memory_prompt
# ---------------------------------------------------------------------------

class TestFormatLongTermMemoryPrompt:
    def test_wraps_in_read_only_section(self):
        from bobi.memory import format_long_term_memory_prompt
        result = format_long_term_memory_prompt("## Facts\n\nlots of facts")
        assert "## Long-Term Memory" in result
        assert "read-only" in result.lower()
        assert "lots of facts" in result

    def test_empty_content_yields_empty(self):
        from bobi.memory import format_long_term_memory_prompt
        assert format_long_term_memory_prompt("") == ""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

class TestPolicyPaths:
    def test_policy_path_under_state(self, tmp_path):
        assert paths.policy_path(tmp_path) == tmp_path / "state" / "long_term_memory.md"

    def test_cursor_path_under_state(self, tmp_path):
        assert paths.long_term_memory_cursor_path(tmp_path) == (
            tmp_path / "state" / "long_term_memory_cursor"
        )

    def test_policy_path_does_not_mkdir(self, tmp_path):
        _ = paths.policy_path(tmp_path)
        # path-only constructor must not create the state dir (read-only safe)
        assert not (tmp_path / "state").exists()


# ---------------------------------------------------------------------------
# Injection swap (spec test 5): prompts inject ## Long-Term Memory, not ## Decision Log
# ---------------------------------------------------------------------------

class TestStartupPromptInjection:
    def _install_role(self, tmp_path):
        roles_dir = paths.roles_dir(tmp_path) / "director"
        roles_dir.mkdir(parents=True)
        (roles_dir / "ROLE.md").write_text("# Director\nYou direct things.")

    # The base prompt now *describes* the read-only ## Long-Term Memory block, so its
    # bare heading appears even with no long_term_memory.md. The dynamically-injected block
    # is identified by its distinctive lead-in line instead.
    INJECTED_MARKER = "Below is the team's curated, durable long-term memory"

    def test_injects_long_term_memory(self, tmp_path):
        from bobi.prompts.resolver import build_startup_prompt
        self._install_role(tmp_path)
        state = paths.state_path(tmp_path)
        _write_policy(state, "## Facts\n\nThis repo uses GitHub.\n\n## Decisions\n\n"
                             "Chose squash merges.")
        result = build_startup_prompt("director", tmp_path, agent_name="test",
                                      session_name="moda-director-tmp")
        assert self.INJECTED_MARKER in result
        assert "This repo uses GitHub." in result
        assert "squash merges" in result
        assert "## Decision Log" not in result

    def test_no_policy_no_section(self, tmp_path):
        from bobi.prompts.resolver import build_startup_prompt
        self._install_role(tmp_path)
        result = build_startup_prompt("director", tmp_path, agent_name="test")
        assert self.INJECTED_MARKER not in result
        assert "## Decision Log" not in result


class TestSubagentMemoryInjection:
    def test_load_long_term_memory_prompt_reads_long_term_memory(self, tmp_path, monkeypatch):
        import bobi.subagent as subagent
        state = paths.state_path(tmp_path)
        _write_policy(state, "## Decisions\n\nKey decision recorded.")
        monkeypatch.setattr(paths, "state_path", lambda *a, **k: state)
        out = subagent._load_long_term_memory_prompt()
        assert "## Long-Term Memory" in out
        assert "Key decision recorded." in out

    def test_load_long_term_memory_prompt_empty_when_absent(self, tmp_path, monkeypatch):
        import bobi.subagent as subagent
        monkeypatch.setattr(paths, "state_path",
                            lambda *a, **k: tmp_path / "state")
        assert subagent._load_long_term_memory_prompt() == ""


class TestDoctorPolicyCheck:
    def test_ok_when_no_policy(self, tmp_path):
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert r.ok

    def test_ok_when_sleep_cycle_due_tick_had_no_work(self, tmp_path):
        state = paths.state_path(tmp_path)
        state.mkdir(parents=True)
        (state / "monitor_state.json").write_text(json.dumps({
            "sleep-cycle": {"last_run": "2026-07-08T13:33:00+00:00"}
        }))
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert r.ok

    def test_flags_sleep_cycle_spawn_without_policy_or_cursor(self, tmp_path):
        state = paths.state_path(tmp_path)
        state.mkdir(parents=True)
        (state / "monitor_state.json").write_text(json.dumps({
            "sleep-cycle": {
                "last_run": "2026-07-08T13:33:00+00:00",
                "last_spawn": "2026-07-08T13:33:00+00:00",
            }
        }))
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert not r.ok
        assert "sleep-cycle has spawned" in r.detail

    def test_ok_when_policy_present(self, tmp_path):
        state = paths.state_path(tmp_path)
        _write_policy(state, "## Facts\n\nsmall and bounded")
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert r.ok
        assert "long_term_memory.md present" in r.detail

    def test_flags_oversized_policy(self, tmp_path):
        from bobi.memory import MAX_MEMORY_CHARS
        state = paths.state_path(tmp_path)
        _write_policy(state, "x" * (MAX_MEMORY_CHARS + 100))
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert not r.ok
        assert "over" in r.detail
