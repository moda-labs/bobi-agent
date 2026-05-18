"""Tests for skill installation and discovery.

Verifies that agentd skills are properly installed in .claude/skills/
and discoverable by Claude Code.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SKILLS_SRC = REPO_ROOT / "skills"
SKILLS_INSTALLED = REPO_ROOT / ".claude" / "skills"

EXPECTED_SKILLS = ["pickup", "spec", "implement", "ship-pr", "feedback"]


class TestSkillsExist:

    def test_source_skills_directory_exists(self):
        assert SKILLS_SRC.exists()

    def test_all_source_skills_have_skill_md(self):
        for name in EXPECTED_SKILLS:
            skill_md = SKILLS_SRC / name / "SKILL.md"
            assert skill_md.exists(), f"Missing {skill_md}"

    def test_installed_skills_directory_exists(self):
        assert SKILLS_INSTALLED.exists(), (
            ".claude/skills/ not found. Run setup to install skills."
        )

    def test_all_skills_installed(self):
        for name in EXPECTED_SKILLS:
            installed = SKILLS_INSTALLED / name
            assert installed.exists(), f"Skill '{name}' not installed in .claude/skills/"

    def test_installed_skills_are_symlinks(self):
        for name in EXPECTED_SKILLS:
            installed = SKILLS_INSTALLED / name
            if not installed.exists():
                continue
            assert installed.is_symlink(), (
                f".claude/skills/{name} should be a symlink, not a copy"
            )

    def test_symlinks_resolve_to_source(self):
        for name in EXPECTED_SKILLS:
            installed = SKILLS_INSTALLED / name
            if not installed.exists():
                continue
            resolved = installed.resolve()
            expected = (SKILLS_SRC / name).resolve()
            assert resolved == expected, (
                f".claude/skills/{name} points to {resolved}, expected {expected}"
            )

    def test_installed_skills_have_skill_md(self):
        for name in EXPECTED_SKILLS:
            skill_md = SKILLS_INSTALLED / name / "SKILL.md"
            assert skill_md.exists(), (
                f".claude/skills/{name}/SKILL.md not found (broken symlink?)"
            )


class TestSkillContent:

    def test_skill_md_not_empty(self):
        for name in EXPECTED_SKILLS:
            skill_md = SKILLS_SRC / name / "SKILL.md"
            content = skill_md.read_text()
            assert len(content) > 50, f"{name}/SKILL.md is too short ({len(content)} chars)"

    def test_skill_md_has_title(self):
        for name in EXPECTED_SKILLS:
            skill_md = SKILLS_SRC / name / "SKILL.md"
            first_line = skill_md.read_text().splitlines()[0]
            assert first_line.startswith("# "), f"{name}/SKILL.md missing # title"

    def test_skill_md_has_exit_contract(self):
        """Every skill except pickup should have an EXIT CONTRACT."""
        for name in EXPECTED_SKILLS:
            skill_md = SKILLS_SRC / name / "SKILL.md"
            content = skill_md.read_text()
            assert "EXIT CONTRACT" in content, (
                f"{name}/SKILL.md missing EXIT CONTRACT section"
            )

    def test_pickup_creates_worktree(self):
        content = (SKILLS_SRC / "pickup" / "SKILL.md").read_text()
        assert "worktree" in content.lower()

    def test_implement_references_review(self):
        content = (SKILLS_SRC / "implement" / "SKILL.md").read_text()
        assert "/review" in content

    def test_ship_pr_references_ship(self):
        content = (SKILLS_SRC / "ship-pr" / "SKILL.md").read_text()
        assert "/ship" in content

    def test_no_skill_invokes_another_skill_directly(self):
        """Skills should not chain — the daemon routes between phases."""
        for name in EXPECTED_SKILLS:
            content = (SKILLS_SRC / name / "SKILL.md").read_text()
            for other in EXPECTED_SKILLS:
                if other == name:
                    continue
                assert f"invoke /{other}" not in content.lower(), (
                    f"{name}/SKILL.md invokes /{other} — skills should not chain"
                )
