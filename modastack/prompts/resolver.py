"""Resolve agent prompts: base + role + project override."""

from __future__ import annotations

import logging
from pathlib import Path

from . import BASE_PATH, AGENTS_DIR

log = logging.getLogger(__name__)


def resolve_agent_prompt(
    role: str,
    project_path: Path | str,
    interactive: bool = True,
) -> str:
    """Build the full prompt for an agent with a given role.

    Resolution: base.md + agents/{role}.md (built-in) or
    .modastack/agents/{role}.md (project override replaces built-in).
    """
    parts = [BASE_PATH.read_text()]

    project = Path(project_path)
    project_agent = project / ".modastack" / "agents" / f"{role}.md"
    builtin_agent = AGENTS_DIR / f"{role}.md"

    if project_agent.exists():
        parts.append(project_agent.read_text())
    elif builtin_agent.exists():
        parts.append(builtin_agent.read_text())

    if interactive:
        parts.append(
            "You can use `modastack ask \"question\"` to ask for guidance "
            "on ambiguous decisions."
        )
    else:
        parts.append(
            "You are running in non-interactive mode. Make your best judgment "
            "on all decisions — do not use `modastack ask`."
        )

    return "\n\n".join(parts)


def build_startup_prompt(
    role: str,
    project_path: Path | str,
) -> str:
    """Build the startup prompt for a persistent agent.

    Includes the role prompt and a list of available workflows.
    """
    prompt = resolve_agent_prompt(role, project_path, interactive=True)
    workflows = list_workflows(project_path)

    project = Path(project_path)
    return (
        f"You are a modastack {role} for {project.name}. "
        f"Act directly using your tools.\n\n{prompt}\n\n"
        f"## Available workflows\n\n{workflows}"
    )


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
    dot = paragraph.find(". ")
    if dot >= 0:
        return paragraph[: dot + 1]
    if paragraph.endswith("."):
        return paragraph
    if len(paragraph) > 80:
        return paragraph[:77] + "..."
    return paragraph


def discover_roles(project_path: Path | str | None = None) -> list[dict]:
    """List available agent roles from built-in and project tiers."""
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
