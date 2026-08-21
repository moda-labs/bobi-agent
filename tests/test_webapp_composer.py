"""The run slab's reply box: which branch a row gets, and what it posts (#987).

Two things are pinned here, and only two, because the rest of the composer is
behaviour in a browser and belongs to `tests/e2e/test_webapp_ui.py`:

1. **The branch.** A live session is delivered to, a parked gate is answered,
   and a row that is neither gets no control at all. Getting that wrong is
   invisible in a screenshot: a gate rendered as an ordinary reply box looks
   exactly like a working one, and swallows the verdict.

2. **The verdict payload.** It is the entire contract with the resume route -
   the workflow's own route step reads it back as `${{event.verdict}}` - and
   it is never inferred from what the operator typed.

Asserting on the JS source would prove nothing about what it returns, so this
runs the real module under Node and reads back its values.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

VIEWS = Path(__file__).resolve().parent.parent / "bobi" / "webapp" / "static" / "views"
MODULE = VIEWS / "composer.js"
VIEW = VIEWS / "agent.js"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        # Never skip on CI: a silently skipped test is a green that proves
        # nothing (the lesson of this repo's vacuous live lanes).
        if os.environ.get("CI"):
            pytest.fail("node is required to run the webapp composer tests")
        pytest.skip("node not on PATH")
    return node


def _call(fn: str, *args) -> object:
    """Call one exported function through the real module, as JSON in and out.

    The module is pure, so it imports cleanly under Node with no DOM and no
    stubs. That is the reason it is a module.
    """
    script = (
        "const m = await import(process.argv[1]);"
        "const args = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(m[process.argv[2]](...args)));"
    )
    proc = subprocess.run(
        [_node(), "--input-type=module", "-e", script, MODULE.as_uri(),
         fn, json.dumps(list(args))],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


GATE_ROW = {
    "kind": "workflow",
    "title": "issue-lifecycle",
    "status": "awaiting_action",
    "session_id": "wf-issue-lifecycle-eng-team-987",
    "run_id": "11d31ce5",
    "detail": {"await_event": "approval", "suspended_at_step": 7,
               "run_key": "987", "repo": "bobi-agent", "live": False},
}
LIVE_ROW = {
    "kind": "session", "title": "Fix the flaky test", "status": "running",
    "session_id": "worker-a", "run_id": "",
    "detail": {"role": "engineer", "live": True},
}


def test_module_exists() -> None:
    assert MODULE.is_file(), f"missing {MODULE}"
    # The view reaches the decision through the module rather than inlining a
    # copy, which is the only reason the tests below can see it at all.
    assert 'from "./composer.js"' in VIEW.read_text()


class TestComposerMode:
    def test_a_live_session_is_delivered_to(self) -> None:
        assert _call("composerMode", LIVE_ROW) == "live"

    def test_a_parked_gate_is_answered(self) -> None:
        assert _call("composerMode", GATE_ROW) == "gate"

    def test_liveness_wins_over_the_gate_status(self) -> None:
        """A gate whose session is somehow still live is chatted with, not
        answered: there is a process reading, and the composer's job is to
        reach whoever can act."""
        row = dict(GATE_ROW, detail=dict(GATE_ROW["detail"], live=True))
        assert _call("composerMode", row) == "live"

    def test_a_finished_session_with_no_gate_gets_no_control(self) -> None:
        """The branch that replaced the manager relay. Nothing is behind this
        row and there is no verdict to give, so offering a box would promise a
        delivery that cannot happen."""
        row = dict(LIVE_ROW, status="done",
                   detail={"role": "engineer", "live": False})
        assert _call("composerMode", row) == "ended"

    def test_an_awaiting_row_with_no_run_is_not_a_gate(self) -> None:
        """The verdict is delivered by resuming a run. A row without one has
        nothing to resume, so it must not offer Approve - a button that posts
        to `/workflows/runs//resume` is a 404 dressed as an approval."""
        row = dict(GATE_ROW, run_id="")
        assert _call("composerMode", row) == "ended"

    def test_a_row_with_no_detail_at_all_degrades(self) -> None:
        assert _call("composerMode", {"status": "done"}) == "ended"


class TestResumeBody:
    def test_the_verdict_is_what_was_clicked(self) -> None:
        assert _call("resumeBody", "approve", "ship it") == {
            "verdict": "approve", "reply": "ship it"}
        assert _call("resumeBody", "reject", "not yet") == {
            "verdict": "reject", "reply": "not yet"}

    def test_the_verdict_is_never_read_out_of_the_text(self) -> None:
        """An operator who types "looks fine to me" and clicks Reject has
        rejected it. Nothing here parses their prose for an intent."""
        assert _call("resumeBody", "reject", "approve this please") == {
            "verdict": "reject", "reply": "approve this please"}

    def test_the_reason_is_optional_and_trimmed(self) -> None:
        assert _call("resumeBody", "approve", "   ") == {
            "verdict": "approve", "reply": ""}
        assert _call("resumeBody", "approve", None) == {
            "verdict": "approve", "reply": ""}
