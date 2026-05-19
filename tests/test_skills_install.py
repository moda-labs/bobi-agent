"""Tests for skill installation and discovery.

Verifies that agentd skills are properly installed in .claude/skills/
and discoverable by Claude Code.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SKILLS_ROOT = REPO_ROOT / "skills"
ENGINEER_SKILLS_SRC = SKILLS_ROOT / "engineer"
METHODOLOGY_SKILLS_SRC = SKILLS_ROOT / "methodology"
DOMAIN_SKILLS_SRC = SKILLS_ROOT / "domains"
SKILLS_INSTALLED = REPO_ROOT / ".claude" / "skills"

EXPECTED_ENGINEER_SKILLS = ["pickup", "spec", "implement", "ship-pr", "feedback"]
EXPECTED_METHODOLOGY_SKILLS = ["frontdoor", "build", "brand-identity", "office-helper"]
EXPECTED_DOMAIN_SKILLS = ["ticketing", "source-control", "code-review", "knowledge-base", "messaging"]
EXPECTED_SKILLS = EXPECTED_ENGINEER_SKILLS + EXPECTED_METHODOLOGY_SKILLS + EXPECTED_DOMAIN_SKILLS


class TestSkillsExist:

    def test_source_skills_directory_exists(self):
        assert ENGINEER_SKILLS_SRC.exists()
        assert METHODOLOGY_SKILLS_SRC.exists()
        assert DOMAIN_SKILLS_SRC.exists()

    def test_all_source_skills_have_skill_md(self):
        for name in EXPECTED_ENGINEER_SKILLS:
            skill_md = ENGINEER_SKILLS_SRC / name / "SKILL.md"
            assert skill_md.exists(), f"Missing {skill_md}"
        for name in EXPECTED_METHODOLOGY_SKILLS:
            skill_md = METHODOLOGY_SKILLS_SRC / name / "SKILL.md"
            assert skill_md.exists(), f"Missing {skill_md}"
        for name in EXPECTED_DOMAIN_SKILLS:
            skill_md = DOMAIN_SKILLS_SRC / name / "SKILL.md"
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
            expected = _skill_path(name).parent.resolve()
            assert resolved == expected, (
                f".claude/skills/{name} points to {resolved}, expected {expected}"
            )

    def test_installed_skills_have_skill_md(self):
        for name in EXPECTED_SKILLS:
            skill_md = SKILLS_INSTALLED / name / "SKILL.md"
            assert skill_md.exists(), (
                f".claude/skills/{name}/SKILL.md not found (broken symlink?)"
            )


def _skill_path(name: str) -> Path:
    """Resolve a skill name to its source path."""
    if name in EXPECTED_ENGINEER_SKILLS:
        return ENGINEER_SKILLS_SRC / name / "SKILL.md"
    if name in EXPECTED_DOMAIN_SKILLS:
        return DOMAIN_SKILLS_SRC / name / "SKILL.md"
    return METHODOLOGY_SKILLS_SRC / name / "SKILL.md"


class TestSkillContent:

    def test_skill_md_not_empty(self):
        for name in EXPECTED_SKILLS:
            content = _skill_path(name).read_text()
            assert len(content) > 50, f"{name}/SKILL.md is too short ({len(content)} chars)"

    def test_skill_md_has_title(self):
        for name in EXPECTED_SKILLS:
            content = _skill_path(name).read_text()
            # Skip YAML frontmatter if present
            lines = content.splitlines()
            has_title = any(l.startswith("# ") for l in lines[:30])
            has_frontmatter = lines[0].strip() == "---"
            assert has_title or has_frontmatter, f"{name}/SKILL.md missing # title or frontmatter"

    def test_engineer_skills_reference_domains(self):
        """Engineer workflow skills should reference domain skills, not hardcode platform details."""
        for name in EXPECTED_ENGINEER_SKILLS:
            content = _skill_path(name).read_text()
            assert "domains/" in content, (
                f"{name}/SKILL.md should reference domains/ for platform-specific knowledge"
            )

    def test_pickup_creates_worktree(self):
        content = _skill_path("pickup").read_text()
        assert "worktree" in content.lower()

    def test_implement_references_review(self):
        content = _skill_path("implement").read_text()
        assert "/review" in content

    def test_ship_pr_references_ship(self):
        content = _skill_path("ship-pr").read_text()
        assert "/ship" in content

    def test_ship_pr_moves_to_in_review(self):
        content = _skill_path("ship-pr").read_text()
        assert "In Review" in content

    def test_no_engineer_skill_invokes_another_engineer_skill(self):
        """Engineer skills should not chain — the manager routes between phases."""
        for name in EXPECTED_ENGINEER_SKILLS:
            content = _skill_path(name).read_text()
            for other in EXPECTED_ENGINEER_SKILLS:
                if other == name:
                    continue
                assert f"invoke /{other}" not in content.lower(), (
                    f"{name}/SKILL.md invokes /{other} — engineer skills should not chain"
                )
