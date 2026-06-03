"""Workflow orchestrator — deterministic state machine driving one agent session.

One Claude Code session persists across all steps. The agent accumulates
context as it progresses — what it learns in setup carries into pickup,
pickup insights carry into implement.

One registry entry per workflow. One log file. One session ID.

The orchestrator has no LLM — it is pure code. The agent does all the
work using its tools.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from modastack.sdk import (
    get_cli_path, get_registry, save_session_id, load_session_id,
    log_activity, SessionEntry,
)
from modastack.subagent import (
    HANDOFF_DIR,
    AgentResult,
    _emit_lifecycle_event,
    _parse_issue_number,
)
from modastack.workflow.schema import Workflow, StepDef
from modastack.workflow.variables import VariableContext

log = logging.getLogger(__name__)

MAX_HANDOFF_RETRIES = 2


def make_session_name(workflow_name: str, repo: str, issue_id: str) -> str:
    """Deterministic session name for a workflow run."""
    repo_name = repo.split("/")[-1] if "/" in repo else repo
    return f"wf-{workflow_name}-{repo_name}-{issue_id}"


def _setup_worktree(cwd: str, session_name: str) -> str:
    """Create a git worktree for the session and return its path.

    Worktrees live inside the repo at .claude/worktrees/<session_name>.
    If the worktree already exists, just return its path.
    """
    import subprocess as sp

    repo_root = Path(cwd).resolve()
    worktree_dir = repo_root / ".claude" / "worktrees" / session_name
    branch = session_name

    if worktree_dir.exists():
        return str(worktree_dir)

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    result = sp.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_dir)],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        result = sp.run(
            ["git", "worktree", "add", str(worktree_dir), branch],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning(f"Worktree creation failed: {result.stderr.strip()}")
            return str(repo_root)

    log.info(f"Created worktree at {worktree_dir} on branch {branch}")
    return str(worktree_dir)


def run_workflow(
    workflow: Workflow,
    task: str,
    repo: str,
    cwd: str,
    issue_id: str | None = None,
    requested_by: dict | None = None,
    timeout: int = 3600,
) -> bool:
    """Execute a workflow end-to-end with a single agent session."""
    issue_id = issue_id or _parse_issue_number(task) or "adhoc"
    requested_by = requested_by or {}
    started_at = time.time()

    # Clean stale handoff from previous runs
    old_handoff = HANDOFF_DIR / f"{issue_id}.md"
    if old_handoff.exists():
        old_handoff.unlink()
        log.info(f"Cleaned stale handoff at {old_handoff}")

    session_name = make_session_name(workflow.name, repo, issue_id)
    worktree_cwd = _setup_worktree(cwd, session_name)
    registry = get_registry()
    registry.register(SessionEntry(
        name=session_name, session_id="", role="engineer",
        issue_id=issue_id, title=task[:80], phase=workflow.name,
        repo=repo, cwd=worktree_cwd, status="running", pid=os.getpid(),
        requested_by=requested_by,
    ))

    _emit_lifecycle_event("engineer/workflow.started", {
        "issue_id": issue_id,
        "workflow": workflow.name,
        "repo": repo,
        "task": task[:500],
        "text": f"Workflow {workflow.name} started for {issue_id}",
    })

    ctx = VariableContext()
    ctx.set_scope("input", {"task": task, "repo": repo, "issue_id": issue_id})

    ctx.set_scope("worktree", {"path": worktree_cwd})

    success = asyncio.run(
        _run_workflow_async(
            workflow, task, repo, worktree_cwd, issue_id, session_name,
            registry, ctx, requested_by, timeout,
        )
    )

    duration = time.time() - started_at
    if success:
        _emit_lifecycle_event("engineer/workflow.completed", {
            "issue_id": issue_id,
            "workflow": workflow.name,
            "duration": round(duration, 1),
            "text": f"Workflow {workflow.name} completed for {issue_id} in {duration:.0f}s",
        }, blocking=True)
    else:
        _emit_lifecycle_event("engineer/workflow.failed", {
            "issue_id": issue_id,
            "workflow": workflow.name,
            "text": f"Workflow {workflow.name} failed for {issue_id}",
        }, blocking=True)

    # Clean up — registry only holds active workers
    registry.remove(session_name)

    log.info(f"Workflow {workflow.name} {'completed' if success else 'failed'} "
             f"in {duration:.0f}s")
    return success


async def _run_workflow_async(
    workflow: Workflow,
    task: str,
    repo: str,
    cwd: str,
    issue_id: str,
    session_name: str,
    registry,
    ctx: VariableContext,
    requested_by: dict,
    timeout: int,
) -> bool:
    """Async core: one ClaudeSDKClient session for all steps."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    saved_id = load_session_id(session_name)

    def _make_options(resume_id=None):
        return ClaudeAgentOptions(
            cwd=cwd,
            permission_mode="bypassPermissions",
            max_turns=200,
            cli_path=get_cli_path(),
            resume=resume_id,
            skills="all",
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": (
                    f"You are an engineer agent working on issue #{issue_id}. "
                    f"Your working directory is an isolated git worktree at {cwd}. "
                    f"All code changes go here — never modify the main repo checkout. "
                    f"You will receive step-by-step instructions. Follow each one, "
                    f"then write your handoff file when asked."
                ),
            },
        )

    _emit_lifecycle_event("engineer/session.started", {
        "issue_id": issue_id, "repo": repo,
        "text": f"Engineer started working on {issue_id}",
    })

    # Try resume, fall back to fresh session
    for attempt in range(2):
        resume_id = saved_id if attempt == 0 else None
        client = ClaudeSDKClient(_make_options(resume_id))
        try:
            initial_prompt = task if not resume_id else None
            await client.connect(initial_prompt)
            if resume_id:
                await client.query(task)
            await _drain_response(client, session_name, issue_id)
            break
        except Exception as e:
            if resume_id and attempt == 0:
                log.warning(f"Resume failed (stale session?), retrying fresh: {e}")
                save_session_id(session_name, "")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                continue
            raise

    try:

        registry.update(session_name, status="running",
                        session_id=saved_id or "")

        step_idx = 0
        failed_step = ""

        while step_idx < len(workflow.steps):
            step = workflow.steps[step_idx]

            # Route step — deterministic, no LLM
            if step.condition:
                taken = ctx.evaluate_condition(step.condition)
                target = step.goto if taken else step.else_goto
                log.info(f"Route {step.name}: {step.condition} → {target}")
                if target:
                    jump = workflow.step_index(target)
                    if jump >= 0:
                        step_idx = jump
                        continue
                step_idx += 1
                continue

            # Await step — suspend
            if step.await_event:
                log.info(f"Await step {step.name}: suspended")
                registry.update(session_name, status="waiting", phase=step.name)
                return True

            # Prompt step — inject into the persistent session
            step_start = time.time()
            registry.update(session_name, phase=step.name)

            _emit_lifecycle_event("engineer/step.started", {
                "issue_id": issue_id,
                "workflow": workflow.name,
                "step": step.name,
                "repo": repo,
                "text": f"Step {step.name} started",
            })

            prompt = _build_step_prompt(step, ctx)
            log.info(f"Step {step.name}: injecting prompt ({len(prompt)} chars)")

            await client.query(prompt)
            final_text = await _drain_response(client, session_name, issue_id)

            if final_text is None:
                failed_step = step.name
                _emit_step_failed(issue_id, workflow.name, step.name,
                                  "connection lost")
                return False

            # Validate handoff
            handoff = _read_handoff(issue_id)
            missing = _validate_handoff(step, handoff)

            for retry in range(MAX_HANDOFF_RETRIES):
                if not missing:
                    break
                log.warning(f"Step {step.name}: handoff missing {missing}, re-prompting")
                fix_prompt = (
                    f"Your handoff is missing required fields: {', '.join(missing)}. "
                    f"Please update your handoff file with these fields and confirm."
                )
                await client.query(fix_prompt)
                await _drain_response(client, session_name, issue_id)
                handoff = _read_handoff(issue_id)
                missing = _validate_handoff(step, handoff)

            if missing:
                failed_step = step.name
                error = f"Handoff missing required fields after retries: {missing}"
                _emit_step_failed(issue_id, workflow.name, step.name, error)
                return False

            # Capture outputs for routing
            outputs = {k: handoff.get(k, "") for k in
                       step.handoff.required + step.handoff.optional
                       if k in handoff}
            ctx.set_scope(step.name, outputs)
            for k, v in outputs.items():
                ctx.set_flat(k, v)

            duration = time.time() - step_start
            _emit_lifecycle_event("engineer/step.completed", {
                "issue_id": issue_id,
                "workflow": workflow.name,
                "step": step.name,
                "outputs": outputs,
                "duration": round(duration, 1),
                "text": f"Step {step.name} completed in {duration:.0f}s",
            })
            log.info(f"Step {step.name} completed ({duration:.0f}s): {outputs}")

            step_idx += 1

        return True

    except Exception as e:
        log.error(f"Workflow error: {e}")
        _emit_lifecycle_event("engineer/workflow.failed", {
            "issue_id": issue_id,
            "workflow": workflow.name,
            "error": str(e),
            "text": f"Workflow error: {e}",
        }, blocking=True)
        return False
    finally:
        _emit_lifecycle_event("engineer/session.completed", {
            "issue_id": issue_id, "repo": repo,
            "text": f"Engineer finished {issue_id}",
        }, blocking=True)
        try:
            await client.disconnect()
        except Exception:
            pass


