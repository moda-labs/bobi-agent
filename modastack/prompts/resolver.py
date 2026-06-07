"""Resolve agent prompts: base + agent pack role + tools + project override."""

from __future__ import annotations

import logging
from pathlib import Path

from . import BASE_PATH, AGENTS_CACHE_DIR, BUILTIN_AGENTS_DIR, PROMPTS_DIR

log = logging.getLogger(__name__)


def _resolve_role_prompt(role: str, agent_dir: Path | None, project: Path | None) -> str | None:
    """Find the role prompt.

    Resolution order:
      1. <project>/.modastack/roles/{role}/ROLE.md — project override
      2. Agent pack roles/{role}/ROLE.md           — from resolved agent pack
      3. Built-in: modastack/prompts/agents/{role}/ROLE.md — framework-shipped
    """
    if project:
        folder = project / ".modastack" / "roles" / role / "ROLE.md"
        if folder.exists():
            return folder.read_text()

    if agent_dir:
        folder = agent_dir / "roles" / role / "ROLE.md"
        if folder.exists():
            return folder.read_text()

    builtin = BUILTIN_AGENTS_DIR / role / "ROLE.md"
    if builtin.exists():
        return builtin.read_text()

    return None


def _resolve_tools(agent_dir: Path | None, project: Path | None) -> str:
    """Load all tool markdown files from the pack and project overrides.

    Tools are service interaction guides (e.g. gmail.md, jira.md) that
    describe how to interact with external services.

    Project-level tools override pack tools with the same filename.
    """
    tools: dict[str, str] = {}

    if agent_dir:
        tools_dir = agent_dir / "tools"
        if tools_dir.is_dir():
            for md in sorted(tools_dir.glob("*.md")):
                tools[md.stem] = md.read_text()

    if project:
        project_tools = project / ".modastack" / "tools"
        if project_tools.is_dir():
            for md in sorted(project_tools.glob("*.md")):
                tools[md.stem] = md.read_text()

    if not tools:
        return ""

    parts = ["## Tools\n"]
    for name, content in sorted(tools.items()):
        parts.append(f"### {name}\n\n{content.strip()}")
    return "\n\n".join(parts)


def _resolve_agent_dir(agent_name: str | None, project_path: Path | None = None) -> Path | None:
    """Find the agent pack directory.

    Resolution order:
      1. <project>/agents/{name}             — project-level (visible)
      2. <project>/.modastack/agents/{name}  — project override (hidden)
      3. ~/.modastack/agents/{name}          — user cache (fetched from remote)
    """
    if not agent_name:
        return None
    if project_path:
        project = Path(project_path)
        visible = project / "agents" / agent_name
        if visible.is_dir():
            return visible
        hidden = project / ".modastack" / "agents" / agent_name
        if hidden.is_dir():
            return hidden
    d = AGENTS_CACHE_DIR / agent_name
    return d if d.is_dir() else None


def resolve_agent_prompt(
    role: str,
    project_path: Path | str,
    agent_name: str | None = None,
    interactive: bool = True,
) -> str:
    """Build the full prompt for an agent with a given role.

    Assembly order:
      1. Base framework prompt
      2. Role prompt (folder or flat, project override or pack)
      3. Tools (service interaction guides from pack + project)
      4. Interactive/non-interactive notice
    """
    parts = [BASE_PATH.read_text()]

    project = Path(project_path)
    agent_dir = _resolve_agent_dir(agent_name, project)

    role_prompt = _resolve_role_prompt(role, agent_dir, project)
    if role_prompt:
        parts.append(role_prompt)

    tools_section = _resolve_tools(agent_dir, project)
    if tools_section:
        parts.append(tools_section)

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
                    trigger = getattr(wf.trigger, 'event', None) or str(wf.trigger)[:60]
                    lines.append(f"- {wf.name}: {trigger}")
                except Exception:
                    continue
        return "\n".join(lines) if lines else "No workflows found."
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Role discovery
# ---------------------------------------------------------------------------

def _resolve_role_path(role_name: str, roles_dir: Path) -> Path | None:
    """Find the role prompt file in a roles/ directory."""
    folder = roles_dir / role_name / "ROLE.md"
    if folder.exists():
        return folder
    return None


def _discover_roles_in_dir(roles_dir: Path) -> list[tuple[str, Path]]:
    """Discover all roles in a roles/ directory."""
    found: list[tuple[str, Path]] = []
    if not roles_dir.is_dir():
        return found
    for entry in sorted(roles_dir.iterdir()):
        if entry.is_dir():
            role_file = entry / "ROLE.md"
            if role_file.exists():
                found.append((entry.name, role_file))
    return found


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
            for name, path in _discover_roles_in_dir(agent_dir / "roles"):
                roles[name] = {
                    "name": name,
                    "source": agent_name,
                    "description": _extract_description(path),
                    "path": str(path),
                }
    elif AGENTS_CACHE_DIR.is_dir():
        for pack in sorted(AGENTS_CACHE_DIR.iterdir()):
            if not pack.is_dir():
                continue
            for name, path in _discover_roles_in_dir(pack / "roles"):
                if name not in roles:
                    roles[name] = {
                        "name": name,
                        "source": pack.name,
                        "description": _extract_description(path),
                        "path": str(path),
                    }

    if project_path:
        project = Path(project_path)
        for agents_dir in [project / "agents", project / ".modastack" / "agents"]:
            if agents_dir.is_dir():
                for pack in sorted(agents_dir.iterdir()):
                    if not pack.is_dir():
                        continue
                    for name, path in _discover_roles_in_dir(pack / "roles"):
                        if name not in roles:
                            roles[name] = {
                                "name": name,
                                "source": pack.name,
                                "description": _extract_description(path),
                                "path": str(path),
                            }

        project_roles = project / ".modastack" / "roles"
        for name, path in _discover_roles_in_dir(project_roles):
            roles[name] = {
                "name": name,
                "source": "project",
                "description": _extract_description(path),
                "path": str(path),
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
    if agent_dir and _resolve_role_path(role_name, agent_dir / "roles"):
        return True
    if project_path:
        project = Path(project_path)
        project_roles = project / ".modastack" / "roles"
        if _resolve_role_path(role_name, project_roles):
            return True
        for agents_dir in [project / "agents", project / ".modastack" / "agents"]:
            if agents_dir.is_dir():
                for pack in agents_dir.iterdir():
                    if pack.is_dir() and _resolve_role_path(role_name, pack / "roles"):
                        return True
    if not agent_name and AGENTS_CACHE_DIR.is_dir():
        for pack in AGENTS_CACHE_DIR.iterdir():
            if pack.is_dir() and _resolve_role_path(role_name, pack / "roles"):
                return True
    if _resolve_role_path(role_name, BUILTIN_AGENTS_DIR):
        return True
    return False
