"""The three things an operator can DO to a suspended workflow run.

The runs table is read-only except here. A run that stopped at a human gate can
be resumed (answer the gate with a verdict and let the workflow take the branch
that answer chose), reminded (re-send the gate's notification without touching
the run), or closed (abandon it, and mark its session cancelled).

Extracted from ``LocalRuntime`` so the supervisor's admin commands call the
SAME function rather than a second copy. That is the anti-drift property the
read builders already have: two runtimes, one implementation, so a fix to the
resume claim or the status precondition cannot land on one surface and miss the
other.

Local exceptions, like ``details.UnknownRun``: this module knows nothing about
``TeamRuntime``, and its callers map these onto their own error vocabulary
(the webapp raises the runtime's typed errors; the supervisor puts a ``code``
on the command result). That keeps the import edge one-way — the runtime
imports the builders, never the reverse.

Every action takes an explicit root: one webapp process serves every team on
the machine and binds none of them.
"""

from __future__ import annotations

from pathlib import Path


class UnknownRun(Exception):
    """No workflow run with that id under this root."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"unknown run '{run_id}'")


class RunNotWaiting(Exception):
    """The run exists but is not at a gate, so there is nothing to act on."""

    def __init__(self, run_id: str, status: str) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(f"run {run_id} is '{status}', not 'waiting'")


class ActionFailed(Exception):
    """The precondition held but the action itself could not complete."""


def _waiting_run(root: Path, run_id: str):
    """The run, or the reason it cannot be acted on.

    One lookup and one status check, shared by all three actions: they differ
    in what they do to a waiting run, never in what counts as one.
    """
    from bobi.workflow.state import WorkflowRun

    run = next((r for r in WorkflowRun.list_runs(root=root)
                if r.run_id == run_id), None)
    if run is None:
        raise UnknownRun(run_id)
    if run.status != "waiting":
        raise RunNotWaiting(run_id, run.status)
    return run


def resume_run(root: Path, run_id: str, *, verdict: str = "",
               reply: str = "") -> dict:
    """Resume a suspended workflow run by spawning the CLI resume.

    *verdict* and *reply* are the human's answer to the gate. They reach the
    workflow as the ``event`` scope, so a route step after the await branches
    on ``${{event.verdict}}`` - which is what makes a resume an answer rather
    than a force-continue.

    An unrecognised verdict is refused here rather than passed on. The CLI
    would refuse it too, but that refusal happens in a detached child whose
    output goes to /dev/null, so the operator would see an accepted resume
    that silently did nothing.

    A SPAWN, not a thread, and the orchestrator says why in its own words
    (``try_resume_for_event``): ``resume_workflow`` re-stamps the session
    registry entry with ``os.getpid()`` and the resume timeout, which assumes a
    dedicated per-run process. Resuming inside a long-lived process would stamp
    THAT process's pid - a reconciler timeout or a ``subagents cancel`` would
    then signal the web app, or the supervisor, instead of the run.

    The single-winner claim lives in the spawned command, not here: a claim
    held by a caller that then fails to spawn strands the run. This checks the
    status so the ordinary "not resumable" case is a clean rejection rather
    than a child that exits in the dark. Two concurrent resumes therefore both
    spawn, and the loser exits without running the workflow - the claim is the
    arbiter, not this check.
    """
    import subprocess
    import sys

    from bobi import paths
    from bobi.workflow.schema import (
        GATE_VERDICT_REJECT, GATE_VERDICTS, reads_gate_verdict,
    )
    from bobi.workflow.triggers import WorkflowDispatcher

    verdict = (verdict or "").strip()
    if verdict and verdict not in GATE_VERDICTS:
        raise ActionFailed(
            f"unknown verdict '{verdict}'; expected one of "
            f"{', '.join(GATE_VERDICTS)}")

    run = _waiting_run(root, run_id)

    # A rejection only means something if the workflow reads it. Without a
    # route in the slot a resume lands on, rejecting would run the next step -
    # advancing the very work the human just refused. Refuse instead, and say
    # which workflow cannot honour it.
    if verdict == GATE_VERDICT_REJECT:
        dispatcher = WorkflowDispatcher()
        dispatcher.load_all_workflows(project_path=root)
        workflow = dispatcher.find_workflow(run.workflow_name)
        if workflow is None:
            raise ActionFailed(
                f"workflow '{run.workflow_name}' is no longer installed")
        if not reads_gate_verdict(workflow, run.suspended_at_step):
            raise ActionFailed(
                f"workflow '{run.workflow_name}' has no route on the gate's "
                f"verdict, so a rejection cannot be honoured; resuming it "
                f"would run its next step")
    cmd = [sys.executable, "-m", "bobi.cli", "agent",
           paths.agent_name_for_root(root), "workflows", "resume", run_id]
    if verdict:
        cmd += ["--verdict", verdict]
    if reply:
        cmd += ["--reply", reply]
    subprocess.Popen(cmd, cwd=str(root), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Accepted, not finished. A workflow run takes as long as it takes; the
    # page watches the runs table for the status to move, the same
    # submit-then-poll discipline chat uses.
    return {"ok": True, "accepted": True, "run_id": run_id,
            "workflow": run.workflow_name,
            "await_event": run.await_event,
            "verdict": verdict}


def remind_run(root: Path, run_id: str) -> dict:
    """Re-send a waiting gate's notification. The run is untouched - this
    nudges the human, it does not advance the workflow."""
    from bobi.workflow.orchestrator import remind_workflow
    from bobi.workflow.triggers import WorkflowDispatcher

    run = _waiting_run(root, run_id)
    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows(project_path=root)
    workflow = dispatcher.find_workflow(run.workflow_name)
    if workflow is None:
        raise ActionFailed(
            f"workflow '{run.workflow_name}' is no longer installed")
    outcome = remind_workflow(run, workflow)
    if not outcome.delivered:
        raise ActionFailed(outcome.error)
    return {"ok": True, "delivered": True, "run_id": run_id,
            "workflow": run.workflow_name,
            "await_event": run.await_event}


def close_run(root: Path, run_id: str) -> dict:
    """Abandon a waiting run and mark its session cancelled.

    ``WorkflowRun.close`` is itself conditional on the run still being
    ``waiting``, so a race with a real event delivery is decided there and
    reported honestly rather than double-applied here.
    """
    from bobi.sdk import SessionRegistry
    from bobi.workflow.state import WorkflowRun

    _waiting_run(root, run_id)
    closed = WorkflowRun.close(run_id, root=root)
    if closed is None:
        raise ActionFailed(f"run {run_id} is no longer waiting")
    if closed.session_name:
        SessionRegistry(root).mark_terminal(
            closed.session_name, "cancelled",
            error="workflow closed by operator")
    return {"ok": True, "closed": True, "run_id": run_id,
            "workflow": closed.workflow_name}
