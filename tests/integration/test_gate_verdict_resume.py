"""Answering a human gate, through the real CLI and a real workflow (#987).

`tests/test_orchestrator.py::TestGateVerdictRouting` proves which STEP each
verdict runs, with a fake brain. This proves the hop that sits between the
console and that engine: `run_actions.resume_run` spawns a detached
`bobi agent <name> workflows resume <id> --verdict ...`, and nothing in that
subprocess is exercised by asserting on the Popen args.

So each test here suspends a real run on a real installed workflow, then runs
the real command as a subprocess against the isolated stub-brain home, and
reads the run records back off disk. The three endings are unambiguous there:

  approve   the record is `completed` and no run is left waiting
  reject    the record is `superseded` and a FRESH record waits at the gate
            again - the same session, the same run key, reworked not built
  neither   an empty or malformed verdict ends exactly like a rejection,
            because the workflow's route makes its `else` the safe branch

Stub brain only, and deliberately: what is under test is the verdict's route
through argument parsing, the event scope, and the step loop. A real model
cannot change where a route step sends a run, so a claude leg would spend
money re-proving a branch it has no say in (the exemption CLAUDE.md's
"one mechanism, two brains" rule names).
"""

from __future__ import annotations

import textwrap

import pytest

WORKFLOW = "gated-review"
RUN_KEY = "987"

# The issue-lifecycle gate in miniature: the rework target sits BEFORE the
# await, so rejecting is a back edge through the gate rather than a step that
# has to be inserted after it. The route's condition names the one verdict
# that advances; everything else - including the verdict that never arrived -
# takes the else.
WORKFLOW_YAML = textwrap.dedent("""\
    name: gated-review
    trigger: manual
    steps:
      - name: spec
        prompt: |
          Write the spec. Verdict on the last round: ${{event.verdict}}
      - name: await_approval
        await: approval
      - name: approval_route
        if: "${{event.verdict}} == 'approve'"
        goto: implement
        else: spec
      - name: implement
        prompt: "Build it."
""")


@pytest.fixture
def gated(stub_bobi_env):
    """Install the gated workflow into the isolated stub-brain team.

    The installed package image is read-only at runtime, so writing a
    workflow into it goes through the same guard a real install edit would.
    """
    from bobi import paths
    from bobi.runtime_guard import with_mutable_runtime_package

    path = stub_bobi_env.workflows_dir / f"{WORKFLOW}.yaml"
    with with_mutable_runtime_package(stub_bobi_env.project_path):
        path.write_text(WORKFLOW_YAML)
    # The stub home is module-scoped, so every test starts from an empty run
    # store: these assertions are about WHICH records exist, and a leftover
    # waiting run from the previous test reads as this one having re-gated.
    runs_dir = (paths.state_path(stub_bobi_env.project_path)
                / "workflow" / "runs")
    for stale in runs_dir.glob("*.json"):
        stale.unlink()
    try:
        yield stub_bobi_env
    finally:
        with with_mutable_runtime_package(stub_bobi_env.project_path):
            path.unlink(missing_ok=True)


def _runs():
    from bobi.workflow.state import WorkflowRun
    return WorkflowRun.list_runs()


def _waiting():
    return [r for r in _runs() if r.status == "waiting"]


def _park(gated):
    """Run the workflow from the top until its gate parks it."""
    from bobi.workflow.orchestrator import run_workflow
    from bobi.workflow.schema import load_workflow

    wf = load_workflow(gated.workflows_dir / f"{WORKFLOW}.yaml")
    assert run_workflow(
        wf, task="Gate the spec", repo="test-repo",
        cwd=str(gated.project_path), run_key=RUN_KEY,
        timeout=120, interactive=False) is True

    waiting = _waiting()
    assert len(waiting) == 1, [(r.run_id, r.status) for r in _runs()]
    run = waiting[0]
    # The +1 the engine already writes: a resume lands on the ROUTE, which is
    # what lets the verdict decide instead of the resume itself.
    assert run.suspended_at_step == 2
    assert run.await_event == "approval"
    return run


def _resume(cli_run, run_id, *args):
    result = cli_run("workflows", "resume", run_id, *args, timeout=180)
    return result


@pytest.mark.timeout(600)
def test_an_approve_resumes_and_the_run_finishes(gated, stub_cli_run):
    run = _park(gated)

    result = _resume(stub_cli_run, run.run_id, "--verdict", "approve")

    assert result.returncode == 0, result.stderr
    assert "with verdict 'approve'" in result.stdout
    assert "Workflow completed." in result.stdout

    from bobi.workflow.state import WorkflowRun
    assert WorkflowRun.load(run.run_id).status == "completed"
    assert _waiting() == [], "an approved run is still parked at its gate"


@pytest.mark.timeout(600)
def test_a_reject_reworks_the_same_run_and_re_gates_it(gated, stub_cli_run):
    """Not a dead end and not a fresh session: the same work, reworked.

    The record that ran is stamped `superseded` rather than `completed` - a
    re-suspend mints a new waiting record, and calling the old one done is
    what made a dormant run read as finished.
    """
    run = _park(gated)

    result = _resume(stub_cli_run, run.run_id,
                     "--verdict", "reject", "--reply", "widen the scope")

    assert result.returncode == 0, result.stderr
    assert "Workflow completed." not in result.stdout
    assert "suspended again" in result.stdout

    from bobi.workflow.state import WorkflowRun
    assert WorkflowRun.load(run.run_id).status == "superseded"

    waiting = _waiting()
    assert len(waiting) == 1, [(r.run_id, r.status) for r in _runs()]
    regated = waiting[0]
    assert regated.run_id != run.run_id
    assert regated.session_name == run.session_name
    assert regated.run_key == run.run_key
    assert regated.suspended_at_step == 2


@pytest.mark.timeout(600)
def test_a_resume_with_no_verdict_reworks_rather_than_advancing(
        gated, stub_cli_run):
    """The sharp edge, through the real command.

    A missing scope resolves to "" with only a log warning, so an unanswered
    resume takes whichever branch is the `else`. It has to be the safe one:
    reworking a spec nobody approved costs a round trip, building one is the
    failure this whole change exists to remove.
    """
    run = _park(gated)

    result = _resume(stub_cli_run, run.run_id)

    assert result.returncode == 0, result.stderr
    assert "suspended again" in result.stdout

    from bobi.workflow.state import WorkflowRun
    assert WorkflowRun.load(run.run_id).status == "superseded"
    assert len(_waiting()) == 1


@pytest.mark.timeout(600)
def test_a_malformed_verdict_is_refused_and_the_gate_still_holds(
        gated, stub_cli_run):
    """Refused at the boundary, with the run left exactly as it was: still
    waiting, still claimable, still answerable by someone who types a verdict
    the vocabulary contains."""
    run = _park(gated)

    result = _resume(stub_cli_run, run.run_id, "--verdict", "approved")

    assert result.returncode != 0
    assert "approved" in result.stderr

    from bobi.workflow.state import WorkflowRun
    reloaded = WorkflowRun.load(run.run_id)
    assert reloaded.status == "waiting"
    assert reloaded.suspended_at_step == 2
    assert [r.run_id for r in _waiting()] == [run.run_id]
