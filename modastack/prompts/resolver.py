"""Resolve agent prompts: built-in defaults + repo overrides."""

from __future__ import annotations

import logging
from pathlib import Path

from . import AGENT_BASE_PATH, AGENTS_DIR, MANAGER_BASE_PATH

log = logging.getLogger(__name__)


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


def resolve_manager_prompt(project_path: Path | str) -> str:
    """Build the full system prompt for a manager agent.

    Resolution: manager_base.md + manager_engineering.md (if exists)
    + project .modastack/manager.md (if exists).
    """
    project = Path(project_path)
    parts = [MANAGER_BASE_PATH.read_text()]

    builtin_role = MANAGER_BASE_PATH.parent / "manager_engineering.md"
    if builtin_role.exists():
        parts.append(builtin_role.read_text())

    repo_mgr = project / ".modastack" / "manager.md"
    if repo_mgr.exists():
        parts.append(f"## {project.name} policies\n\n" + repo_mgr.read_text())

    return "\n\n".join(parts)


def list_workflows(project_path: Path | str) -> str:
    """List available workflows as a formatted string for agent prompts."""
    try:
        from modastack.workflow.triggers import WORKFLOWS_DIR
        from modastack.workflow.schema import load_workflow

        project = Path(project_path)
        sources = [WORKFLOWS_DIR]
        repo_wf = project / ".modastack" / "workflows"
        if repo_wf.exists():
            sources.append(repo_wf)

        seen: set[str] = set()
        lines: list[str] = []
        for d in reversed(sources):
            for f in sorted(d.glob("*.yaml")):
                if f.stem in seen:
                    continue
                seen.add(f.stem)
                try:
                    wf = load_workflow(f)
                    lines.append(f"- {wf.name}: trigger={wf.trigger.event}, {len(wf.nodes)} nodes")
                except Exception:
                    continue
        return "\n".join(lines) if lines else "No workflows found."
    except Exception:
        return ""


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
