"""Workflow orchestrator — deterministic state machine driving an agent.

Each step injects a prompt into a single agent session, waits for the
agent to write a handoff, validates the outputs, and moves to the next
step. Route steps branch based on handoff values. Await steps suspend
until an external event arrives.

The orchestrator has no LLM — it is pure code. The agent does all the
work using its tools.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from modastack.sdk import get_registry, SessionEntry
from modastack.subagent import (
    HANDOFF_DIR,
    AgentResult,
    _emit_lifecycle_event,
    _parse_issue_number,
    run_phase_blocking,
)
from modastack.workflow.schema import Workflow, StepDef
from modastack.workflow.variables import VariableContext

log = logging.getLogger(__name__)

MAX_HANDOFF_RETRIES = 2


def run_workflow(
    workflow: Workflow,
    task: str,
    repo: str,
    cwd: str,
    issue_id: str | None = None,
    requested_by: dict | None = None,
    timeout: int = 3600,
    title: str = "",
) -> bool:
    """Execute a workflow end-to-end. Returns True on success."""
    issue_id = issue_id or _parse_issue_number(task) or "adhoc"
    requested_by = requested_by or {}
    started_at = time.time()

    session_name = f"wf-{workflow.name}-{issue_id}"
    registry = get_registry()
    registry.register(SessionEntry(
        name=session_name, session_id="", role="engineer",
        issue_id=issue_id, title=(title or task)[:80], phase=workflow.name,
        repo=repo, cwd=cwd, status="running", pid=os.getpid(),
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
    step_idx = 0
    success = True
    failed_step = ""

    try:
        while step_idx < len(workflow.steps):
            step = workflow.steps[step_idx]

            if step.condition:
                taken = ctx.evaluate_condition(step.condition)
                if taken:
                    target = step.goto
                else:
                    target = step.else_goto
                log.info(f"Route {step.name}: {step.condition} → {target}")
                if target:
                    jump = workflow.step_index(target)
                    if jump >= 0:
                        step_idx = jump
                        continue
                step_idx += 1
                continue

            if step.await_event:
                log.info(f"Await step {step.name}: suspended (not yet implemented)")
                registry.update(session_name, status="waiting")
                break

            step_start = time.time()
            _emit_lifecycle_event("engineer/step.started", {
                "issue_id": issue_id,
                "workflow": workflow.name,
                "step": step.name,
                "repo": repo,
                "text": f"Step {step.name} started",
            })

            prompt = _build_step_prompt(step, ctx)
            log.info(f"Step {step.name}: injecting prompt ({len(prompt)} chars)")

            result = run_phase_blocking(
                issue_id=issue_id,
                phase=step.name,
                cwd=cwd,
                context=prompt,
                title=task[:80],
                repo=repo,
                timeout=step.timeout,
            )

            if not result.success:
                failed_step = step.name
                _emit_lifecycle_event("engineer/step.failed", {
                    "issue_id": issue_id,
                    "workflow": workflow.name,
                    "step": step.name,
                    "error": result.error,
                    "text": f"Step {step.name} failed: {result.error}",
                }, blocking=True)
                success = False
                break

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
                result = run_phase_blocking(
                    issue_id=issue_id, phase=f"{step.name}-fix",
                    cwd=cwd, context=fix_prompt, title=task[:80],
                    repo=repo, timeout=300,
                )
                handoff = _read_handoff(issue_id)
                missing = _validate_handoff(step, handoff)

            if missing:
                failed_step = step.name
                error = f"Handoff missing required fields after retries: {missing}"
                _emit_lifecycle_event("engineer/step.failed", {
                    "issue_id": issue_id,
                    "workflow": workflow.name,
                    "step": step.name,
                    "error": error,
                    "text": f"Step {step.name} failed: {error}",
                }, blocking=True)
                success = False
                break

            outputs = {k: handoff.get(k, "") for k in
                       step.handoff.required + step.handoff.optional
                       if k in handoff}
            ctx.set_scope(step.name, outputs)
            # Also expose by bare name so route conditions like
            # `needs_spec == true` resolve against this step's handoff.
            ctx.update_flat(outputs)

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

    except Exception as e:
        success = False
        failed_step = workflow.steps[step_idx].name if step_idx < len(workflow.steps) else "unknown"
        log.error(f"Workflow {workflow.name} error at step {failed_step}: {e}")
        _emit_lifecycle_event("engineer/step.failed", {
            "issue_id": issue_id,
            "workflow": workflow.name,
            "step": failed_step,
            "error": str(e),
            "text": f"Step {failed_step} error: {e}",
        }, blocking=True)

    duration = time.time() - started_at
    if success:
        _emit_lifecycle_event("engineer/workflow.completed", {
            "issue_id": issue_id,
            "workflow": workflow.name,
            "duration": round(duration, 1),
            "text": f"Workflow {workflow.name} completed for {issue_id} in {duration:.0f}s",
        }, blocking=True)
        registry.update(session_name, status="done")
    else:
        _emit_lifecycle_event("engineer/workflow.failed", {
            "issue_id": issue_id,
            "workflow": workflow.name,
            "step": failed_step,
            "error": f"Failed at step {failed_step}",
            "text": f"Workflow {workflow.name} failed at step {failed_step}",
        }, blocking=True)
        registry.update(session_name, status="error")

    log.info(f"Workflow {workflow.name} {'completed' if success else 'failed'} "
             f"in {duration:.0f}s")
    return success


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