async def _drain_response(client, session_name: str, issue_id: str) -> str | None:
    """Drain one turn of the agent's response. Returns final text or None."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    final_text = ""
    try:
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                if text_parts:
                    final_text = "\n".join(text_parts)
                    log_activity("response", {"text": final_text[:500]},
                                 session=session_name)
            elif isinstance(msg, ResultMessage):
                save_session_id(session_name, msg.session_id)
                log_activity("stop", {"session_id": msg.session_id},
                             session=session_name)
                return final_text
    except Exception as e:
        log.error(f"Drain error: {e}")
    return None


def _emit_step_failed(issue_id, workflow_name, step_name, error):
    _emit_lifecycle_event("engineer/step.failed", {
        "issue_id": issue_id,
        "workflow": workflow_name,
        "step": step_name,
        "error": error,
        "text": f"Step {step_name} failed: {error}",
    }, blocking=True)


def _build_step_prompt(step: StepDef, ctx: VariableContext) -> str:
    """Build the full prompt for a step, including handoff contract."""
    prompt = ctx.resolve(step.prompt)

    if step.handoff.required or step.handoff.optional:
        prompt += "\n\nWhen complete, write your handoff file with:"
        for field in step.handoff.required:
            prompt += f"\n- `{field}` (required)"
        for field in step.handoff.optional:
            prompt += f"\n- `{field}` (optional)"

    return prompt


def _read_handoff(issue_id: str) -> dict:
    """Read the handoff YAML for an issue."""
    candidates = [
        HANDOFF_DIR / f"{issue_id.lower()}.md",
        HANDOFF_DIR / f"{issue_id}.md",
    ]
    for path in candidates:
        if path.exists():
            content = path.read_text()
            if content.startswith("---"):
                try:
                    end = content.index("---", 3)
                    return yaml.safe_load(content[3:end]) or {}
                except (ValueError, yaml.YAMLError):
                    pass
    return {}


def _validate_handoff(step: StepDef, handoff: dict) -> list[str]:
    """Return list of missing required fields."""
    return [f for f in step.handoff.required if f not in handoff]
