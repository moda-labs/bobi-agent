"""Sub-agent executor — runs engineer phases as Claude Code sessions.

Each engineer gets a persistent ClaudeSDKClient session tracked in the
registry. Sessions survive restarts and can be resumed, interacted with
from the dashboard, or cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modastack.sdk import (
    get_cli_path, save_session_id, load_session_id, log_activity,
    get_registry, SessionEntry,
)

log = logging.getLogger(__name__)

ROLES_DIR = Path(__file__).parent.parent / "roles" / "engineer" / "process"
HANDOFF_DIR = Path.home() / ".modastack" / "handoffs"

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
    client: Any = None


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


def _session_name(issue_id: str) -> str:
    return f"eng-{issue_id.lower()}"


async def _run_agent(
    prompt: str,
    cwd: str,
    issue_id: str,
    phase: str,
    timeout: int,
    max_budget_usd: float | None = None,
) -> AgentResult:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    name = _session_name(issue_id)
    saved_id = load_session_id(name)
    registry = get_registry()

    options = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="bypassPermissions",
        max_turns=200,
        cli_path=get_cli_path(),
        resume=saved_id or None,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                f"You are an engineer agent working on issue #{issue_id}, "
                f"phase: {phase}. Follow the skill file instructions exactly."
            ),
        },
    )

    client = ClaudeSDKClient(options)
    key = issue_id.lower()
    if key in _running:
        _running[key].client = client

    registry.register(SessionEntry(
        name=name, session_id=saved_id or "", role="engineer",
        issue_id=issue_id, phase=phase, cwd=cwd, status="running",
    ))

    result = AgentResult(
        session_id="", issue_id=issue_id, phase=phase, success=False,
    )

    try:
        connect_prompt = prompt if not saved_id else None
        await client.connect(connect_prompt)

        if saved_id:
            await client.query(prompt)

        async for msg in client.receive_messages():
            if isinstance(msg, AssistantMessage):
                text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                if text_parts:
                    log_activity("eng_response", {
                        "issue_id": issue_id,
                        "phase": phase,
                        "text": "\n".join(text_parts)[:500],
                    })

            elif isinstance(msg, ResultMessage):
                save_session_id(name, msg.session_id)
                result.session_id = msg.session_id
                result.success = not msg.is_error
                result.duration_ms = msg.duration_ms
                result.total_cost_usd = msg.total_cost_usd or 0.0
                result.num_turns = msg.num_turns
                if msg.is_error:
                    result.error = msg.result or "unknown error"
                registry.update(name, status="done", session_id=msg.session_id)
    except asyncio.TimeoutError:
        result.error = f"timeout after {timeout}s"
        registry.update(name, status="error")
    except Exception as e:
        result.error = str(e)
        registry.update(name, status="error")
        log.error(f"Sub-agent error for {issue_id}/{phase}: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return result


def run_phase(
    issue_id: str,
    phase: str,
    cwd: str,
    context: str = "",
    max_budget_usd: float | None = None,
) -> str:
    key = issue_id.lower()
    if key in _running and not _running[key].task.done():
        log.warning(f"Agent already running for {key}, skipping")
        return key

    prompt = _build_prompt(phase, issue_id, context)
    timeout = PHASE_TIMEOUT.get(phase, 1800)

    name = _session_name(issue_id)
    registry = get_registry()
    registry.register(SessionEntry(
        name=name, session_id="", role="engineer",
        issue_id=issue_id, phase=phase, cwd=cwd, status="starting",
    ))

    loop = _ensure_loop()

    async def _wrapped():
        try:
            return await asyncio.wait_for(
                _run_agent(prompt, cwd, issue_id, phase, timeout, max_budget_usd),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            registry.update(name, status="error")
            return AgentResult(
                session_id="",
                issue_id=issue_id,
                phase=phase,
                success=False,
                error=f"timeout after {timeout}s",
            )

    future = asyncio.run_coroutine_threadsafe(_wrapped(), loop)
    _running[key] = RunningAgent(
        issue_id=issue_id,
        phase=phase,
        session_id="",
        task=future,
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
    prompt = _build_prompt(phase, issue_id, context)
    timeout = PHASE_TIMEOUT.get(phase, 1800)
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(
        asyncio.wait_for(
            _run_agent(prompt, cwd, issue_id, phase, timeout, max_budget_usd),
            timeout=timeout,
        ),
        loop,
    )
    return future.result(timeout=timeout + 10)


def inject_message(issue_id: str, text: str) -> bool:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent or not agent.client:
        log.warning(f"No running agent for {key}")
        return False
    try:
        loop = _ensure_loop()
        future = asyncio.run_coroutine_threadsafe(agent.client.query(text), loop)
        future.result(timeout=10)
        return True
    except Exception as e:
        log.error(f"Inject to {key} failed: {e}")
        return False


def is_running(issue_id: str) -> bool:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent:
        return False
    return not agent.task.done()


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
    name = _session_name(issue_id)
    get_registry().update(name, status="cancelled")
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


_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is not None and _loop.is_running():
        return _loop

    _loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _loop_thread = threading.Thread(target=_run, daemon=True, name="subagent-loop")
    _loop_thread.start()

    # Wait until the loop is actually running
    while not _loop.is_running():
        time.sleep(0.01)

    return _loop
