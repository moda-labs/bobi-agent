"""Resolve agent prompts from repo-provided .modastack/agents/ files."""

from pathlib import Path

from . import AGENT_BASE_PATH


def resolve_agent_prompt(
    agent_name: str,
    repo_path: Path | str,
    interactive: bool = True,
) -> str:
    """Build the full system prompt for an agent.

    Combines the framework agent base with the repo-specific agent role.
    """
    parts = [AGENT_BASE_PATH.read_text()]

    repo = Path(repo_path)
    agent_file = repo / ".modastack" / "agents" / f"{agent_name}.md"
    if agent_file.exists():
        parts.append(agent_file.read_text())

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
