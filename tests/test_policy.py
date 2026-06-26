"""Tests for the team policy doc primitives (#456).

policy.md replaces the per-session decision log: a single team-scoped,
curated, capped file with two sections (## Facts / ## Decisions), injected
read-only into every agent prompt.
"""

from pathlib import Path

from unittest.mock import patch


def _write_policy(state_dir: Path, body: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "policy.md"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# memory.load_policy
# ---------------------------------------------------------------------------

class TestLoadPolicy:
    def test_returns_empty_when_absent(self, tmp_path):
        from bobi.memory import load_policy
        assert load_policy(tmp_path / "state") == ""

    def test_returns_empty_when_blank(self, tmp_path):
        from bobi.memory import load_policy
        state = tmp_path / "state"
        _write_policy(state, "   \n\n")
        assert load_policy(state) == ""

    def test_loads_two_section_content(self, tmp_path):
        from bobi.memory import load_policy
        state = tmp_path / "state"
        _write_policy(state, "## Facts\n\nThis repo uses GitHub.\n\n"
                             "## Decisions\n\nChose squash merges over rebase.")
        result = load_policy(state)
        assert "## Facts" in result
        assert "This repo uses GitHub." in result
        assert "## Decisions" in result
        assert "squash merges" in result

    def test_truncates_oversized_policy(self, tmp_path):
        from bobi.memory import load_policy, MAX_POLICY_CHARS
        state = tmp_path / "state"
        _write_policy(state, "## Facts\n\n" + ("x" * (MAX_POLICY_CHARS + 5000)))
        result = load_policy(state)
        assert len(result) <= MAX_POLICY_CHARS + 200
        assert "[policy truncated]" in result


# ---------------------------------------------------------------------------
# memory.format_policy_prompt
# ---------------------------------------------------------------------------

class TestFormatPolicyPrompt:
    def test_wraps_in_read_only_section(self):
        from bobi.memory import format_policy_prompt
        result = format_policy_prompt("## Facts\n\nlots of facts")
        assert "## Team Policy" in result
        assert "read-only" in result.lower()
        assert "lots of facts" in result

    def test_empty_content_yields_empty(self):
        from bobi.memory import format_policy_prompt
        assert format_policy_prompt("") == ""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

class TestPolicyPaths:
    def test_policy_path_under_state(self, tmp_path):
        from bobi import paths
        assert paths.policy_path(tmp_path) == tmp_path / ".bobi" / "state" / "policy.md"

    def test_cursor_path_under_state(self, tmp_path):
        from bobi import paths
        assert paths.policy_cursor_path(tmp_path) == (
            tmp_path / ".bobi" / "state" / "policy_cursor"
        )

    def test_policy_path_does_not_mkdir(self, tmp_path):
        from bobi import paths
        _ = paths.policy_path(tmp_path)
        # path-only constructor must not create the state dir (read-only safe)
        assert not (tmp_path / ".bobi" / "state").exists()


# ---------------------------------------------------------------------------
# Injection swap (spec test 5): prompts inject ## Team Policy, not ## Decision Log
# ---------------------------------------------------------------------------

class TestStartupPromptInjection:
    def _install_role(self, tmp_path):
        roles_dir = tmp_path / ".bobi" / "roles" / "director"
        roles_dir.mkdir(parents=True)
        (roles_dir / "ROLE.md").write_text("# Director\nYou direct things.")

    # The base prompt now *describes* the read-only ## Team Policy block, so its
    # bare heading appears even with no policy.md. The dynamically-injected block
    # is identified by its distinctive lead-in line instead.
    INJECTED_MARKER = "Below is the team's curated, durable policy"

    def test_injects_team_policy(self, tmp_path):
        from bobi.prompts.resolver import build_startup_prompt
        self._install_role(tmp_path)
        state = tmp_path / ".bobi" / "state"
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


class TestSubagentPolicyInjection:
    def test_load_policy_prompt_reads_team_policy(self, tmp_path, monkeypatch):
        from bobi import paths
        import bobi.subagent as subagent
        state = tmp_path / ".bobi" / "state"
        _write_policy(state, "## Decisions\n\nKey decision recorded.")
        monkeypatch.setattr(paths, "state_path", lambda *a, **k: state)
        out = subagent._load_policy_prompt()
        assert "## Team Policy" in out
        assert "Key decision recorded." in out

    def test_load_policy_prompt_empty_when_absent(self, tmp_path, monkeypatch):
        from bobi import paths
        import bobi.subagent as subagent
        monkeypatch.setattr(paths, "state_path",
                            lambda *a, **k: tmp_path / ".bobi" / "state")
        assert subagent._load_policy_prompt() == ""


class TestDoctorPolicyCheck:
    def test_ok_when_no_policy(self, tmp_path):
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert r.ok

    def test_ok_when_policy_present(self, tmp_path):
        state = tmp_path / ".bobi" / "state"
        _write_policy(state, "## Facts\n\nsmall and bounded")
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert r.ok
        assert "policy.md present" in r.detail

    def test_flags_oversized_policy(self, tmp_path):
        from bobi.memory import MAX_POLICY_CHARS
        state = tmp_path / ".bobi" / "state"
        _write_policy(state, "x" * (MAX_POLICY_CHARS + 100))
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert not r.ok
        assert "over" in r.detail
