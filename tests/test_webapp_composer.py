"""The run slab's reply box: the relay it composes, and the call it must
never gain (#987).

Two things are pinned here, and only two, because the rest of the composer is
behaviour in a browser and belongs to `tests/e2e/test_webapp_ui.py`:

1. **The relay payload.** On a run whose session has ended there is nobody to
   deliver to, so the text goes to the team manager instead. That message is
   the entire contract with the manager - it has to name the target session,
   the run, and the operator's own words, or a fresh session starts with no
   idea what it is continuing. Asserting on the JS source would prove nothing
   about what it builds, so this runs the real module under Node and reads
   back what it returns (the `test_webapp_markdown.py` pattern).

2. **The absence of a resume call.** A suspended workflow run records the step
   AFTER its gate (`run.suspended_at_step = step_idx + 1`), and
   `resume_workflow` feeds that back as the starting step. So resuming a
   parked run from this page would skip the approval it is waiting for and
   begin the next step - for `issue-lifecycle`, writing code against a spec
   nobody approved. The page has never had a resume call. This test is what
   keeps it that way, and it is a source-text assertion on purpose: the point
   is that the call cannot be reached because it does not exist.
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


def relay(row: dict, text: str) -> str:
    """Build the relay through the real module and return it verbatim.

    The module is pure text, so it imports cleanly under Node with no DOM
    and no stubs. That is the reason it is a module.
    """
    script = (
        "const { continuationRelay } = await import(process.argv[1]);"
        "process.stdout.write(continuationRelay("
        "  JSON.parse(process.argv[2]), process.argv[3]));"
    )
    proc = subprocess.run(
        [_node(), "--input-type=module", "-e", script, MODULE.as_uri(),
         json.dumps(row), text],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout


GATE_ROW = {
    "kind": "workflow",
    "title": "issue-lifecycle",
    "status": "awaiting_action",
    "session_id": "wf-issue-lifecycle-eng-team-987",
    "run_id": "11d31ce5",
    "detail": {"await_event": "approval", "suspended_at_step": 7,
               "run_key": "987", "repo": "bobi-agent", "live": False},
}


def test_module_exists() -> None:
    assert MODULE.is_file(), f"missing {MODULE}"
    # The view reaches the relay through the module rather than inlining a
    # copy, which is the only reason the tests above can see it at all.
    assert 'from "./composer.js"' in VIEW.read_text()


class TestRelay:
    def test_it_names_the_target_session_and_its_run(self) -> None:
        """What the manager needs to find the work on disk.

        Every one of these is read off the row the modal already has: the
        session name, the run id, the run key, and the step it is parked at.
        """
        out = relay(GATE_ROW, "please pick this up")
        assert "target_session:    wf-issue-lifecycle-eng-team-987" in out
        assert "run_id:            11d31ce5" in out
        assert "run_key:           987" in out
        assert "workflow:          issue-lifecycle" in out
        assert "awaiting:          approval" in out
        assert "suspended_at_step: 7" in out

    def test_the_operator_text_survives_verbatim_and_delimited(self) -> None:
        """Their words are a payload, not part of the console's framing.

        Delimiting matters: an operator who writes something that reads like
        an instruction to the console must not have it merged into the
        console's own instruction.
        """
        typed = "Do NOT ship this.\n\nAsk Luke first: is the spec still right?"
        out = relay(GATE_ROW, typed)
        body = out.split("--- the operator's message, verbatim ---\n")[1]
        assert body.split("\n--- end of message ---")[0] == typed

    def test_it_forbids_resuming_the_parked_run_by_id(self) -> None:
        """The instruction agrees with the absence of the button.

        A resume starts at the step after the gate, so the relay names the run
        that must not be resumed and says why.
        """
        out = relay(GATE_ROW, "carry on")
        assert "Do NOT resume run 11d31ce5" in out
        assert "step AFTER its gate" in out
        assert "FRESH session" in out

    def test_it_points_at_the_durable_context_a_fresh_session_can_read(
            self) -> None:
        """A fresh session cannot inherit the brain transcript (it is keyed by
        session name, and that name is the workflow's), but the run's handoffs
        are plain files. The relay says where they are."""
        out = relay(GATE_ROW, "carry on")
        assert "state/sessions/wf-issue-lifecycle-eng-team-987/" in out
        assert "variable scopes" in out

    def test_a_plain_session_row_degrades_without_inventing_a_run(self) -> None:
        """A finished subagent has no run id, run key or gate. The relay must
        carry what exists and claim nothing that does not - a fabricated run
        id would send the manager looking for a record that is not there."""
        out = relay({"kind": "session", "title": "Fix the flaky test",
                     "status": "done", "session_id": "worker-a", "run_id": "",
                     "detail": {"role": "engineer", "live": False}},
                    "what happened here?")
        assert "target_session:    worker-a" in out
        assert "run_id:" not in out
        assert "run_key:" not in out
        assert "awaiting:" not in out
        # No run to resume, so no warning about resuming one.
        assert "resume" not in out
        assert "FRESH session" in out

    def test_a_step_index_of_zero_is_still_a_step(self) -> None:
        """`suspended_at_step` is -1 when there is none, so the field cannot be
        tested for truthiness: step 0 is a real step."""
        row = dict(GATE_ROW, detail=dict(GATE_ROW["detail"],
                                         suspended_at_step=0))
        assert "suspended_at_step: 0" in relay(row, "hi")

        row = dict(GATE_ROW, detail=dict(GATE_ROW["detail"],
                                         suspended_at_step=-1))
        assert "suspended_at_step:" not in relay(row, "hi")


class TestNoResumeCall:
    """The sharp edge, pinned in the one place it could be introduced."""

    def test_the_agent_view_has_no_call_to_the_resume_route(self) -> None:
        src = VIEW.read_text()
        assert "/resume" not in src, (
            "agent.js gained a call to the workflow resume route. A resume "
            "starts at the step AFTER the gate, so this would advance a "
            "workflow past an approval nobody gave.")

    def test_no_view_reaches_the_resume_route(self) -> None:
        # The composer could be lifted into another view as easily as this
        # one, so the guard covers the whole directory rather than one file.
        for view in sorted(VIEWS.glob("*.js")):
            assert "/resume" not in view.read_text(), view.name

    def test_no_read_or_write_call_in_the_view_mentions_resume(self) -> None:
        """Belt to the above's braces: the route could be assembled rather
        than written out. Every `api(...)` line is checked for the word."""
        offenders = [line.strip() for line in VIEW.read_text().splitlines()
                     if "api(" in line and "resume" in line]
        assert not offenders, offenders
