"""Tests for skills discovery."""

from pathlib import Path

import yaml

from dispatch.skills import (
    Skill,
    SkillPack,
    discover_skill_packs,
    get_relevant_skills,
    format_skills_for_prompt,
)


def test_skill_pack_from_manifest(tmp_path):
    manifest = tmp_path / "skill-pack.yaml"
    manifest.write_text(yaml.dump({
        "name": "test-pack",
        "skills": [
            {"name": "deploy", "description": "Deploy to prod", "trigger": "/deploy"},
            {"name": "rollback", "description": "Rollback last deploy", "trigger": "/rollback"},
        ],
    }))

    pack = SkillPack.from_directory(tmp_path)
    assert pack is not None
    assert pack.name == "test-pack"
    assert len(pack.skills) == 2
    assert pack.skills[0].name == "deploy"
    assert pack.skills[0].trigger == "/deploy"


def test_skill_pack_from_convention(tmp_path):
    # Create gstack-like structure
    (tmp_path / "review").mkdir()
    (tmp_path / "review" / "SKILL.md").write_text("Review code for bugs and issues.\n\n# Details...")

    (tmp_path / "ship").mkdir()
    (tmp_path / "ship" / "SKILL.md").write_text("Ship workflow: test, commit, push, PR.\n\n# Details...")

    pack = SkillPack.from_directory(tmp_path)
    assert pack is not None
    assert len(pack.skills) == 2
    names = {s.name for s in pack.skills}
    assert "review" in names
    assert "ship" in names


def test_skill_pack_empty_dir(tmp_path):
    pack = SkillPack.from_directory(tmp_path)
    assert pack is None


def test_skill_pack_nonexistent():
    pack = SkillPack.from_directory(Path("/nonexistent"))
    assert pack is None


def test_get_relevant_skills_bug_label():
    packs = [SkillPack(
        name="test",
        path=Path("/tmp"),
        skills=[
            Skill("investigate", "Debug issues", "/investigate"),
            Skill("review", "Code review", "/review"),
            Skill("ship", "Ship code", "/ship"),
            Skill("benchmark", "Perf testing", "/benchmark"),
        ],
    )]

    relevant = get_relevant_skills(packs, ["bug", "agent"])
    names = {s.name for s in relevant}
    assert "investigate" in names
    assert "review" in names
    assert "benchmark" not in names


def test_get_relevant_skills_feature_label():
    packs = [SkillPack(
        name="test",
        path=Path("/tmp"),
        skills=[
            Skill("office-hours", "Product thinking", "/office-hours"),
            Skill("ship", "Ship code", "/ship"),
            Skill("review", "Code review", "/review"),
            Skill("cso", "Security audit", "/cso"),
        ],
    )]

    relevant = get_relevant_skills(packs, ["feature"])
    names = {s.name for s in relevant}
    assert "office-hours" in names
    assert "ship" in names
    assert "review" in names
    assert "cso" not in names


def test_get_relevant_skills_no_labels_defaults():
    packs = [SkillPack(
        name="test",
        path=Path("/tmp"),
        skills=[
            Skill("review", "Code review", "/review"),
            Skill("ship", "Ship code", "/ship"),
            Skill("investigate", "Debug", "/investigate"),
            Skill("benchmark", "Perf", "/benchmark"),
        ],
    )]

    relevant = get_relevant_skills(packs, None)
    names = {s.name for s in relevant}
    assert "review" in names
    assert "ship" in names
    assert "investigate" in names
    assert "benchmark" not in names


def test_format_skills_empty():
    assert format_skills_for_prompt([]) == ""


def test_format_skills_output():
    skills = [
        Skill("review", "Code review", "/review"),
        Skill("ship", "Ship workflow", "/ship"),
    ]
    output = format_skills_for_prompt(skills)
    assert "/review" in output
    assert "/ship" in output
    assert "Code review" in output
    assert "Available skills" in output
