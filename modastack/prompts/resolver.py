"""Resolve agent prompts: base + agent pack role + project override."""

from __future__ import annotations

import logging
from pathlib import Path

from . import BASE_PATH, AGENTS_DIR

log = logging.getLogger(__name__)


def _resolve_agent_dir(agent_name: str | None, project_path: Path | None = None) -> Path | None:
    """Find the agent pack: .modastack/agents/{name} → built-in agents/{name}."""
    if not agent_name:
        return None
    if project_path:
        local = Path(project_path) / ".modastack" / "agents" / agent_name
        if local.is_dir():
            return local
    d = AGENTS_DIR / agent_name
    return d if d.is_dir() else None


def resolve_agent_prompt(
    role: str,
    project_path: Path | str,
    agent_name: str | None = None,
    interactive: bool = True,
) -> str:
    """Build the full prompt for an agent with a given role.

    Resolution: base.md + agent pack role (agents/{agent}/roles/{role}.md)
    or project override (.modastack/roles/{role}.md replaces pack role).
    """
    parts = [BASE_PATH.read_text()]

    project = Path(project_path)
    project_role = project / ".modastack" / "roles" / f"{role}.md"

    if project_role.exists():
        parts.append(project_role.read_text())
    else:
        agent_dir = _resolve_agent_dir(agent_name, project)
        if agent_dir:
            pack_role = agent_dir / "roles" / f"{role}.md"
            if pack_role.exists():
                parts.append(pack_role.read_text())

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
    agent_name: str | None = None,
) -> str:
    """Build the startup prompt for a persistent agent."""
    prompt = resolve_agent_prompt(role, project_path, agent_name=agent_name, interactive=True)
    workflows = list_workflows(project_path, agent_name=agent_name)

    project = Path(project_path)
    return (
        f"You are a modastack {role} for {project.name}. "
        f"Act directly using your tools.\n\n{prompt}\n\n"
        f"## Available workflows\n\n{workflows}"
    )


def list_workflows(project_path: Path | str, agent_name: str | None = None) -> str:
    """List available workflows as a formatted string for agent prompts."""
    try:
        from modastack.workflow.schema import load_workflow

        project = Path(project_path)
        sources: list[Path] = []

        agent_dir = _resolve_agent_dir(agent_name, project)
        if agent_dir:
            wf_dir = agent_dir / "workflows"
            if wf_dir.exists():
                sources.append(wf_dir)

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


def discover_roles(
    project_path: Path | str | None = None,
    agent_name: str | None = None,
) -> list[dict]:
    """List available agent roles from agent pack and project tiers."""
    roles: dict[str, dict] = {}

    if agent_name:
        agent_dir = _resolve_agent_dir(agent_name, Path(project_path) if project_path else None)
        if agent_dir:
            roles_dir = agent_dir / "roles"
            if roles_dir.is_dir():
                for md in sorted(roles_dir.glob("*.md")):
                    roles[md.stem] = {
                        "name": md.stem,
                        "source": agent_name,
                        "description": _extract_description(md),
                        "path": str(md),
                    }
    elif AGENTS_DIR.is_dir():
        for pack in sorted(AGENTS_DIR.iterdir()):
            if not pack.is_dir():
                continue
            roles_dir = pack / "roles"
            if roles_dir.is_dir():
                for md in sorted(roles_dir.glob("*.md")):
                    if md.stem not in roles:
                        roles[md.stem] = {
                            "name": md.stem,
                            "source": pack.name,
                            "description": _extract_description(md),
                            "path": str(md),
                        }

    if project_path:
        project_roles = Path(project_path) / ".modastack" / "roles"
        if project_roles.is_dir():
            for md in sorted(project_roles.glob("*.md")):
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
        source = "project" if r["source"] == "project" else "built-in"
        lines.append(f"  {r['name']:20s} [{source:8s}]  {r['description']}")
    return "\n".join(lines)


def validate_role(
    role_name: str,
    project_path: Path | str | None = None,
    agent_name: str | None = None,
) -> bool:
    """Check whether a role exists in any tier."""
    agent_dir = _resolve_agent_dir(agent_name, Path(project_path) if project_path else None)
    if agent_dir and (agent_dir / "roles" / f"{role_name}.md").exists():
        return True
    if project_path:
        project_role = Path(project_path) / ".modastack" / "roles" / f"{role_name}.md"
        if project_role.exists():
            return True
    if not agent_name and AGENTS_DIR.is_dir():
        for pack in AGENTS_DIR.iterdir():
            if pack.is_dir() and (pack / "roles" / f"{role_name}.md").exists():
                return True
    return False
