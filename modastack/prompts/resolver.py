"""Resolve agent prompts: built-in defaults + repo overrides."""

from __future__ import annotations

from pathlib import Path

from . import AGENT_BASE_PATH, AGENTS_DIR


def resolve_agent_prompt(
    agent_name: str,
    project_path: Path | str,
    interactive: bool = True,
) -> str:
    """Build the full system prompt for an agent.

    Resolution: framework base + built-in role + project override.
    Project override replaces the built-in role if present.
    """
    parts = [AGENT_BASE_PATH.read_text()]

    project = Path(project_path)
    project_agent = project / ".modastack" / "agents" / f"{agent_name}.md"
    builtin_agent = AGENTS_DIR / f"{agent_name}.md"

    if project_agent.exists():
        parts.append(project_agent.read_text())
    elif builtin_agent.exists():
        parts.append(builtin_agent.read_text())

    if interactive:
        parts.append(
            "You can use `modastack ask \"question\"` to ask the manager "
            "for guidance on ambiguous decisions."
        )
    else:
        parts.append(
            "You are running in non-interactive mode. Make your best judgment "
            "on all decisions — do not use `modastack ask`."
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Role discovery
# ---------------------------------------------------------------------------

def _extract_description(path: Path) -> str:
    """Extract a one-sentence description from a role markdown file."""
    try:
        text = path.read_text()
    except OSError:
        return ""
    # Collect the first non-heading paragraph
    lines: list[str] = []
    found_content = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped:
            if found_content:
                break
            continue
        found_content = True
        lines.append(stripped)
    paragraph = " ".join(lines)
    # Take the first sentence
    dot = paragraph.find(". ")
    if dot >= 0:
        return paragraph[: dot + 1]
    if paragraph.endswith("."):
        return paragraph
    if len(paragraph) > 80:
        return paragraph[:77] + "..."
    return paragraph


def discover_roles(project_path: Path | str | None = None) -> list[dict]:
    """List available agent roles from built-in and project tiers.

    Returns a list of dicts: [{"name", "source", "description", "path"}].
    Project roles override built-in roles with the same name.
    """
    roles: dict[str, dict] = {}

    for md in sorted(AGENTS_DIR.glob("*.md")):
        roles[md.stem] = {
            "name": md.stem,
            "source": "built-in",
            "description": _extract_description(md),
            "path": str(md),
        }

    if project_path:
        project_agents = Path(project_path) / ".modastack" / "agents"
        if project_agents.is_dir():
            for md in sorted(project_agents.glob("*.md")):
                roles[md.stem] = {
                    "name": md.stem,
                    "source": "project",
                    "description": _extract_description(md),
                    "path": str(md),
                }

    return list(roles.values())


def format_role_list(roles: list[dict]) -> str:
    """Format roles for terminal output."""
    if not roles:
        return "No roles found."
    lines = ["Available roles:\n"]
    for r in roles:
        source = "repo" if r["source"] == "repo" else "built-in"
        lines.append(f"  {r['name']:20s} [{source:8s}]  {r['description']}")
    return "\n".join(lines)


def validate_role(role_name: str, project_path: Path | str | None = None) -> bool:
    """Check whether a role exists in either tier."""
    if (AGENTS_DIR / f"{role_name}.md").exists():
        return True
    if project_path:
        project_agent = Path(project_path) / ".modastack" / "agents" / f"{role_name}.md"
        if project_agent.exists():
            return True
    return False
