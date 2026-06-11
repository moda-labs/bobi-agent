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
    log_activity, SessionEntry, SessionRegistry,
)
from modastack.subagent import (
    AgentResult,
    _emit_lifecycle_event,
    _parse_issue_number,
)
from modastack.workflow.schema import Workflow, StepDef
from modastack.workflow.state import WorkflowRun
from modastack.workflow.variables import VariableContext

log = logging.getLogger(__name__)

MAX_HANDOFF_RETRIES = 2


def try_resume_for_event(event_type: str, issue_id: str = "", event: dict | None = None) -> bool:
    """Check if any suspended workflow is waiting for this event type and resume it.

    Called by the manager when it receives an event that might unblock a workflow.
    Returns True if a workflow was resumed.
    """
    from modastack.workflow.triggers import WorkflowDispatcher

    run = WorkflowRun.find_waiting(event_type, issue_id)
    if not run:
        return False

    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    wf = dispatcher.find_workflow(run.workflow_name)
    if not wf:
        log.error(f"Cannot resume run {run.run_id}: workflow '{run.workflow_name}' not found")
        return False

    log.info(f"Resuming workflow {run.workflow_name} for {run.issue_id} "
             f"(run {run.run_id}, awaited '{event_type}')")

    import threading
    t = threading.Thread(
        target=resume_workflow,
        args=(run, wf),
        kwargs={"event": event},
        daemon=True,
        name=f"resume-{run.run_id}",
    )
    t.start()
    return True


