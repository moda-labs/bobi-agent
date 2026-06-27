"""Resolve agent prompts: base + installed package role + tools."""

from __future__ import annotations

import logging
from pathlib import Path

from bobi import paths

from . import BASE_PATH, PROMPTS_DIR

log = logging.getLogger(__name__)


def _resolve_role_prompt(role: str, project: Path | None) -> str | None:
    """Find the role prompt at <run>/package/roles/{role}/ROLE.md."""
    if project:
        installed = paths.roles_dir(project) / role / "ROLE.md"
        if installed.exists():
            return installed.read_text()
    return None


def _resolve_tools(project: Path | None) -> str:
    """Load all tool markdown files from <run>/package/tools/.

    Tools are service interaction guides (e.g. gmail.md, jira.md) that
    describe how to interact with external services.
    """
    tools: dict[str, str] = {}

    if project:
        tools_dir = paths.tools_dir(project)
        if tools_dir.is_dir():
            for md in sorted(tools_dir.glob("*.md")):
                tools[md.stem] = md.read_text()

    if not tools:
        return ""

    parts = ["## Tools\n"]
    for name, content in sorted(tools.items()):
        parts.append(f"### {name}\n\n{content.strip()}")
    return "\n\n".join(parts)


def _first_line(path: Path) -> str:
    """First non-empty line of a file, stripped of markdown heading marks."""
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _resolve_context_index(project: Path | None) -> str:
    """Index the installed <run>/package/context/ files.

    Context files are pack-shipped reference content agents read on
    demand. Only the index goes into the prompt — never the contents.
    """
    if not project:
        return ""
    context_dir = paths.context_dir(project)
    if not context_dir.is_dir():
        return ""
    lines = []
    for f in sorted(context_dir.rglob("*")):
        if f.is_file():
            rel = f.relative_to(project).as_posix()
            desc = _first_line(f)
            lines.append(f"- `{rel}`" + (f" — {desc}" if desc else ""))
    if not lines:
        return ""
    return (
        "## Context files\n\n"
        "Reference files shipped with this agent team. Read them when "
        "relevant — they are not loaded automatically.\n\n"
        + "\n".join(lines)
    )


def _resolve_workspace_note(project: Path | None) -> str:
    """Point agents at the selected runtime workspace/ directory if it exists."""
    if not project:
        return ""
    workspace = paths.workspace_dir(project)
    if not workspace.is_dir():
        return ""
    return (
        "## Workspace\n\n"
        f"`{workspace}` holds domain files and work products. Your role "
        "prompt defines what lives there."
    )


def resolve_agent_prompt(
    role: str,
    project_path: Path | str,
    agent_name: str | None = None,
    interactive: bool = True,
) -> str:
    """Build the full prompt for an agent with a given role.

    Assembly order:
      1. Base framework prompt
      2. Role prompt from the installed package
      3. Tools from the installed package
      4. Context file index + workspace note
      5. Interactive/non-interactive notice
    """
    parts = [BASE_PATH.read_text()]

    project = Path(project_path)

    role_prompt = _resolve_role_prompt(role, project)
    if role_prompt:
        parts.append(role_prompt)

    tools_section = _resolve_tools(project)
    if tools_section:
        parts.append(tools_section)

    for section in (_resolve_context_index(project),
                    _resolve_workspace_note(project)):
        if section:
            parts.append(section)

    try:
        slot_name = paths.agent_name_for_root(project)
    except Exception:
        slot_name = agent_name or "<agent>"
    ask_cmd = f'bobi agent {slot_name} ask "question"'
    if interactive:
        parts.append(
            f"You can use `{ask_cmd}` to ask for guidance "
            "on ambiguous decisions."
        )
    else:
        parts.append(
            "You are running in non-interactive mode. Make your best judgment "
            f"on all decisions — do not use `{ask_cmd}`."
        )

    return "\n\n".join(parts)


def build_startup_prompt(
    role: str,
    project_path: Path | str,
    agent_name: str | None = None,
    session_name: str | None = None,
) -> str:
    """Build the startup prompt for a persistent agent."""
    prompt = resolve_agent_prompt(role, project_path, agent_name=agent_name, interactive=True)
    workflows = list_workflows(project_path, agent_name=agent_name)

    project = Path(project_path)

    # Team policy is team-scoped (#456) — injected for every agent regardless of
    # session. session_name is retained in the signature for back-compat but no
    # longer selects a per-session journal.
    policy_section = _load_policy_section(project)

    try:
        slot_name = paths.agent_name_for_root(project)
    except Exception:
        slot_name = agent_name or project.name

    parts = [
        f"You are a bobi {role} for {slot_name}. "
        f"Act directly using your tools.\n\n{prompt}",
    ]
    if policy_section:
        parts.append(policy_section)
    parts.append(f"## Available workflows\n\n{workflows}")
    return "\n\n".join(parts)


def _load_policy_section(project: Path) -> str:
    """Load the team policy.md and format it read-only for prompt injection (#456)."""
    try:
        from bobi.memory import load_policy, format_policy_prompt
        content = load_policy(paths.state_path(project))
        return format_policy_prompt(content)
    except Exception:
        log.debug("Failed to load policy for %s", project, exc_info=True)
        return ""


def list_workflows(project_path: Path | str, agent_name: str | None = None) -> str:
    """List available workflows as a formatted string for agent prompts.

    Delegates to WorkflowDispatcher so agents see the same menu (same
    tiers, dedup, and priority) as `bobi agent <name> workflows list`.
    """
    try:
        from bobi.workflow.triggers import WorkflowDispatcher

        dispatcher = WorkflowDispatcher()
        dispatcher.load_all_workflows(Path(project_path), agent_name=agent_name)
        return dispatcher.format_workflow_menu()
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
    """List available agent roles from <run>/package/roles/."""
    roles: dict[str, dict] = {}

    if project_path:
        project = Path(project_path)
        installed_roles = paths.roles_dir(project)
        for name, path in _discover_roles_in_dir(installed_roles):
            roles[name] = {
                "name": name,
                "source": agent_name or "installed",
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
        source = "installed"
        lines.append(f"  {r['name']:20s} [{source:8s}]  {r['description']}")
    return "\n".join(lines)


def validate_role(
    role_name: str,
    project_path: Path | str | None = None,
    agent_name: str | None = None,
) -> bool:
    """Check whether a role exists in <run>/package/roles/."""
    if project_path:
        project = Path(project_path)
        installed_roles = paths.roles_dir(project)
        if _resolve_role_path(role_name, installed_roles):
            return True
    return False
