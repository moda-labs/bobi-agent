"""Tests for skill installation and discovery.

Verifies that modastack skills are properly installed in .claude/skills/
and discoverable by Claude Code.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ENGINEER_ROOT = REPO_ROOT / "engineer"
PROCESS_SKILLS_SRC = ENGINEER_ROOT / "process"
PRACTICES_SKILLS_SRC = ENGINEER_ROOT / "practices"
PRODUCT_MANAGER_SKILLS_SRC = REPO_ROOT / "product_manager"
TOOLS_SKILLS_SRC = REPO_ROOT / "tools"  # shared, top-level
SKILLS_INSTALLED = REPO_ROOT / ".claude" / "skills"

EXPECTED_PROCESS_SKILLS = ["pickup", "spec", "implement", "prepare-pr", "feedback"]
EXPECTED_PRACTICES_SKILLS = [
    "triage", "build", "code-review", "ticketing-policy", "source-control-conventions",
    "review", "investigate", "ship", "autoplan",
    "plan-eng-review", "plan-design-review", "plan-ceo-review",
    "office-hours", "qa",
]
EXPECTED_PRODUCT_MANAGER_SKILLS = ["brand-identity", "design-critic"]
EXPECTED_TOOLS_SKILLS = ["linear", "github-issues", "git", "github", "slack", "notion"]
EXPECTED_SKILLS = EXPECTED_PROCESS_SKILLS + EXPECTED_PRACTICES_SKILLS + EXPECTED_PRODUCT_MANAGER_SKILLS + EXPECTED_TOOLS_SKILLS


class TestSkillsExist:

    def test_source_skills_directory_exists(self):
        assert PROCESS_SKILLS_SRC.exists()
        assert PRACTICES_SKILLS_SRC.exists()
        assert PRODUCT_MANAGER_SKILLS_SRC.exists()
        assert TOOLS_SKILLS_SRC.exists()

    def test_all_source_skills_have_skill_md(self):
        for name in EXPECTED_PROCESS_SKILLS:
            skill_md = PROCESS_SKILLS_SRC / name / "SKILL.md"
            assert skill_md.exists(), f"Missing {skill_md}"
        for name in EXPECTED_PRACTICES_SKILLS:
            skill_md = PRACTICES_SKILLS_SRC / name / "SKILL.md"
            assert skill_md.exists(), f"Missing {skill_md}"
        for name in EXPECTED_PRODUCT_MANAGER_SKILLS:
            skill_md = PRODUCT_MANAGER_SKILLS_SRC / name / "SKILL.md"
            assert skill_md.exists(), f"Missing {skill_md}"
        for name in EXPECTED_TOOLS_SKILLS:
            skill_md = TOOLS_SKILLS_SRC / name / "SKILL.md"
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
    if name in EXPECTED_PROCESS_SKILLS:
        return PROCESS_SKILLS_SRC / name / "SKILL.md"
    if name in EXPECTED_PRACTICES_SKILLS:
        return PRACTICES_SKILLS_SRC / name / "SKILL.md"
    if name in EXPECTED_PRODUCT_MANAGER_SKILLS:
        return PRODUCT_MANAGER_SKILLS_SRC / name / "SKILL.md"
    return TOOLS_SKILLS_SRC / name / "SKILL.md"


class TestSkillContent:

    def test_skill_md_not_empty(self):
        for name in EXPECTED_SKILLS:
            content = _skill_path(name).read_text()
            assert len(content) > 50, f"{name}/SKILL.md is too short ({len(content)} chars)"

    def test_skill_md_has_title(self):
        for name in EXPECTED_SKILLS:
            content = _skill_path(name).read_text()
            lines = content.splitlines()
            has_title = any(l.startswith("# ") for l in lines[:30])
            has_frontmatter = lines[0].strip() == "---"
            assert has_title or has_frontmatter, f"{name}/SKILL.md missing # title or frontmatter"

    def test_process_skills_reference_practices_or_tools(self):
        """Process skills should reference practices/ or tools/ for platform-specific knowledge."""
        for name in EXPECTED_PROCESS_SKILLS:
            content = _skill_path(name).read_text()
            assert "practices/" in content or "tools/" in content, (
                f"{name}/SKILL.md should reference practices/ or tools/"
            )

    def test_pickup_creates_worktree(self):
        content = _skill_path("pickup").read_text()
        assert "worktree" in content.lower()

    def test_implement_references_review(self):
        content = _skill_path("implement").read_text()
        assert "/review" in content

    def test_prepare_pr_references_ship(self):
        content = _skill_path("prepare-pr").read_text()
        assert "/ship" in content

    def test_prepare_pr_moves_to_in_review(self):
        content = _skill_path("prepare-pr").read_text()
        assert "In Review" in content

    def test_no_process_skill_invokes_another_process_skill(self):
        """Process skills should not chain — the manager routes between phases."""
        for name in EXPECTED_PROCESS_SKILLS:
            content = _skill_path(name).read_text()
            for other in EXPECTED_PROCESS_SKILLS:
                if other == name:
                    continue
                assert f"invoke /{other}" not in content.lower(), (
                    f"{name}/SKILL.md invokes /{other} — process skills should not chain"
                )