def _find_project_root(cwd: str) -> Path:
    """Return the project root — the directory modastack was started in."""
    from modastack.sdk import get_project_root
    return get_project_root() or Path(cwd)


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
            raise RuntimeError(
                f"Failed to create worktree for {session_name}: "
                f"{result.stderr.strip()}"
            )

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
    interactive: bool = True,
    role: str = "engineer",
) -> bool:
    """Execute a workflow end-to-end with a single agent session."""
    issue_id = issue_id or _parse_issue_number(task) or "adhoc"
    requested_by = requested_by or {}
    started_at = time.time()

    # Session dir is created by the registry on register

    session_name = make_session_name(workflow.name, repo, issue_id)
    needs_worktree = any(s.worktree for s in workflow.steps)
    work_cwd = _setup_worktree(cwd, session_name) if needs_worktree else cwd
    from modastack.sdk import compute_manifest_hash
    registry = get_registry()
    registry.register(SessionEntry(
        name=session_name, session_id="", role=role,
        issue_id=issue_id, title=task[:80], phase=workflow.name,
        project=repo, cwd=work_cwd, status="running", pid=os.getpid(),
        requested_by=requested_by,
        image_hash=compute_manifest_hash(Path(cwd)),
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

    if needs_worktree:
        ctx.set_scope("worktree", {"path": work_cwd})

    success = asyncio.run(
        _run_workflow_async(
            workflow, task, repo, work_cwd, issue_id, session_name,
            registry, ctx, requested_by, timeout, interactive, role=role,
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

    registry.mark_done(session_name)

    log.info(f"Workflow {workflow.name} {'completed' if success else 'failed'} "
             f"in {duration:.0f}s")
    return success


def resume_workflow(
    run: WorkflowRun,
    workflow: Workflow,
    event: dict | None = None,
    timeout: int = 3600,
    interactive: bool = True,
) -> bool:
    """Resume a suspended workflow from its await step.

    Restores the variable context and session, then continues execution
    from the step after the one that suspended.
    """
    session_name = run.session_name
    issue_id = run.issue_id
    repo = run.repo
    cwd = run.cwd
    step_idx = run.suspended_at_step
    started_at = time.time()

    registry = get_registry()
    registry.update(session_name, status="running", phase=f"resuming")

    ctx = VariableContext()
    ctx.scopes = run.variable_scopes

    if event:
        ctx.set_scope("event", event.get("data", {}))

    run.status = "running"
    run.await_event = ""
    run.suspended_at_step = -1
    run.resumed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    run.save()

    _emit_lifecycle_event("engineer/workflow.resumed", {
        "issue_id": issue_id,
        "workflow": workflow.name,
        "run_id": run.run_id,
        "resume_step": workflow.steps[step_idx].name if step_idx < len(workflow.steps) else "end",
        "text": f"Workflow {workflow.name} resumed for {issue_id}",
    })

    success = asyncio.run(
        _run_workflow_async(
            workflow, f"Resuming workflow from step {step_idx}", repo, cwd,
            issue_id, session_name, registry, ctx, {}, timeout, interactive,
            start_step=step_idx,
        )
    )

    duration = time.time() - started_at
    if success:
        run.status = "completed"
        run.completed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        _emit_lifecycle_event("engineer/workflow.completed", {
            "issue_id": issue_id,
            "workflow": workflow.name,
            "duration": round(duration, 1),
            "text": f"Workflow {workflow.name} completed for {issue_id} in {duration:.0f}s",
        }, blocking=True)
    else:
        run.status = "failed"
        _emit_lifecycle_event("engineer/workflow.failed", {
            "issue_id": issue_id,
            "workflow": workflow.name,
            "text": f"Workflow {workflow.name} failed for {issue_id}",
        }, blocking=True)

    run.save()
    registry.mark_done(session_name)
    log.info(f"Resumed workflow {workflow.name} {'completed' if success else 'failed'} "
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
    interactive: bool = True,
    start_step: int = 0,
    role: str = "engineer",
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
    uses_worktree = any(s.worktree for s in workflow.steps)

    from modastack.prompts.resolver import resolve_agent_prompt

    project_root = _find_project_root(cwd)

    def _make_options(resume_id=None, agent_name=""):
        agent_prompt = ""
        if agent_name:
            agent_prompt = resolve_agent_prompt(agent_name, project_root, interactive=interactive)
        else:
            agent_prompt = resolve_agent_prompt("", project_root, interactive=interactive)

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
                    f"You are an agent working on issue #{issue_id}. "
                    + (f"Your working directory is an isolated git worktree at {cwd}. "
                       f"All changes go here — never modify the main repo checkout. "
                       if uses_worktree else
                       f"Your working directory is {cwd}. ")
                    + f"You will receive step-by-step instructions. Follow each one, "
                    f"then write your handoff file when asked.\n\n"
                    + agent_prompt
                ),
            },
        )

    _emit_lifecycle_event("engineer/session.started", {
        "issue_id": issue_id, "repo": repo,
        "text": f"Engineer started working on {issue_id}",
    })

    # CLI --role always wins; fall back to workflow step's agent field
    first_agent = role or ""
    if not first_agent:
        for s in workflow.steps[start_step:]:
            if s.agent:
                first_agent = s.agent
                break

    # Try resume, fall back to fresh session
    for attempt in range(2):
        resume_id = saved_id if attempt == 0 else None
        client = ClaudeSDKClient(_make_options(resume_id, agent_name=first_agent))
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

        step_idx = start_step
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

            # Await step — suspend and persist state for resume
            if step.await_event:
                log.info(f"Await step {step.name}: suspending, waiting for '{step.await_event}'")
                registry.update(session_name, status="waiting", phase=step.name)

                run = WorkflowRun.create(workflow.name, {"data": {"issue_id": issue_id}})
                run.status = "waiting"
                run.suspended_at_step = step_idx + 1
                run.await_event = step.await_event
                run.session_name = session_name
                run.variable_scopes = ctx.scopes
                run.repo = repo
                run.cwd = cwd
                run.issue_id = issue_id
                run.save()

                _emit_lifecycle_event("engineer/workflow.suspended", {
                    "issue_id": issue_id,
                    "workflow": workflow.name,
                    "step": step.name,
                    "await_event": step.await_event,
                    "run_id": run.run_id,
                    "text": f"Workflow suspended at {step.name}, waiting for '{step.await_event}'",
                })

                try:
                    await client.disconnect()
                except Exception:
                    pass
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

            prompt = _build_step_prompt(step, ctx, session_name, step.name)
            log.info(f"Step {step.name}: injecting prompt ({len(prompt)} chars)")

            await client.query(prompt)
            final_text = await _drain_response(client, session_name, issue_id)

            if final_text is None:
                failed_step = step.name
                _emit_step_failed(issue_id, workflow.name, step.name,
                                  "connection lost")
                return False

            # Validate handoff
            handoff = _read_handoff(session_name, step.name)
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
                handoff = _read_handoff(session_name, step.name)
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


def _build_step_prompt(step: StepDef, ctx: VariableContext, session_name: str = "", step_name: str = "") -> str:
    """Build the full prompt for a step, including handoff contract."""
    prompt = ctx.resolve(step.prompt)

    if step.handoff.required or step.handoff.optional:
        handoff_path = SessionRegistry.handoff_path(session_name, step_name) if session_name else "<session>/handoff-<step>.yaml"
        prompt += f"\n\nWhen complete, write your handoff file at `{handoff_path}` as YAML:"
        prompt += "\n```yaml"
        for field in step.handoff.required:
            prompt += f"\n{field}: <value>"
        for field in step.handoff.optional:
            prompt += f"\n{field}: <value>  # optional"
        prompt += "\n```"

    return prompt


def _read_handoff(session_name: str, step_name: str) -> dict:
    """Read the handoff YAML for a step."""
    path = SessionRegistry.handoff_path(session_name, step_name)
    if not path.exists():
        return {}
    try:
        content = path.read_text()
        return yaml.safe_load(content) or {}
    except yaml.YAMLError:
        return {}


def _validate_handoff(step: StepDef, handoff: dict) -> list[str]:
    """Return list of missing required fields."""
    return [f for f in step.handoff.required if f not in handoff]
