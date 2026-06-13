"""System prompt composition for the setup session.

The stage machine lives in prompts/setup.md; the authoring reference is
the packaged create-agent skill, appended verbatim so the two never
drift apart. setup.md documents process, the skill documents formats —
neither duplicates the other.
"""

from __future__ import annotations

from pathlib import Path

from modastack.prompts import PROMPTS_DIR
from modastack.setup.state import SetupState

SETUP_PROMPT_PATH = PROMPTS_DIR / "setup.md"


def _create_agent_guide() -> str:
    import modastack
    guide = Path(modastack.__file__).parent / "skills" / "create-agent.md"
    return guide.read_text()


def build_setup_prompt() -> str:
    return SETUP_PROMPT_PATH.read_text() + "\n" + _create_agent_guide()


def kickoff_prompt(project: Path, state: SetupState, resumed: bool = False) -> str:
    """The first message of the session — project context, then go."""
    from modastack.setup.tools import installed_team_name

    lines = [
        f"Setup is starting in {project}.",
    ]

    installed = installed_team_name(project)
    if installed:
        lines.append(
            f"Note: '{installed}' is already installed in .modastack/ — "
            "the user chose to replace it; acknowledge that before the "
            "choose stage.")

    if resumed:
        lines.append(
            "This RESUMES an interrupted setup. Current state: "
            f"stage='{state.stage.value}', branch='{state.branch or 'undecided'}', "
            f"team='{state.team_name or 'unnamed'}'.")
        if state.answers:
            recorded = ", ".join(f"{k}={v!r}" for k, v in state.answers.items())
            lines.append(f"Recorded interview answers: {recorded}.")
        if state.monitors_recorded:
            lines.append(f"{len(state.monitors_recorded)} monitor(s) already "
                         "recorded during discovery.")
        if state.stage_summaries:
            done = "; ".join(f"{k}: {v}" for k, v in state.stage_summaries.items())
            lines.append(f"Stage summaries so far: {done}")
        lines.append("Recap where things stand for the user, then continue "
                     "from the current stage.")
    else:
        lines.append("Greet the user briefly, then start the choose stage.")

    return "\n\n".join(lines)
