"""Workflow orchestrator — deterministic state machine driving agent sessions.

One brain session persists across steps until a prompt step changes the
effective model. Workflow handoffs carry structured context across steps and
across any model switch.

One registry entry per workflow. One log file. One session ID.

The orchestrator has no LLM — it is pure code. The agent does all the
work using its tools.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from bobi.sdk import (
    get_registry, save_session_id, load_session_id,
    log_activity, SessionEntry, SessionRegistry,
    TERMINAL_COMPLETED, TERMINAL_FAILED, ACTIVE_STATUSES,
)
from bobi.subagent import (
    AgentResult,
    _emit_lifecycle_event,
    _network_drop_error,
    _timeout_error,
    _tool_crash_error,
)
from bobi.workflow.schema import Workflow, StepDef
from bobi.workflow.state import WorkflowRun
from bobi.workflow.variables import VariableContext

log = logging.getLogger(__name__)

MAX_HANDOFF_RETRIES = 2


def _close_if_still_active(registry, session_name: str) -> None:
    """Close a session as ``done`` ONLY if it is still in an active status.

    ``_run_workflow_async`` now persists the honest terminal status in its
    ``finally`` (completed/failed) or leaves the entry ``waiting`` on suspend, so
    the caller must not blindly ``mark_done`` — that would clobber the honest
    status with a lossy ``done`` (and drop ``emit_confirmed``). This only fires
    as a defensive fallback if the entry was somehow left active (MDS-65 #3)."""
    entry = registry.get(session_name)
    if entry is None or entry.status in ACTIVE_STATUSES:
        registry.mark_done(session_name)


def try_resume_for_event(event_type: str, run_key: str = "", event: dict | None = None,
                         repo: str = "") -> bool:
    """Check if any suspended workflow is waiting for this event type and resume it.

    Called by the manager when it receives an event that might unblock a workflow.
    Returns True if a workflow was resumed.

    *repo* scopes the lookup to a specific repository so that identical
    run_keys in different repos do not collide.
    """
    from bobi.workflow.triggers import WorkflowDispatcher

    run = WorkflowRun.find_waiting(event_type, run_key, repo=repo)
    if not run:
        return False

    if not run.claim():
        log.info(f"Run {run.run_id} already claimed by another process")
        return False

    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    wf = dispatcher.find_workflow(run.workflow_name)
    if not wf:
        log.error(f"Cannot resume run {run.run_id}: workflow '{run.workflow_name}' not found")
        return False

    log.info(f"Resuming workflow {run.workflow_name} for {run.run_key} "
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
    """Return the installation root. The process bound it at its entry
    point; cwd plays no part — guessing from it is how workflow state
    forked into repo checkouts."""
    from bobi.paths import bobi_root
    return bobi_root()


def make_session_name(workflow_name: str, repo: str, run_key: str) -> str:
    """Deterministic session name for a workflow run."""
    repo_name = repo.split("/")[-1] if "/" in repo else repo
    return f"wf-{workflow_name}-{repo_name}-{run_key}"


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
    run_key: str | None = None,
    requested_by: dict | None = None,
    timeout: int = 3600,
    interactive: bool = True,
    role: str = "",
    input_fields: dict | None = None,
    model: str = "",
) -> bool:
    """Execute a workflow end-to-end with a single agent session.

    ``model`` is an explicit launch override: like ``--role``, it wins over
    every step-level and config-level model for the whole run.
    """
    run_key = run_key or "adhoc"
    requested_by = requested_by or {}
    started_at = time.time()

    # Session dir is created by the registry on register

    session_name = make_session_name(workflow.name, repo, run_key)
    needs_worktree = any(s.worktree for s in workflow.steps)
    work_cwd = _setup_worktree(cwd, session_name) if needs_worktree else cwd
    from bobi.sdk import compute_manifest_hash
    registry = get_registry()
    registry.register(SessionEntry(
        name=session_name, session_id="", role=role,
        run_key=run_key, title=task[:80], phase=workflow.name,
        project=repo, cwd=work_cwd, status="running", pid=os.getpid(),
        requested_by=requested_by,
        # Bound root, not cwd: the manifest lives at the installation root;
        # hashing a repo checkout/worktree yields "" and silently disables
        # image rotation.
        image_hash=compute_manifest_hash(),
        # Declared timeout for the dead-man reconciler's deadline (MDS-65 §4.6).
        timeout=timeout,
    ))

    _emit_lifecycle_event("agent/workflow.started", {
        "run_key": run_key,
        "role": role,
        "workflow": workflow.name,
        "repo": repo,
        "task": task[:500],
        "text": f"Workflow {workflow.name} started for {run_key}",
    })

    ctx = VariableContext()
    input_scope = {"task": task, "repo": repo, "run_key": run_key}
    if input_fields:
        input_scope.update(input_fields)
    ctx.set_scope("input", input_scope)
    if requested_by:
        ctx.set_scope("requested_by", requested_by)

    if needs_worktree:
        ctx.set_scope("worktree", {"path": work_cwd})

    success = asyncio.run(
        _run_workflow_async(
            workflow, task, repo, work_cwd, run_key, session_name,
            registry, ctx, requested_by, timeout, interactive, role=role,
            launch_model=model,
        )
    )

    duration = time.time() - started_at
    if success:
        _emit_lifecycle_event("agent/workflow.completed", {
            "run_key": run_key,
            "role": role,
            "workflow": workflow.name,
            "duration": round(duration, 1),
            "text": f"Workflow {workflow.name} completed for {run_key} in {duration:.0f}s",
        }, blocking=True)
    else:
        _emit_lifecycle_event("agent/workflow.failed", {
            "run_key": run_key,
            "role": role,
            "workflow": workflow.name,
            "text": f"Workflow {workflow.name} failed for {run_key}",
        }, blocking=True)

    # _run_workflow_async already persisted the honest terminal status (or left
    # the entry "waiting" on suspend). Only fall back to mark_done if it somehow
    # didn't — never clobber a completed/failed/waiting status with "done".
    _close_if_still_active(registry, session_name)

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
    run_key = run.run_key
    repo = run.repo
    cwd = run.cwd
    step_idx = run.suspended_at_step
    started_at = time.time()

    registry = get_registry()
    registry.update(session_name, status="running", phase=f"resuming")

    ctx = VariableContext()
    ctx.scopes = run.variable_scopes

    # RC#4: requested_by was persisted on the run's variable scopes at suspend —
    # thread it back so the resumed run's terminal session event still routes to
    # the requester's thread (the resume path used to drop it).
    requested_by = run.variable_scopes.get("requested_by", {}) or {}

    if event:
        ctx.set_scope("event", event.get("data", {}))

    run.status = "running"
    run.await_event = ""
    run.suspended_at_step = -1
    run.resumed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    run.save()

    _emit_lifecycle_event("agent/workflow.resumed", {
        "run_key": run_key,
        "workflow": workflow.name,
        "run_id": run.run_id,
        "resume_step": workflow.steps[step_idx].name if step_idx < len(workflow.steps) else "end",
        "text": f"Workflow {workflow.name} resumed for {run_key}",
    })

    # A launch-time --model override survives suspension via the _runtime
    # scope; without it the resume would re-resolve to the config default,
    # trip the model-mismatch guard, and both discard the saved session and
    # silently change the run's model.
    runtime_scope = run.variable_scopes.get("_runtime", {})
    launch_model = (
        str(runtime_scope.get("launch_model", "") or "")
        if isinstance(runtime_scope, dict) else ""
    )

    success = asyncio.run(
        _run_workflow_async(
            workflow, f"Resuming workflow from step {step_idx}", repo, cwd,
            run_key, session_name, registry, ctx, requested_by, timeout,
            interactive, start_step=step_idx, launch_model=launch_model,
        )
    )

    duration = time.time() - started_at
    if success:
        run.status = "completed"
        run.completed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        _emit_lifecycle_event("agent/workflow.completed", {
            "run_key": run_key,
            "workflow": workflow.name,
            "duration": round(duration, 1),
            "text": f"Workflow {workflow.name} completed for {run_key} in {duration:.0f}s",
        }, blocking=True)
    else:
        run.status = "failed"
        _emit_lifecycle_event("agent/workflow.failed", {
            "run_key": run_key,
            "workflow": workflow.name,
            "text": f"Workflow {workflow.name} failed for {run_key}",
        }, blocking=True)

    run.save()
    _close_if_still_active(registry, session_name)
    log.info(f"Resumed workflow {workflow.name} {'completed' if success else 'failed'} "
             f"in {duration:.0f}s")
    return success


async def _run_workflow_async(
    workflow: Workflow,
    task: str,
    repo: str,
    cwd: str,
    run_key: str,
    session_name: str,
    registry,
    ctx: VariableContext,
    requested_by: dict,
    timeout: int,
    interactive: bool = True,
    start_step: int = 0,
    role: str = "",
    launch_model: str = "",
) -> bool:
    """Async core: one brain session for all steps."""
    from bobi.brain import (
        continuation_token, get_brain, get_process_brain_model, resolve_model,
    )

    _brain = get_brain()
    saved_id = load_session_id(session_name)
    uses_worktree = any(s.worktree for s in workflow.steps)

    from bobi.prompts.resolver import resolve_agent_prompt

    project_root = _find_project_root(cwd)
    from bobi.config import Config
    try:
        team_cfg = Config.load(project_root)
    except Exception:
        team_cfg = None

    def _effective_step_model(step: StepDef | None) -> str:
        # Launch flag > step override > acting role's configured model >
        # team default (#617). The acting role mirrors prompt resolution:
        # a forced --role wins, else the step's agent, else the inherited one.
        if launch_model:
            return launch_model
        if step and step.model:
            return step.model
        step_role = role or ((step.agent if step else "") or current_agent)
        return resolve_model(team_cfg, role=step_role)

    def _is_prompt_step(step: StepDef) -> bool:
        return not (
            step.condition or step.action or step.notify or step.await_event
        )

    def _first_prompt_step() -> StepDef | None:
        for candidate in workflow.steps[start_step:]:
            if _is_prompt_step(candidate):
                return candidate
        return None

    def _continuation_prompt(step: StepDef) -> str:
        scopes = {
            name: data for name, data in ctx.scopes.items()
            if name != "_runtime"
        }
        context_yaml = yaml.safe_dump(scopes, sort_keys=True).strip()
        return (
            f"Continue workflow `{workflow.name}` for issue #{run_key}. "
            f"The next step is `{step.name}`. Use this workflow context from "
            "the original input and prior handoffs:\n\n"
            "```yaml\n"
            f"{context_yaml}\n"
            "```"
        )

    def _make_session(resume_id=None, agent_name="", model=""):
        agent_prompt = ""
        if agent_name:
            agent_prompt = resolve_agent_prompt(agent_name, project_root, interactive=interactive)
        else:
            agent_prompt = resolve_agent_prompt("", project_root, interactive=interactive)

        options = {"max_turns": 200, "skills": "all"}
        if model:
            options["model"] = model

        return _brain.make_session(
            cwd=cwd,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": (
                    f"You are an agent working on issue #{run_key}. "
                    + (f"Your working directory is an isolated git worktree at {cwd}. "
                       f"All changes go here — never modify the main repo checkout. "
                       if uses_worktree else
                       f"Your working directory is {cwd}. ")
                    + f"You will receive step-by-step instructions. Follow each one, "
                    f"then write your handoff file when asked.\n\n"
                    + agent_prompt
                ),
            },
            resume=resume_id,
            options=options,
        )

    _emit_lifecycle_event("agent/session.started", {
        "run_key": run_key, "role": role, "project": repo,
        "text": f"{role or 'Agent'} started working on {run_key}",
    })

    # CLI --role always wins; fall back to workflow step's agent field
    first_agent = role or ""
    if not first_agent:
        for s in workflow.steps[start_step:]:
            if s.agent:
                first_agent = s.agent
                break
    current_agent = first_agent
    first_prompt_step = _first_prompt_step()
    first_prompt_model = _effective_step_model(first_prompt_step)
    runtime_scope = ctx.scopes.get("_runtime", {})
    saved_session_model = (
        str(runtime_scope.get("model", "") or "")
        if isinstance(runtime_scope, dict) else ""
    )
    visit_counts = (
        dict(runtime_scope.get("visits", {}) or {})
        if isinstance(runtime_scope, dict) else {}
    )
    current_model = saved_session_model if saved_session_model else first_prompt_model
    fresh_resume_step = None

    if saved_id:
        # The model the saved session ran under: the recorded one, else (for
        # a run suspended before models were tracked) the process default it
        # must have used. A start_step=0 run with no record has nothing to
        # guard against.
        if saved_session_model:
            resume_from_model = saved_session_model
        elif start_step > 0:
            resume_from_model = get_process_brain_model()
        else:
            resume_from_model = first_prompt_model
        token = continuation_token(
            _brain, session_id=saved_id,
            from_model=resume_from_model, to_model=first_prompt_model,
        )
        if not token:
            log.info(
                "Saved workflow session model %r differs from next step model "
                "%r; starting a fresh session.",
                resume_from_model or "<default>",
                first_prompt_model or "<default>",
            )
            saved_id = ""
            current_model = first_prompt_model
            fresh_resume_step = first_prompt_step
        elif resume_from_model != first_prompt_model:
            log.info(
                "Saved workflow session continues natively from model %r "
                "to %r.",
                resume_from_model or "<default>",
                first_prompt_model or "<default>",
            )
            current_model = first_prompt_model

    # Terminal-emit outcome (MDS-65 RC#2). The `finally` emits the honest
    # lifecycle event for this session: session.completed on success/suspend,
    # session.failed on any failure path — never session.completed after a
    # failure. Declared before the try so the finally always sees them even if
    # an early statement raises.
    run_failed = False
    failure_error = ""
    # A suspended (await) run is dormant, not terminal — it must NOT emit a
    # terminal session event (the manager is now subscribed and would otherwise
    # be told the agent "finished" while it waits) and must NOT be marked
    # terminal in the registry (the reconciler leaves "waiting" alone).
    suspended = False

    # Try resume, fall back to fresh session
    for attempt in range(2):
        resume_id = (saved_id or None) if attempt == 0 else None
        client = _make_session(
            resume_id, agent_name=current_agent, model=current_model,
        )
        try:
            if resume_id:
                initial_prompt = None
            elif fresh_resume_step is not None:
                initial_prompt = _continuation_prompt(fresh_resume_step)
            elif attempt > 0 and start_step > 0 and first_prompt_step is not None:
                # A resumed run whose native resume failed (stale session):
                # re-inject the persisted scopes rather than starting with
                # the bare "Resuming workflow" task.
                initial_prompt = _continuation_prompt(first_prompt_step)
            else:
                initial_prompt = task
            await client.connect(initial_prompt)
            if resume_id:
                await client.query(task)
            _, drain_error = await _drain_response(
                client, session_name, run_key, model=current_model,
            )
            if drain_error:
                raise RuntimeError(drain_error)
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
            run_failed, failure_error = True, str(e)
            break

    try:
        if run_failed:
            return False

        registry.update(session_name, status="running",
                        session_id=saved_id or "")

        step_idx = start_step
        failed_step = ""

        def _exhaust_step(step: StepDef) -> tuple[int, str]:
            error = (
                f"Step {step.name} exceeded max_iterations="
                f"{step.max_iterations}"
            )
            log.error("%s in workflow %s", error, workflow.name)
            _emit_lifecycle_event("agent/step.exhausted", {
                "run_key": run_key,
                "workflow": workflow.name,
                "step": step.name,
                "visits": visit_counts[step.name],
                "max_iterations": step.max_iterations,
                "on_exhausted": step.on_exhausted,
                "text": error,
            }, blocking=True)
            if step.on_exhausted:
                jump = workflow.step_index(step.on_exhausted)
                if jump >= 0:
                    return jump, ""
                error = (
                    f"{error}; on_exhausted target "
                    f"{step.on_exhausted!r} was not found"
                )
            return -1, error

        while step_idx < len(workflow.steps):
            step = workflow.steps[step_idx]
            visit_counts[step.name] = int(visit_counts.get(step.name, 0)) + 1

            if step.max_iterations and visit_counts[step.name] > step.max_iterations:
                exhausted_jump, error = _exhaust_step(step)
                if exhausted_jump >= 0:
                    step_idx = exhausted_jump
                    continue
                failed_step = step.name
                run_failed, failure_error = True, error
                _emit_step_failed(run_key, workflow.name, step.name, error)
                return False

            # Route step — deterministic, no LLM
            if step.condition:
                taken = ctx.evaluate_condition(step.condition)
                target = step.goto if taken else step.else_goto
                log.info(f"Route {step.name}: {step.condition} → {target}")
                if target:
                    jump = workflow.step_index(target)
                    if jump >= 0:
                        if (
                            step.max_iterations
                            and jump <= step_idx
                            and visit_counts[step.name] >= step.max_iterations
                        ):
                            exhausted_jump, error = _exhaust_step(step)
                            if exhausted_jump >= 0:
                                step_idx = exhausted_jump
                                continue
                            failed_step = step.name
                            run_failed, failure_error = True, error
                            _emit_step_failed(
                                run_key, workflow.name, step.name, error,
                            )
                            return False
                        step_idx = jump
                        continue
                step_idx += 1
                continue

            # Native action step — deterministic, no LLM
            if step.action:
                log.info(f"Native action step {step.name}: {step.action}")
                result = _execute_native_action(step, ctx, cwd)
                ctx.set_scope(step.name, result)
                for k, v in result.items():
                    ctx.set_flat(k, v)
                _emit_lifecycle_event("agent/step.completed", {
                    "run_key": run_key,
                    "workflow": workflow.name,
                    "step": step.name,
                    "outputs": result,
                    "text": f"Native step {step.name} completed: {result.get('status', '')}",
                })
                step_idx += 1
                continue

            # Notify step — deterministic, no LLM
            if step.notify:
                _execute_notify_step(step, ctx, cwd, run_key, workflow.name)
                step_idx += 1
                continue

            # Await step — suspend and persist state for resume
            if step.await_event:
                log.info(f"Await step {step.name}: suspending, waiting for '{step.await_event}'")
                registry.update(session_name, status="waiting", phase=step.name)

                run = WorkflowRun.create(workflow.name, {"data": {"run_key": run_key}})
                run.status = "waiting"
                run.suspended_at_step = step_idx + 1
                run.await_event = step.await_event
                run.session_name = session_name
                ctx.set_scope("_runtime", {
                    "model": current_model,
                    "launch_model": launch_model,
                    "visits": visit_counts,
                })
                run.variable_scopes = ctx.scopes
                run.repo = repo
                run.cwd = cwd
                run.run_key = run_key
                run.save()

                _emit_lifecycle_event("agent/workflow.suspended", {
                    "run_key": run_key,
                    "workflow": workflow.name,
                    "step": step.name,
                    "await_event": step.await_event,
                    "run_id": run.run_id,
                    "text": f"Workflow suspended at {step.name}, waiting for '{step.await_event}'",
                })

                suspended = True
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return True

            # Prompt step — inject into the persistent session
            step_start = time.time()
            registry.update(session_name, phase=step.name)

            step_model = _effective_step_model(step)
            if step_model != current_model:
                # Continue the live session natively on the new model when
                # the brain supports it (#642); otherwise fresh + re-inject
                # the workflow scopes as YAML (lossy fallback).
                token = continuation_token(
                    _brain, session_id=load_session_id(session_name),
                    from_model=current_model, to_model=step_model,
                )
                log.info(
                    "Step %s: switching model from %r to %r (%s)",
                    step.name, current_model or "<default>",
                    step_model or "<default>",
                    "native resume" if token else "fresh session",
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                current_model = step_model
                if not role:
                    current_agent = step.agent or current_agent
                client = _make_session(
                    resume_id=token or None, agent_name=current_agent,
                    model=current_model,
                )
                if token:
                    await client.connect(None)
                else:
                    await client.connect(_continuation_prompt(step))
                    _, drain_error = await _drain_response(
                        client, session_name, run_key, model=current_model,
                    )
                    if drain_error:
                        failed_step = step.name
                        run_failed, failure_error = True, drain_error
                        _emit_step_failed(
                            run_key, workflow.name, step.name, drain_error,
                        )
                        return False

            _emit_lifecycle_event("agent/step.started", {
                "run_key": run_key,
                "workflow": workflow.name,
                "step": step.name,
                "repo": repo,
                "text": f"Step {step.name} started",
            })

            prompt = _build_step_prompt(step, ctx, session_name, step.name)
            log.info(f"Step {step.name}: injecting prompt ({len(prompt)} chars)")

            await client.query(prompt)
            final_text, drain_error = await _drain_response(
                client, session_name, run_key, model=current_model,
            )

            if final_text is None:
                failed_step = step.name
                run_failed, failure_error = True, drain_error
                _emit_step_failed(run_key, workflow.name, step.name,
                                  drain_error)
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
                await _drain_response(client, session_name, run_key,
                                      model=current_model)
                handoff = _read_handoff(session_name, step.name)
                missing = _validate_handoff(step, handoff)

            if missing:
                failed_step = step.name
                error = f"Handoff missing required fields after retries: {missing}"
                run_failed, failure_error = True, error
                _emit_step_failed(run_key, workflow.name, step.name, error)
                return False

            # Capture outputs for routing
            outputs = {k: handoff.get(k, "") for k in
                       step.handoff.required + step.handoff.optional
                       if k in handoff}
            ctx.set_scope(step.name, outputs)
            for k, v in outputs.items():
                ctx.set_flat(k, v)

            duration = time.time() - step_start
            _emit_lifecycle_event("agent/step.completed", {
                "run_key": run_key,
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
        run_failed, failure_error = True, str(e)
        _emit_lifecycle_event("agent/workflow.failed", {
            "run_key": run_key,
            "workflow": workflow.name,
            "error": str(e),
            "text": f"Workflow error: {e}",
        }, blocking=True)
        return False
    finally:
        # A suspended run is not terminal — skip the terminal emit + status
        # write entirely (the agent/workflow.suspended event already fired and
        # the entry stays "waiting" for resume).
        if not suspended:
            # RC#2: emit the HONEST terminal session event — session.failed
            # (carrying the error) on any failure path, never session.completed
            # right after workflow.failed. RC#4: carry requested_by so the
            # launcher can route it to the requester's thread.
            if run_failed:
                landed = _emit_lifecycle_event("agent/session.failed", {
                    "run_key": run_key, "role": role, "project": repo,
                    "error": failure_error or "unknown error",
                    "requested_by": requested_by or None,
                    "text": f"{role or 'Agent'} failed on {run_key}: {failure_error}",
                }, blocking=True)
            else:
                landed = _emit_lifecycle_event("agent/session.completed", {
                    "run_key": run_key, "role": role, "project": repo,
                    "requested_by": requested_by or None,
                    "text": f"{role or 'Agent'} finished {run_key}",
                }, blocking=True)
            # RC#3: durably record the honest terminal status here, matching what
            # was emitted, with emit_confirmed tracking whether the POST landed.
            # This closes the crash window between this finally and the caller's
            # close: if the process dies now, the durable record is already the
            # correct terminal status (not a stale "running" the reconciler would
            # mis-report as a crash), and an unconfirmed emit is re-sent later.
            registry.mark_terminal(
                session_name,
                TERMINAL_FAILED if run_failed else TERMINAL_COMPLETED,
                error=failure_error if run_failed else "",
                emit_confirmed=bool(landed),
            )
        try:
            await client.disconnect()
        except Exception:
            pass


async def _drain_response(
    client, session_name: str, run_key: str, model: str | None = None,
) -> tuple[str | None, str]:
    """Drain one turn. Returns ``(final_text, error)``.

    ``model`` is the model the session currently runs under; passing it keeps
    the session store's model record in step with mid-run switches (#642).
    ``None`` leaves any existing record untouched.
    """
    from bobi.brain import AssistantText, TurnResult

    final_text = ""
    try:
        async for msg in client.receive_response():
            if isinstance(msg, AssistantText):
                if msg.text:
                    final_text = msg.text
                    log_activity("response", {"text": final_text[:500]},
                                 session=session_name)
            elif isinstance(msg, TurnResult):
                save_session_id(session_name, msg.session_id, model=model)
                log_activity("stop", {"session_id": msg.session_id},
                             session=session_name)
                if msg.is_error:
                    return None, msg.result_text or "turn failed"
                return final_text, ""
    except asyncio.TimeoutError:
        error = _timeout_error()
        log.error(f"Drain timeout: {error}")
        return None, error
    except Exception as e:
        error = _tool_crash_error(e)
        log.error(f"Drain error: {error}")
        return None, error
    return None, _network_drop_error()


def _emit_step_failed(run_key, workflow_name, step_name, error):
    _emit_lifecycle_event("agent/step.failed", {
        "run_key": run_key,
        "workflow": workflow_name,
        "step": step_name,
        "error": error,
        "text": f"Step {step_name} failed: {error}",
    }, blocking=True)


def _remote_matches_slug(origin_url: str, repo_slug: str) -> bool:
    """Return True if *origin_url* points at *repo_slug* (``owner/repo``).

    Handles both HTTPS (``https://github.com/owner/repo.git``) and SSH
    (``git@github.com:owner/repo.git``) URLs by normalising to the
    ``owner/repo`` suffix and comparing with ``==`` to avoid substring
    false-positives (e.g. ``org/api`` matching ``org/api-private``).
    """
    # Normalise: strip trailing .git, grab the last two path components.
    url = origin_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # SSH URLs use ":" before the path; HTTPS uses "/".
    path_part = url.split(":")[-1] if ":" in url else url
    parts = path_part.rsplit("/", 2)
    if len(parts) >= 2:
        normalised = f"{parts[-2]}/{parts[-1]}"
        return normalised == repo_slug
    return False


def _resolve_repo_root(ctx: VariableContext) -> str | None:
    """Resolve the local checkout for the repo identified by ``input.repo``.

    ``input.repo`` is a GitHub slug like ``org/name``.  The checkout lives
    either as a child directory of the installation root (director-style
    layout) or *is* the installation root (single-repo layout).

    Returns ``None`` when the repo cannot be found locally.
    """
    from bobi.paths import bobi_root

    repo_slug = ctx.resolve("${{ input.repo }}") if "input" in ctx.scopes else ""
    if not repo_slug or repo_slug.startswith("${{"):
        return None

    root = bobi_root()
    repo_name = repo_slug.split("/")[-1]

    # Reject path-traversal components in the repo name so a crafted
    # input.repo like "org/.." cannot escape the installation root.
    if not repo_name or repo_name in (".", "..") or "/" in repo_name or "\\" in repo_name:
        return None

    # Director-style: repo is a child directory of the installation root
    candidate = root / repo_name
    if candidate.is_dir() and (candidate / ".git").exists():
        return str(candidate)

    # Single-repo: the installation root IS the repo — but only if the
    # remote URL contains the slug so we don't run git ops against the
    # wrong repo (e.g. an event for org/other-repo hitting the install root).
    if (root / ".git").exists():
        try:
            origin_url = subprocess.run(
                ["git", "-C", str(root), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except Exception:
            origin_url = ""
        if _remote_matches_slug(origin_url, repo_slug):
            return str(root)

    return None


def _cleanup_worktree_action(ctx: VariableContext, cwd: str) -> dict:
    """Native action: clean up the worktree for a closed PR's head branch."""
    from bobi.workflow.cleanup import cleanup_worktree

    head_branch = ctx.resolve("${{ input.head_branch }}") if "input" in ctx.scopes else ""
    if not head_branch or head_branch.startswith("${{"):
        return {"status": "skipped", "reason": "no head_branch in input"}

    repo_root = _resolve_repo_root(ctx)
    if repo_root is None:
        return {"status": "error", "reason": "could not resolve target repo from input"}
    return cleanup_worktree(repo_root, head_branch)


# Registry of native action functions.
# Each receives (ctx: VariableContext, cwd: str) and returns a dict.
_NATIVE_ACTIONS: dict = {
    "cleanup_worktree": _cleanup_worktree_action,
}


def _execute_native_action(step: StepDef, ctx: VariableContext, cwd: str) -> dict:
    """Run a registered native action. Returns the action's result dict."""
    action_fn = _NATIVE_ACTIONS.get(step.action)
    if action_fn is None:
        log.error(f"Unknown native action: {step.action}")
        return {"status": "error", "reason": f"unknown action: {step.action}"}
    try:
        return action_fn(ctx, cwd)
    except Exception as e:
        log.error(f"Native action {step.action} failed: {e}")
        return {"status": "error", "reason": str(e)}


def _execute_notify_step(
    step: StepDef,
    ctx: VariableContext,
    cwd: str,
    run_key: str,
    workflow_name: str,
) -> None:
    """Execute a notify step — deterministic Slack message, no LLM.

    Resolves the message template, finds Slack credentials from the project
    config, and posts to the appropriate channel.  Channel resolution:
    1. requested_by.channel (reply in the requester's thread)
    2. Falls back silently if no channel is available.
    """
    message = ctx.resolve(step.message)

    if step.notify != "slack":
        log.warning(f"Notify step {step.name}: unknown target '{step.notify}', skipping")
        return

    from bobi.config import Config
    project_root = _find_project_root(cwd)
    cfg = Config.load(project_root)
    token = cfg.credential("slack", "bot_token")
    if not token:
        log.warning(f"Notify step {step.name}: no Slack bot_token configured, skipping")
        return

    # Determine channel and thread from the requester context
    requester = ctx.scopes.get("requested_by", {})
    channel = requester.get("channel", "")
    thread_ts = requester.get("thread_ts", "")

    if not channel:
        log.warning(f"Notify step {step.name}: no Slack channel available, skipping")
        return

    from bobi.slack import post_slack_message
    try:
        post_slack_message(token, channel, message, thread_ts=thread_ts)
        log.info(f"Notify step {step.name}: posted to {channel}")
        _emit_lifecycle_event("engineer/notify.sent", {
            "run_key": run_key,
            "workflow": workflow_name,
            "step": step.name,
            "channel": channel,
            "text": f"Notify {step.name}: {message[:200]}",
        })
    except Exception as e:
        # Notification failures are non-fatal — log and continue
        log.warning(f"Notify step {step.name}: Slack post failed: {e}")
        _emit_lifecycle_event("engineer/notify.failed", {
            "run_key": run_key,
            "workflow": workflow_name,
            "step": step.name,
            "error": str(e),
            "text": f"Notify {step.name} failed: {e}",
        })


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
