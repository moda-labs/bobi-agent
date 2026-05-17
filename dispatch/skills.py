"""Skills discovery — detect installed skill packs and adapt prompts."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


SKILL_SEARCH_PATHS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".codex" / "skills",
    Path.home() / ".cursor" / "skills",
]


@dataclass
class Skill:
    """A single skill that can be invoked."""

    name: str
    description: str
    trigger: str  # slash command or invocation pattern


@dataclass
class SkillPack:
    """A set of skills from one source (e.g., gstack, custom, third-party)."""

    name: str
    path: Path
    skills: list[Skill] = field(default_factory=list)

    @classmethod
    def from_directory(cls, path: Path) -> "SkillPack | None":
        """Try to load a skill pack from a directory."""
        if not path.is_dir():
            return None

        # Check for a manifest (skill-pack.yaml or SKILL.md)
        manifest = path / "skill-pack.yaml"
        if manifest.exists():
            return cls._from_manifest(path, manifest)

        # Heuristic: scan for SKILL.md files in subdirectories
        skills = []
        for child in path.iterdir():
            if child.is_dir():
                skill_md = child / "SKILL.md"
                if skill_md.exists():
                    skills.append(Skill(
                        name=child.name,
                        description=_extract_description(skill_md),
                        trigger=f"/{child.name}",
                    ))

        if not skills:
            return None

        return cls(name=path.name, path=path, skills=skills)

    @classmethod
    def _from_manifest(cls, path: Path, manifest: Path) -> "SkillPack":
        raw = yaml.safe_load(manifest.read_text()) or {}
        skills = []
        for entry in raw.get("skills", []):
            skills.append(Skill(
                name=entry["name"],
                description=entry.get("description", ""),
                trigger=entry.get("trigger", f"/{entry['name']}"),
            ))
        return cls(name=raw.get("name", path.name), path=path, skills=skills)


def discover_skill_packs() -> list[SkillPack]:
    """Scan known locations for installed skill packs."""
    packs = []

    for search_path in SKILL_SEARCH_PATHS:
        if not search_path.exists():
            continue
        for child in search_path.iterdir():
            if child.is_dir():
                pack = SkillPack.from_directory(child)
                if pack:
                    packs.append(pack)

    return packs


def get_relevant_skills(packs: list[SkillPack], task_labels: list[str] | None = None) -> list[Skill]:
    """Filter skills to those relevant for a given task."""
    # Relevance mapping: label → skill names that help
    relevance = {
        "bug": ["investigate", "review"],
        "feature": ["office-hours", "plan-eng-review", "ship", "review"],
        "refactor": ["review", "ship"],
        "security": ["cso"],
        "docs": ["document-release"],
        "performance": ["benchmark"],
        "design": ["design-review", "plan-design-review"],
        "qa": ["qa", "browse"],
        "deploy": ["land-and-deploy", "canary"],
    }

    if not task_labels:
        # Default: return the most universally useful skills
        universal = {"review", "ship", "investigate"}
        return [s for p in packs for s in p.skills if s.name in universal]

    relevant_names = set()
    for label in task_labels:
        label_lower = label.lower()
        for key, skill_names in relevance.items():
            if key in label_lower:
                relevant_names.update(skill_names)

    if not relevant_names:
        relevant_names = {"review", "ship"}

    return [s for p in packs for s in p.skills if s.name in relevant_names]


def format_skills_for_prompt(skills: list[Skill]) -> str:
    """Format discovered skills into prompt text for the agent."""
    if not skills:
        return ""

    lines = ["## Available skills (auto-detected)", ""]
    for skill in skills:
        desc = f" — {skill.description}" if skill.description else ""
        lines.append(f"- `{skill.trigger}`{desc}")

    lines.append("")
    lines.append("Use these skills when appropriate for the task.")
    return "\n".join(lines)


def _extract_description(skill_md: Path) -> str:
    """Pull the first meaningful line from a SKILL.md as description."""
    try:
        for line in skill_md.read_text().splitlines()[:20]:
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("---"):
                return line[:120]
    except (OSError, UnicodeDecodeError):
        pass
    return ""
