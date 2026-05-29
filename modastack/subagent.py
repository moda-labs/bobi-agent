"""Sub-agent executor — runs engineer phases as Claude Code sub-agents.

Replaces tmux-based session management with the claude-agent-sdk.
Each phase (pickup, spec, implement, prepare-pr, feedback) runs as
an independent sub-agent with its own session. The manager stays
responsive while sub-agents work in the background.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROLES_DIR = Path(__file__).parent.parent / "roles" / "engineer" / "process"
HANDOFF_DIR = Path.home() / ".modastack" / "handoffs"
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"

PHASE_TIMEOUT = {
    "pickup": 600,
    "triage": 1200,
    "spec": 3000,
    "implement": 3600,
    "prepare-pr": 600,
    "feedback": 1200,
}


@dataclass
class AgentResult:
    session_id: str
    issue_id: str
    phase: str
    success: bool
    duration_ms: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    error: str = ""


@dataclass
class RunningAgent:
    issue_id: str
    phase: str
    session_id: str
    task: asyncio.Task[AgentResult]
    started_at: float = field(default_factory=time.time)
    cwd: str = ""


_running: dict[str, RunningAgent] = {}


def _resolve_skill_path(phase: str) -> Path | None:
    skill_dir = ROLES_DIR / phase
    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        return skill_file
    return None


def _build_prompt(phase: str, issue_id: str, context: str = "") -> str:
    skill_path = _resolve_skill_path(phase)
    parts = []
    if skill_path:
        parts.append(
            f"Read and follow the skill file at {skill_path}. "
            f"Execute every step exactly as written."
        )
    parts.append(f"Issue: #{issue_id}")
    if context:
        parts.append(context)
    parts.append(
        f"After completing this phase, update the handoff file at "
        f"{HANDOFF_DIR / f'{issue_id.lower()}.md'} with your results."
    )
    return "\n\n".join(parts)


async def _run_agent(
    prompt: str,
    cwd: str,
    issue_id: str,
    phase: str,
    timeout: int,
    max_budget_usd: float | None = None,
) -> AgentResult:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="bypassPermissions",
        max_turns=200,
        max_budget_usd=max_budget_usd or 5.0,
        cli_path=CLAUDE,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                f"You are an engineer agent working on issue #{issue_id}, "
                f"phase: {phase}. Follow the skill file instructions exactly."
            ),
        },
    )

    session_id = ""
    result = AgentResult(
        session_id="", issue_id=issue_id, phase=phase, success=False,
    )

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                session_id = msg.session_id
                result.session_id = session_id
                result.success = not msg.is_error
                result.duration_ms = msg.duration_ms
                result.total_cost_usd = msg.total_cost_usd or 0.0
                result.num_turns = msg.num_turns
                if msg.is_error:
                    result.error = msg.result or "unknown error"
    except asyncio.TimeoutError:
        result.error = f"timeout after {timeout}s"
    except Exception as e:
        result.error = str(e)
        log.error(f"Sub-agent error for {issue_id}/{phase}: {e}")

    return result


def run_phase(
    issue_id: str,
    phase: str,
    cwd: str,
    context: str = "",
    max_budget_usd: float | None = None,
) -> str:
    """Start a sub-agent for a workflow phase. Returns immediately.

    The agent runs in the background. Use is_running() and get_result()
    to check status.

    Returns the agent key (issue_id).
    """
    key = issue_id.lower()
    if key in _running:
        log.warning(f"Agent already running for {key}, skipping")
        return key

    prompt = _build_prompt(phase, issue_id, context)
    timeout = PHASE_TIMEOUT.get(phase, 1800)

    loop = _get_or_create_loop()

    async def _wrapped():
        try:
            return await asyncio.wait_for(
                _run_agent(prompt, cwd, issue_id, phase, timeout, max_budget_usd),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return AgentResult(
                session_id="",
                issue_id=issue_id,
                phase=phase,
                success=False,
                error=f"timeout after {timeout}s",
            )

    task = loop.create_task(_wrapped())
    _running[key] = RunningAgent(
        issue_id=issue_id,
        phase=phase,
        session_id="",
        task=task,
        cwd=cwd,
    )
    log.info(f"Sub-agent started: {issue_id}/{phase} in {cwd}")
    return key


def run_phase_sync(
    issue_id: str,
    phase: str,
    cwd: str,
    context: str = "",
    max_budget_usd: float | None = None,
) -> AgentResult:
    """Run a sub-agent phase synchronously. Blocks until complete."""
    prompt = _build_prompt(phase, issue_id, context)
    timeout = PHASE_TIMEOUT.get(phase, 1800)
    loop = _get_or_create_loop()
    return loop.run_until_complete(
        asyncio.wait_for(
            _run_agent(prompt, cwd, issue_id, phase, timeout, max_budget_usd),
            timeout=timeout,
        )
    )


def is_running(issue_id: str) -> bool:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent:
        return False
    if agent.task.done():
        return False
    return True


def get_result(issue_id: str) -> AgentResult | None:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent:
        return None
    if not agent.task.done():
        return None
    try:
        result = agent.task.result()
        del _running[key]
        return result
    except Exception as e:
        del _running[key]
        return AgentResult(
            session_id="",
            issue_id=issue_id,
            phase=agent.phase,
            success=False,
            error=str(e),
        )


def cancel_agent(issue_id: str) -> bool:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent:
        return False
    agent.task.cancel()
    del _running[key]
    log.info(f"Sub-agent cancelled: {issue_id}/{agent.phase}")
    return True


def list_agents() -> list[dict[str, Any]]:
    result = []
    for key, agent in list(_running.items()):
        done = agent.task.done()
        elapsed = time.time() - agent.started_at
        result.append({
            "issue_id": agent.issue_id,
            "phase": agent.phase,
            "cwd": agent.cwd,
            "running": not done,
            "elapsed_s": int(elapsed),
        })
    return result


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop
