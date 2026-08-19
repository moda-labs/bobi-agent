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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import yaml

from bobi.sdk import (
    get_registry, save_session_id, load_session_id,
    log_activity, SessionEntry, session_handoff_path,
    TERMINAL_COMPLETED, TERMINAL_FAILED, ACTIVE_STATUSES,
)
from bobi.brain.turns import drain_turn
from bobi.subagent import _emit_lifecycle_event
from bobi.timeutil import now_iso
from bobi.workflow.schema import Workflow, StepDef
from bobi.workflow.state import WorkflowRun
from bobi.workflow.variables import VariableContext

log = logging.getLogger(__name__)

MAX_HANDOFF_RETRIES = 2

# How many times one step may be restarted after the harness cut its session
# off at the turn cap (#845). A cap hit is not a failure - the transcript is
# intact and the session id is valid, so the work continues on a fresh CLI
# process (fresh turn budget) resumed from that id.
#
# This count is the ONLY hard bound on the restart chain. `step.timeout` gates
# whether a new resume STARTS, but nothing in this module enforces it against
# a running drain (there is no `asyncio.wait_for` here, and `run_workflow`
# calls `asyncio.run` bare), so a resume begun just under the deadline still
# gets a full fresh budget. Worst case per prompt step is therefore
# `max_turns * (MAX_TURN_BUDGET_RESUMES + 1)` turns, ended in-process only by
# the agent finishing; the dead-man reconciler is the outer net. Raising
# either number raises that product - see #845 review.
MAX_TURN_BUDGET_RESUMES = 3


def _named_exception(e: BaseException) -> str:
    """*e* as a non-empty string, naming the type when ``str(e)`` is empty.

    A bare ``raise SomeError()`` stringifies to "", and an empty cause is what
    reaches an operator as "unknown error" - the exact discard #845 exists to
    stop. Note ``e or ...`` does NOT work here: an exception object is always
    truthy, so the fallback never fires.
    """
    return str(e) or f"{type(e).__name__} (no message)"


@dataclass(frozen=True)
class _NotifyOutcome:
    delivered: bool
    error: str = ""


def remind_workflow(run: WorkflowRun, workflow: Workflow) -> _NotifyOutcome:
    """Replay the notification that directly armed a waiting workflow gate.

    This is delivery only: it does not emit the awaited event, mutate the run,
    or resume execution. The saved visit map disambiguates branch-specific
    notifications that lead to the same await step.
    """
    if run.status != "waiting" or run.suspended_at_step <= 0:
        return _NotifyOutcome(False, "run is not waiting for action")

    await_idx = run.suspended_at_step - 1
    if await_idx >= len(workflow.steps):
        return _NotifyOutcome(False, "saved await step is outside the workflow")
    await_step = workflow.steps[await_idx]
    if not await_step.await_event:
        return _NotifyOutcome(False, "saved step is not an await gate")

    visits = (run.variable_scopes.get("_runtime", {}) or {}).get("visits", {})
    candidates = []
    for idx, step in enumerate(workflow.steps):
        if not step.notify:
            continue
        if step.goto == await_step.name or idx + 1 == await_idx:
            candidates.append((idx, step))
    visited = [item for item in candidates if visits.get(item[1].name)]
    if visited or len(candidates) == 1:
        _, notify_step = max(visited or candidates, key=lambda item: item[0])
    else:
        action = await_step.await_event.replace("_", " ")
        subject = f" for {run.run_key}" if run.run_key else ""
        notify_step = StepDef(
            name=f"remind_{await_step.name}", notify="slack",
            message=(f"Reminder: {run.workflow_name}{subject} is waiting for "
                     f"your {action}. Reply in this thread to continue, or "
                     "close the workflow from Bobi."),
        )
    ctx = VariableContext()
    ctx.scopes = run.variable_scopes
    return _execute_notify_step(
        notify_step, ctx, run.run_key, run.workflow_name,
    )


class DrainResult(NamedTuple):
    """One drained turn: ``final_text`` (None on failure), error, error kind."""

    final_text: str | None
    error: str
    error_kind: str = ""


def _turn_budget_resume_prompt(step: StepDef, final_try: bool) -> str:
    """The nudge that restarts a step after its session hit the turn cap.

    A nudge, not a re-brief: the transcript survived the cap, so re-injecting
    the step prompt would make the agent redo work it has already done. The
    final continuation says so explicitly - that is the one chance to get a
    handoff written before the step really does fail, which neither of the two
    sessions killed at 200 turns ever got (#845).
    """
    tail = (
        "This is the LAST continuation available for this step: write your "
        "handoff file NOW with whatever you have verified so far, then keep "
        "working."
        if final_try else
        "Keep going until the step is complete."
    )
    return (
        f"Your session reached its turn cap and was restarted on the same "
        f"transcript - no work was lost. Continue step `{step.name}` from "
        f"where you left off (check the repo and your handoff file for what "
        f"already landed rather than redoing it). {tail}"
    )


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

    No production caller today; the CLI ``workflows resume`` is the only live
    resume path. Before wiring one up: resume_workflow re-stamps the registry
    entry with os.getpid() and the resume timeout (#826), which assumes a
    dedicated per-run process. Resuming in a thread of a long-lived manager
    would stamp the MANAGER's pid - a reconciler timeout or ``subagents
    cancel`` would then SIGTERM the whole manager, and a dead resume thread
    would never be reaped as crashed. Spawn a process (as launch does), and
    pass an explicit timeout, before making this the manager's resume path.
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


def _find_project_root() -> Path:
    """Return the installation root. The process bound it at its entry
    point; the caller's cwd plays no part — guessing from it is how workflow
    state forked into repo checkouts, so this takes no cwd to guess from."""
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
    effort: str = "",
    fresh: bool = False,
) -> bool:
    """Execute a workflow end-to-end with a single agent session.

    ``model`` and ``effort`` are explicit launch overrides: like ``--role``,
    each wins over every step-level and config-level value for the whole run.

    ``fresh`` starts a new transcript instead of continuing this session
    name's saved one. Names are deterministic on purpose — they name the
    worktree branch and the registry entry — so a re-dispatch reuses the name
    and by default resumes, which is the retry contract. A worker whose state
    lives in a committed artifact rather than in context wants the opposite,
    and asks for it here. ``resume_workflow`` deliberately never sets it.
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

    # The run ledger (#1048): EVERY run gets a WorkflowRun entry, opened here
    # and closed with the honest outcome below. A retry of a failed run adopts
    # the failed entry and starts at its step checkpoint with its persisted
    # scopes - the same restore semantics as an await resume - instead of
    # replaying from step 0. ``fresh`` opts out, exactly as it does for the
    # session transcript: a fresh launch is a statement that prior state is
    # not wanted.
    run = None
    start_step = 0
    if not fresh:
        prior = WorkflowRun.find_by_run_key(workflow.name, run_key)
        if (prior and prior.status == "failed"
                and 0 <= prior.checkpoint_step < len(workflow.steps)):
            run = prior
            start_step = run.checkpoint_step
            ctx.scopes = run.variable_scopes
            run.status = "running"
            run.resumed_at = now_iso()
            log.info(
                "Retrying failed run %s from checkpoint step %d (%s)",
                run.run_id, start_step, workflow.steps[start_step].name,
            )
    if run is None:
        run = WorkflowRun.create(workflow.name, {"data": {"run_key": run_key}})
        run.run_key = run_key
        run.session_name = session_name
        run.repo = repo
        run.cwd = work_cwd
    run.save()

    success = asyncio.run(
        _run_workflow_async(
            workflow, task, repo, work_cwd, run_key, session_name,
            registry, ctx, requested_by, timeout, interactive,
            start_step=start_step, role=role,
            launch_model=model, launch_effort=effort, fresh=fresh, run=run,
        )
    )

    # A suspended run already persisted "waiting" in the await branch; the
    # outcome writes below are for runs that actually ENDED.
    if run.status != "waiting":
        if success:
            run.status = "completed"
            run.completed_at = now_iso()
        else:
            # Keep the checkpoint: this failed entry is what a retry adopts.
            run.status = "failed"
        run.save()

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
    # Re-stamp the launch-time pid/started_at/timeout with this process's own:
    # the entry still carries the (long-dead) launch pid and the launch
    # deadline, and the dead-man reconciler would otherwise close the resumed
    # run as crashed or timed out while it is healthily working (#826).
    registry.update(
        session_name, status="running", phase="resuming",
        pid=os.getpid(), started_at=started_at, timeout=timeout,
    )

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
    run.resumed_at = now_iso()
    run.save()

    _emit_lifecycle_event("agent/workflow.resumed", {
        "run_key": run_key,
        "workflow": workflow.name,
        "run_id": run.run_id,
        "resume_step": workflow.steps[step_idx].name if step_idx < len(workflow.steps) else "end",
        "text": f"Workflow {workflow.name} resumed for {run_key}",
    })

    # A launch-time --model/--effort override survives suspension via the
    # _runtime scope; without it the resume would re-resolve to the config
    # default - for the model that would trip the mismatch guard, discard the
    # saved session, and silently change the run's model; for the effort it
    # would silently change the run's dial.
    runtime_scope = run.variable_scopes.get("_runtime", {})
    if not isinstance(runtime_scope, dict):
        runtime_scope = {}
    launch_model = str(runtime_scope.get("launch_model", "") or "")
    launch_effort = str(runtime_scope.get("launch_effort", "") or "")

    success = asyncio.run(
        _run_workflow_async(
            workflow, f"Resuming workflow from step {step_idx}", repo, cwd,
            run_key, session_name, registry, ctx, requested_by, timeout,
            interactive, start_step=step_idx, launch_model=launch_model,
            launch_effort=launch_effort, run=run,
        )
    )

    duration = time.time() - started_at
    if run.status == "waiting":
        # Suspended again at a later await step. The run is the SAME ledger
        # entry now (#1048), already persisted as "waiting" by the await
        # branch - writing a terminal status here would close a run that is
        # merely parked.
        pass
    elif success:
        run.status = "completed"
        run.completed_at = now_iso()
        run.save()
        _emit_lifecycle_event("agent/workflow.completed", {
            "run_key": run_key,
            "workflow": workflow.name,
            "duration": round(duration, 1),
            "text": f"Workflow {workflow.name} completed for {run_key} in {duration:.0f}s",
        }, blocking=True)
    else:
        run.status = "failed"
        run.save()
        _emit_lifecycle_event("agent/workflow.failed", {
            "run_key": run_key,
            "workflow": workflow.name,
            "text": f"Workflow {workflow.name} failed for {run_key}",
        }, blocking=True)
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
    launch_effort: str = "",
    fresh: bool = False,
    *,
    run: WorkflowRun,
) -> bool:
    """Async core: one brain session for all steps.

    ``run`` is this run's ledger entry (#1048), owned by the caller. The loop
    checkpoints it after each completed step and flips it to "waiting" at an
    await step; the caller writes the terminal outcome.
    """
    from bobi.brain import (
        ERROR_KIND_MAX_TURNS, continuation_token, get_brain,
        get_process_brain_model, resolve_effort, resolve_max_turns,
        resolve_model,
    )

    _brain = get_brain()
    # A fresh launch ignores the saved transcript but keeps the name: the
    # branch, the registry entry and the admission dedupe all key on it.
    saved_id = "" if fresh else load_session_id(session_name)
    uses_worktree = any(s.worktree for s in workflow.steps)

    from bobi.prompts.resolver import resolve_agent_prompt

    project_root = _find_project_root()
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

    def _effective_step_effort(step: StepDef | None) -> str:
        # The reasoning-effort sibling of _effective_step_model (#778), same
        # precedence: launch flag > step override > role > team default.
        if launch_effort:
            return launch_effort
        if step and step.effort:
            return step.effort
        step_role = role or ((step.agent if step else "") or current_agent)
        return resolve_effort(team_cfg, role=step_role)

    def _effective_step_max_turns(step: StepDef | None) -> int:
        # The turn-cap sibling (#845): step override > role > team default >
        # framework default. There is no launch flag - the cap is a safety
        # backstop an operator configures, not a per-invocation dial.
        step_role = role or ((step.agent if step else "") or current_agent)
        return resolve_max_turns(
            team_cfg, role=step_role,
            explicit=(step.max_turns if step else 0),
        )

    def _is_prompt_step(step: StepDef) -> bool:
        return not (
            step.condition or step.action or step.notify or step.await_event
        )

    def _first_prompt_step() -> StepDef | None:
        for candidate in workflow.steps[start_step:]:
            if _is_prompt_step(candidate):
                return candidate
        return None

    def _context_prefix() -> str:
        """The run's context as a labelled block prepended to a step prompt.

        This is the ONLY delivery of the launch task and persisted scopes
        (#1016): it rides the first step prompt of a fresh transcript instead
        of draining a turn of its own, so nothing in the dispatch text can
        execute before a step's instruction frames it. ``input.task`` travels
        inside the block as a scope value like every other variable.
        """
        scopes = {
            name: data for name, data in ctx.scopes.items()
            if name != "_runtime"
        }
        context_yaml = yaml.safe_dump(scopes, sort_keys=True).strip()
        return (
            f"Workflow `{workflow.name}` context for issue #{run_key} — the "
            "original input and prior handoffs, as reference for the "
            "instruction that follows:\n\n"
            "```yaml\n"
            f"{context_yaml}\n"
            "```\n\n"
        )

    def _make_session(resume_id=None, agent_name="", model="", effort="", *,
                      max_turns):
        from bobi.runtime_guard import prepare_brain_runtime

        prepare_brain_runtime()
        agent_prompt = resolve_agent_prompt(agent_name, project_root, interactive=interactive)

        # max_turns is keyword-REQUIRED: _effective_step_max_turns is the one
        # place the cap is resolved, so a call site cannot quietly fall back to
        # a second default and drift from the configured value (#845).
        options = {"max_turns": max_turns, "skills": "all"}
        if model:
            options["model"] = model
        if effort:
            options["effort"] = effort

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

    # Carries the launch chain for the same reason the adhoc emitter does
    # (#849), through the same helper: this is the path a detached
    # non-persistent launch takes, so leaving it out would blind forensics on
    # the majority of runs.
    from bobi.launch_lineage import lineage_fields
    _emit_lifecycle_event("agent/session.started", {
        "run_key": run_key, "role": role, "project": repo,
        **lineage_fields(),
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
    # Effort is exempt from the resume guard (#778): both brains accept a new
    # effort on a resumed session, so the effective dial is simply recomputed
    # for the next step - no saved-effort record, no continue-vs-fresh check.
    current_effort = _effective_step_effort(first_prompt_step)
    # Like effort, the turn cap is simply recomputed for the step about to run:
    # it is a session-construction option, not conversational state, so it
    # never gates resume-vs-fresh (#845).
    current_max_turns = _effective_step_max_turns(first_prompt_step)
    runtime_scope = ctx.scopes.get("_runtime", {})
    saved_session_model = (
        str(runtime_scope.get("model", "") or "")
        if isinstance(runtime_scope, dict) else ""
    )
    visit_counts = (
        dict(runtime_scope.get("visits", {}) or {})
        if isinstance(runtime_scope, dict) else {}
    )
    # A fresh session always runs the next step's model; only a genuinely
    # resumable session stays on the one it was suspended under.
    current_model = (
        saved_session_model if (saved_id and saved_session_model)
        else first_prompt_model
    )

    if saved_id and (saved_session_model or start_step > 0):
        # The model the saved session ran under: the recorded one, else (for
        # a run suspended before models were tracked) the process default it
        # must have used. A start_step=0 run with no record has nothing to
        # guard against.
        resume_from_model = saved_session_model or get_process_brain_model()
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

    # The brain session is opened lazily, at the first prompt step that
    # actually executes (#1016): connect() is never a turn, no text is
    # delivered at open, and a workflow whose reachable steps are all
    # deterministic (route/action/notify/await) opens no session at all.
    client = None
    # True while the run's context (launch input, persisted scopes) still has
    # to reach the agent — consumed by prepending _context_prefix() to the
    # next step prompt. Set at session open and on any fresh mid-run rebuild.
    context_pending = False

    async def _open_session() -> bool:
        """Open the run's session: native resume when the saved transcript is
        usable, else fresh. Returns True when the transcript was resumed.

        Raises on a fresh-connect failure; the step loop's except path emits
        the honest terminal events (D029's concern lives inside the big try
        now, so a construction failure cannot escape the terminal-honesty
        finally).
        """
        nonlocal client, saved_id
        for attempt in range(2):
            resume_id = (saved_id or None) if attempt == 0 else None
            c = _make_session(
                resume_id, agent_name=current_agent, model=current_model,
                effort=current_effort, max_turns=current_max_turns,
            )
            try:
                await c.connect()
            except Exception as e:
                if resume_id:
                    log.warning(
                        f"Resume failed (stale session?), retrying fresh: {e}")
                    save_session_id(session_name, "")
                    saved_id = ""
                    try:
                        await c.disconnect()
                    except Exception:
                        pass
                    continue
                raise
            client = c
            return bool(resume_id)
        raise RuntimeError("session open fell through both attempts")

    try:
        registry.update(session_name, status="running")

        step_idx = start_step

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

        def _checkpoint(next_idx: int) -> None:
            # Persist "the next step is next_idx" plus everything needed to
            # get there again (#1048): a retry of a failed run restores these
            # scopes and resumes here, exactly like an await resume, instead
            # of replaying completed steps. Same _runtime shape as the await
            # branch below - the two must not drift, both feed the same
            # restore path.
            ctx.set_scope("_runtime", {
                "model": current_model,
                "launch_model": launch_model,
                "launch_effort": launch_effort,
                "visits": visit_counts,
            })
            run.checkpoint_step = next_idx
            run.variable_scopes = ctx.scopes
            run.save()

        while step_idx < len(workflow.steps):
            step = workflow.steps[step_idx]
            visit_counts[step.name] = int(visit_counts.get(step.name, 0)) + 1

            if step.max_iterations and visit_counts[step.name] > step.max_iterations:
                exhausted_jump, error = _exhaust_step(step)
                if exhausted_jump >= 0:
                    step_idx = exhausted_jump
                    continue
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
                _checkpoint(step_idx + 1)
                step_idx += 1
                continue

            # Notify step — deterministic, no LLM
            if step.notify:
                outcome = _execute_notify_step(
                    step, ctx, run_key, workflow.name,
                )
                next_step = (
                    workflow.steps[step_idx + 1]
                    if step_idx + 1 < len(workflow.steps) else None
                )
                if (
                    not outcome.delivered
                    and next_step is not None
                    and next_step.await_event
                ):
                    error = (
                        "workflow.notify_undeliverable: "
                        f"{outcome.error}; refusing to arm await step "
                        f"{next_step.name}"
                    )
                    run_failed, failure_error = True, error
                    _emit_step_failed(run_key, workflow.name, step.name, error)
                    return False
                _checkpoint(step_idx + 1)
                step_idx += 1
                continue

            # Await step — suspend and persist state for resume. The run is
            # the ledger entry opened at launch (#1048), flipped to waiting
            # here - suspension is a state of THIS run, not a new record.
            if step.await_event:
                log.info(f"Await step {step.name}: suspending, waiting for '{step.await_event}'")
                registry.update(session_name, status="waiting", phase=step.name)

                run.status = "waiting"
                run.suspended_at_step = step_idx + 1
                run.await_event = step.await_event
                ctx.set_scope("_runtime", {
                    "model": current_model,
                    "launch_model": launch_model,
                    "launch_effort": launch_effort,
                    "visits": visit_counts,
                })
                run.variable_scopes = ctx.scopes
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
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                return True

            # Prompt step — inject into the persistent session
            step_start = time.time()
            registry.update(session_name, phase=step.name)

            if client is None:
                # First prompt turn of this process. Open under the launch
                # dials (computed for first_prompt_step, with the saved-id
                # model guard already applied); the switch branch below
                # corrects to this step's own dials exactly as it would
                # mid-run. The context block is owed whenever the transcript
                # does not already carry THIS dispatch's input: always on a
                # fresh run's first prompt turn (a resumed transcript holds
                # the previous dispatch, not this task), and on any fresh
                # transcript for a resumed run.
                resumed = await _open_session()
                context_pending = (start_step == 0) or not resumed

            step_model = _effective_step_model(step)
            step_effort = _effective_step_effort(step)
            # The cap is a construction-time CLI flag, so it can only change by
            # rebuilding the session - exactly like effort (#845 review). An
            # earlier cut of this change recomputed it here WITHOUT joining the
            # condition below, on the theory that a construction-time option
            # with no conversational consequence need not force a rebuild. That
            # silently dropped every step-level override after the first,
            # because one session spans every step whose agent/model/effort
            # match (issue-lifecycle.yaml, and the documented example in
            # docs/WORKFLOW_ENGINE.md). Rebuilding costs nothing here: the cap
            # never changes the MODEL, so continuation_token returns the saved
            # session id and the transcript is resumed natively - the same
            # exemption an effort-only change already relies on.
            step_max_turns = _effective_step_max_turns(step)
            if (
                step_model != current_model
                or step_effort != current_effort
                or step_max_turns != current_max_turns
            ):
                # Continue the live session natively on the new model when
                # the brain supports it (#642); otherwise fresh + re-inject
                # the workflow scopes as YAML (lossy fallback). An agent
                # change entering this branch starts fresh: the new agent
                # must not inherit the previous agent's transcript under its
                # system prompt (e.g. a reviewer step contaminated by the
                # builder's reasoning). An agent change matching on ALL THREE
                # dials still never enters the branch - a pre-existing gap in
                # that isolation, narrowed but not closed by adding the cap to
                # the condition: an agent change that also moves the cap now
                # gets the fresh-session isolation it always should have had.
                # No shipped workflow sets a per-step cap yet, so that is a
                # latent improvement rather than a live behavior change. An
                # effort-only
                # or cap-only change is exempt from the resume guard
                # (#778/#845): continuation_token sees the same model on both
                # sides, so whenever a resumable session id exists the session
                # just reconnects natively under the new dial.
                next_agent = (
                    current_agent if role else (step.agent or current_agent)
                )
                token = ""
                if next_agent == current_agent:
                    token = continuation_token(
                        _brain, session_id=load_session_id(session_name),
                        from_model=current_model, to_model=step_model,
                    )
                log.info(
                    "Step %s: switching session options (model %r -> %r, "
                    "effort %r -> %r, max_turns %d -> %d): %s",
                    step.name, current_model or "<default>",
                    step_model or "<default>",
                    current_effort or "<default>",
                    step_effort or "<default>",
                    current_max_turns, step_max_turns,
                    "native resume" if token else "fresh session",
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                current_model = step_model
                current_effort = step_effort
                current_max_turns = step_max_turns
                current_agent = next_agent
                if token:
                    client = _make_session(
                        resume_id=token, agent_name=current_agent,
                        model=current_model, effort=current_effort,
                        max_turns=current_max_turns,
                    )
                    try:
                        await client.connect()
                    except Exception as e:
                        # Stale/unresumable session: fall back to the fresh
                        # path below instead of failing the whole run.
                        log.warning(
                            "Native resume failed at step %s (stale "
                            "session?), retrying fresh: %s", step.name, e,
                        )
                        save_session_id(session_name, "")
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        token = ""
                if not token:
                    client = _make_session(
                        resume_id=None, agent_name=current_agent,
                        model=current_model, effort=current_effort,
                        max_turns=current_max_turns,
                    )
                    await client.connect()
                    # The re-injected scopes ride this step's prompt (#1016)
                    # instead of draining a context-only turn: a fresh
                    # transcript's first turn is the step turn.
                    context_pending = True

            _emit_lifecycle_event("agent/step.started", {
                "run_key": run_key,
                "workflow": workflow.name,
                "step": step.name,
                "repo": repo,
                "text": f"Step {step.name} started",
            })

            prompt = _build_step_prompt(step, ctx, session_name, step.name)
            if context_pending:
                prompt = _context_prefix() + prompt
                context_pending = False
            log.info(f"Step {step.name}: injecting prompt ({len(prompt)} chars)")

            await client.query(prompt)
            drain = await _drain_response(
                client, session_name, model=current_model,
            )

            # A turn-cap kill is recoverable, not terminal (#845): the harness
            # ended the CLI process, but the transcript is intact and the saved
            # session id resumes it on a fresh process with a fresh turn
            # budget. Restart the step there instead of throwing away the whole
            # run mid-edit - bounded by the step's wall-clock timeout (the
            # budget operators actually set) and MAX_TURN_BUDGET_RESUMES.
            resumes = 0
            while (
                drain.final_text is None
                and drain.error_kind == ERROR_KIND_MAX_TURNS
                and resumes < MAX_TURN_BUDGET_RESUMES
                and time.time() - step_start < step.timeout
            ):
                resumes += 1
                final_try = resumes == MAX_TURN_BUDGET_RESUMES
                log.warning(
                    "Step %s hit the turn cap (%s); resuming session %s "
                    "(%d/%d)%s", step.name, drain.error, session_name,
                    resumes, MAX_TURN_BUDGET_RESUMES,
                    " - final continuation" if final_try else "",
                )
                log_activity("turn_budget_resume", {
                    "step": step.name,
                    "error": drain.error,
                    "attempt": resumes,
                    "max_attempts": MAX_TURN_BUDGET_RESUMES,
                    "max_turns": current_max_turns,
                }, session=session_name)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                cap_resume_id = load_session_id(session_name)
                if not cap_resume_id:
                    log.error(
                        "Step %s hit the turn cap with no resumable session "
                        "id; cannot continue", step.name,
                    )
                    break
                client = _make_session(
                    resume_id=cap_resume_id, agent_name=current_agent,
                    model=current_model, effort=current_effort,
                    max_turns=current_max_turns,
                )
                try:
                    await client.connect()
                    await client.query(
                        _turn_budget_resume_prompt(step, final_try)
                    )
                except Exception as e:
                    log.error(
                        "Step %s could not be resumed after the turn cap: %s",
                        step.name, e,
                    )
                    drain = DrainResult(
                        None,
                        f"{drain.error}; resume failed: "
                        f"{_named_exception(e)}",
                        drain.error_kind,
                    )
                    break
                drain = await _drain_response(
                    client, session_name, model=current_model,
                )

            if drain.final_text is None:
                run_failed, failure_error = True, drain.error
                _emit_step_failed(run_key, workflow.name, step.name,
                                  drain.error)
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
                await _drain_response(client, session_name,
                                      model=current_model)
                handoff = _read_handoff(session_name, step.name)
                missing = _validate_handoff(step, handoff)

            if missing:
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

            _checkpoint(step_idx + 1)
            step_idx += 1

        if client is None and start_step == 0 and task:
            # No prompt step executed, so the launch brief was delivered to no
            # agent turn — deterministic steps did all the work. Complete
            # honestly, but say so: a brief that silently vanishes is how a
            # workflow whose only prompt step sits behind an untaken route
            # hides its own inaction (#1016 §5.2).
            log.info(
                "Workflow %s completed without a prompt step; the launch "
                "task was not delivered to any agent turn.", workflow.name,
            )
            _emit_lifecycle_event("agent/workflow.brief_undelivered", {
                "run_key": run_key,
                "workflow": workflow.name,
                "task": task[:500],
                "text": (
                    f"Workflow {workflow.name} ran no agent turn; launch "
                    "task not delivered"
                ),
            })

        return True

    except Exception as e:
        # An exception with an empty str() (a bare `raise SomeError()`) must
        # still name itself rather than reaching an operator as "" (#845).
        error = _named_exception(e)
        log.error(f"Workflow error: {error}")
        run_failed, failure_error = True, error
        _emit_lifecycle_event("agent/workflow.failed", {
            "run_key": run_key,
            "workflow": workflow.name,
            "error": error,
            "text": f"Workflow error: {error}",
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
                # Every path above populates failure_error with a named cause,
                # so this fallback is unreachable in practice - it names the
                # gap rather than inventing an "unknown error" (#845).
                failure_error = failure_error or (
                    f"{workflow.name} failed with no error reported"
                )
                landed = _emit_lifecycle_event("agent/session.failed", {
                    "run_key": run_key, "role": role, "project": repo,
                    "error": failure_error,
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
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


async def _drain_response(
    client, session_name: str, *, model: str,
) -> DrainResult:
    """Adapt one drained turn into the step loop's flat shape.

    ``final_text`` is None exactly when the turn failed; ``error`` is then
    always non-empty and ``error_kind`` carries the brain's classification
    (e.g. ``max_turns_reached``) so callers can act on the failure MODE
    without pattern-matching prose.

    The drain itself (text capture, the model-stamped session-id save,
    activity records, stream-failure normalization) is
    ``bobi.brain.turns.drain_turn``; this adapter only folds the brain's own
    turn verdict into that flat shape.
    """
    outcome = await drain_turn(client, session_name, model=model)
    msg = outcome.result
    if msg is None:
        return DrainResult(None, outcome.failure, outcome.failure_kind)
    if msg.is_error:
        # Prefer the brain's own diagnosis. result_text is EMPTY on a
        # turn-cap kill, which is how "turn failed" - a literal fallback -
        # reached operators as the whole story (#845).
        return DrainResult(None, msg.error_text(), msg.error_kind)
    return DrainResult(outcome.final_text, "", "")


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

    # Deployed-team layout: checkouts live under <root>/checkouts/<name>
    # (#1016 §5.2). Before this branch existed, every pr-closed run on a
    # deployed team resolved to None, the gated cleanup action was inert on
    # arrival, and the un-stepped launch turn did the deletions instead. The
    # remote must corroborate the slug: a checkouts dir can hold a same-named
    # repo from another org, and git ops against it would be the wrong repo.
    candidate = root / "checkouts" / repo_name
    if candidate.is_dir() and (candidate / ".git").exists():
        from bobi.gitutil import origin_url

        if _remote_matches_slug(origin_url(candidate), repo_slug):
            return str(candidate)

    # Single-repo: the installation root IS the repo — but only if the
    # remote URL contains the slug so we don't run git ops against the
    # wrong repo (e.g. an event for org/other-repo hitting the install root).
    if (root / ".git").exists():
        from bobi.gitutil import origin_url

        if _remote_matches_slug(origin_url(root), repo_slug):
            return str(root)

    return None


def _cleanup_worktree_action(ctx: VariableContext, cwd: str) -> dict:
    """Native action: clean up the worktree for a closed PR's head branch.

    The merge verdict comes from a LIVE read of the PR, never from the event
    payload that launched the run - that payload is a snapshot of the moment
    the webhook fired, and a `pull_request.closed` event fires for a PR closed
    WITHOUT merging too (an abandoned PR, or one auto-closed by its base
    branch being deleted). Deleting that head destroys the only copy of the
    work, so anything short of "GitHub says merged, just now" preserves it.

    Every return carries ``merged_live``: it is what the workflow routes on,
    so an early return that omitted it would route as if the PR had merged.
    """
    from bobi.workflow import cleanup as cleanup_mod

    head_branch = ctx.resolve("${{ input.head_branch }}") if "input" in ctx.scopes else ""
    if not head_branch or head_branch.startswith("${{"):
        return {"status": "skipped", "reason": "no head_branch in input",
                "merged_live": False}

    repo_root = _resolve_repo_root(ctx)
    if repo_root is None:
        return {"status": "error", "merged_live": False,
                "reason": "could not resolve target repo from input"}

    repo_slug = ctx.resolve("${{ input.repo }}")
    pr_number = ctx.resolve("${{ input.pr_number }}")
    merge_state = cleanup_mod.pr_merge_state(repo_slug, pr_number)
    merged = merge_state.get("merged") is True

    result = cleanup_mod.cleanup_worktree(repo_root, head_branch, merged=merged)
    result["merged_live"] = merged
    if merge_state.get("error"):
        result["merge_state_error"] = merge_state["error"]
        # `reason` is what reaches a human in the notify message. "not merged"
        # would be a claim we cannot make: we could not read the PR at all.
        result["reason"] = (
            "could not read the PR's merge state, so nothing was deleted: "
            f"{merge_state['error']}"
        )
    return result


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
    run_key: str,
    workflow_name: str,
) -> _NotifyOutcome:
    """Execute a notify step — deterministic Slack message, no LLM.

    Resolves the message template, finds Slack credentials from the project
    config, and posts to the appropriate channel.  Channel resolution:
    1. requested_by.channel (reply in the requester's thread)
    2. Returns an undeliverable outcome if no channel is available.
    """
    message = ctx.resolve(step.message)

    def _undeliverable(reason: str) -> _NotifyOutcome:
        error = f"Notify step {step.name}: {reason}"
        log.warning(error)
        _emit_lifecycle_event("engineer/notify.undeliverable", {
            "run_key": run_key,
            "workflow": workflow_name,
            "step": step.name,
            "error": reason,
            "text": error,
        })
        return _NotifyOutcome(delivered=False, error=error)

    if step.notify != "slack":
        return _undeliverable(f"unknown target '{step.notify}'")

    from bobi.config import Config
    project_root = _find_project_root()
    cfg = Config.load(project_root)
    token = cfg.credential("slack", "bot_token")
    if not token:
        return _undeliverable("no Slack bot_token configured")

    # Determine channel and thread from the requester context
    requester = ctx.scopes.get("requested_by") or {}
    channel = requester.get("channel", "")
    thread_ts = requester.get("thread_ts", "")

    if not channel:
        return _undeliverable("no Slack channel available")

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
        return _NotifyOutcome(delivered=True)
    except Exception as e:
        # Notification failures are non-fatal — log and continue
        error = f"Notify step {step.name}: Slack post failed: {e}"
        log.warning(error)
        _emit_lifecycle_event("engineer/notify.failed", {
            "run_key": run_key,
            "workflow": workflow_name,
            "step": step.name,
            "error": str(e),
            "text": f"Notify {step.name} failed: {e}",
        })
        return _NotifyOutcome(delivered=False, error=error)


def _build_step_prompt(step: StepDef, ctx: VariableContext, session_name: str = "", step_name: str = "") -> str:
    """Build the full prompt for a step, including handoff contract."""
    prompt = ctx.resolve(step.prompt)

    if step.handoff.required or step.handoff.optional:
        handoff_path = session_handoff_path(session_name, step_name) if session_name else "<session>/handoff-<step>.yaml"
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
    path = session_handoff_path(session_name, step_name)
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
