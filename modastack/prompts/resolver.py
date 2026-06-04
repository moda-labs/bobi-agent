"""Resolve agent prompts: built-in defaults + repo overrides."""

from pathlib import Path

from . import AGENT_BASE_PATH, AGENTS_DIR


def resolve_agent_prompt(
    agent_name: str,
    repo_path: Path | str,
    interactive: bool = True,
) -> str:
    """Build the full system prompt for an agent.

    Resolution: framework base + built-in role + repo override.
    Repo override replaces the built-in role if present.
    """
    parts = [AGENT_BASE_PATH.read_text()]

    repo = Path(repo_path)
    repo_agent = repo / ".modastack" / "agents" / f"{agent_name}.md"
    builtin_agent = AGENTS_DIR / f"{agent_name}.md"

    if repo_agent.exists():
        parts.append(repo_agent.read_text())
    elif builtin_agent.exists():
        parts.append(builtin_agent.read_text())

    if interactive:
        parts.append(
            "You can use `modastack consult \"question\"` to ask the manager "
            "for guidance on ambiguous decisions."
        )
    else:
        parts.append(
            "You are running in non-interactive mode. Make your best judgment "
            "on all decisions — do not use `modastack consult`."
        )

    return "\n\n".join(parts)
