"""A re-dispatch can start a new transcript without renaming the run.

Session names are deterministic on purpose: `orchestrator._setup_worktree`
names the git branch after the session, `launch_agent`'s admission check
dedupes on it, and operators read it off the registry. So a re-dispatch of the
same unit reuses the name — and, before `fresh`, silently resumed the previous
run's transcript along with its spent turn budget.

That is worst on the adhoc path, where `spawn_adhoc` derives the name from the
task: re-running an identical task string collides by construction. A worker
whose state lives in a committed artifact rather than in context wants the
stable name and a clean transcript, which is what `fresh` buys.

Under an EXPLICIT run key the default stays resume - that is the workflow
engine's documented retry contract (`launch_agent`'s docstring) - so every test
here has a default-behavior twin. Without it the `fresh` assertions would pass
vacuously against a code path that never resumed in the first place.

Under a DERIVED key it does not: #850 made every un-keyed launch land on a
stable name, and a name the caller never chose is not an assertion that this
continues an earlier run. Deriving implies `fresh`, which is also what an
un-keyed launch always got before #850, when its name was random.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from bobi.session import Session


class _Stop(Exception):
    """Ends `_run` right after the resume decision, which is all we assert."""


def _drive_run(*, fresh: bool, saved: str = "dead-session-id") -> tuple[dict, MagicMock]:
    """Run `Session._run` until the brain session is built, and report how.

    Returns the captured `make_brain_session` kwargs and the patched
    `load_resumable_session_id` mock. `_make_brain_session` is called OUTSIDE
    `_run`'s connect try/except, so raising there stops the coroutine without
    tripping the resume-failed fallback.
    """
    captured: dict = {}
    session = Session(name="wf-adhoc-repo-UNIT-1", cwd="/tmp", fresh=fresh)

    def _make(resume=None):
        captured["resume"] = resume
        raise _Stop

    session._make_brain_session = _make

    with patch("bobi.session.load_resumable_session_id",
               return_value=saved) as load_mock:
        with pytest.raises(_Stop):
            asyncio.run(session._run())

    return captured, load_mock


class TestSessionFresh:
    def test_fresh_does_not_resume_the_saved_transcript(self):
        """MUTATION-PROOF. Restore `_run`'s unconditional
        `load_resumable_session_id(...)` and this fails: the dead id comes back
        as `resume`, which is the whole defect."""
        captured, load_mock = _drive_run(fresh=True)

        assert captured["resume"] is None
        # Not merely "resolved to empty" — the lookup is not attempted at all.
        load_mock.assert_not_called()

    def test_default_still_resumes(self):
        """The non-vacuity guard for the test above. A negative assertion goes
        green both when the guard fires and when the path was never reachable;
        this proves the path is reachable and that `fresh` is what changed it."""
        captured, load_mock = _drive_run(fresh=False)

        assert captured["resume"] == "dead-session-id"
        load_mock.assert_called_once()

    def test_fresh_defaults_to_false(self):
        """The engine's retry contract is unchanged by this feature existing."""
        assert Session(name="n", cwd="/tmp")._fresh is False


class TestSpawnAdhocFresh:
    """The adhoc path, where the name collides by construction."""

    def _capture(self, captured: dict):
        def _cls(*args, **kwargs):
            captured.update(kwargs)
            return MagicMock(
                start=MagicMock(return_value=False),  # short-circuit the run
                _total_duration_ms=0, _total_cost_usd=0.0, _total_turns=0,
            )
        return _cls

    def test_fresh_reaches_the_session(self, bobi_install):
        from bobi.subagent import spawn_adhoc

        captured: dict = {}
        with patch("bobi.session.Session", side_effect=self._capture(captured)), \
             patch("bobi.subagent._emit_lifecycle_event"):
            spawn_adhoc(cwd="/tmp", task="work the checklist", fresh=True)

        assert captured["fresh"] is True

    def test_a_derived_name_implies_fresh(self, bobi_install):
        """No `name` given, so the name is inferred from the task - and an
        inference is not the caller saying "continue that run" (#850)."""
        from bobi.subagent import spawn_adhoc

        captured: dict = {}
        with patch("bobi.session.Session", side_effect=self._capture(captured)), \
             patch("bobi.subagent._emit_lifecycle_event"):
            spawn_adhoc(cwd="/tmp", task="work the checklist")

        assert captured["fresh"] is True

    def test_an_explicit_name_keeps_the_resume_default(self, bobi_install):
        """The non-vacuity twin: `fresh` is still opt-in where a caller named
        the run, which is the engine's retry contract."""
        from bobi.subagent import spawn_adhoc

        captured: dict = {}
        with patch("bobi.session.Session", side_effect=self._capture(captured)), \
             patch("bobi.subagent._emit_lifecycle_event"):
            spawn_adhoc(cwd="/tmp", task="work the checklist", name="unit-3")

        assert captured["fresh"] is False

    def test_the_same_task_text_collides_on_one_session_name(self, bobi_install):
        """Why `fresh` exists at all, pinned as behavior rather than asserted in
        a comment. Two dispatches of an identical task resolve to ONE session
        name, so the second inherits the first's transcript unless it opts out.
        A checklist re-dispatch is exactly this shape: the task string is a
        pointer to the artifact and does not change between attempts."""
        from bobi.subagent import spawn_adhoc

        names: list[str] = []

        def _cls(*args, **kwargs):
            names.append(kwargs["name"])
            return MagicMock(start=MagicMock(return_value=False))

        with patch("bobi.session.Session", side_effect=_cls), \
             patch("bobi.subagent._emit_lifecycle_event"):
            spawn_adhoc(cwd="/tmp", task="work the checklist at plans/x.md")
            spawn_adhoc(cwd="/tmp", task="work the checklist at plans/x.md")

        assert names[0] == names[1]
        assert names[0].startswith("adhoc-")


class TestLaunchPlumbing:
    """`fresh` survives the trip through the detached-subprocess arg blob."""

    def test_launch_agent_puts_fresh_in_the_args_blob(self, bobi_install):
        from bobi.subagent import launch_agent

        captured: dict = {}

        def _fake_launch(script, args, log_file, env=None):
            captured["args"] = json.loads(args[0])
            return 4242

        with patch("bobi.subagent._launch_detached", side_effect=_fake_launch), \
             patch("bobi.subagent.check_requires", return_value=[]), \
             patch("bobi.subagent._check_concurrency_semaphore"), \
             patch("bobi.subagent._check_spend_governor"), \
             patch("bobi.sdk.check_image_rotation"), \
             patch("bobi.subagent._emit_lifecycle_event"):
            launch_agent(task="t", cwd="/tmp", workflow_name="adhoc",
                         role="engineer", run_key="UNIT-1", fresh=True)

        assert captured["args"]["fresh"] is True

    def test_entry_defaults_fresh_when_the_blob_predates_it(self, bobi_install):
        """An older spawner's blob has no `fresh` key. Default to the historical
        resume behavior rather than silently starting fresh — a manager running
        older code must not have its retry semantics changed underneath it."""
        from bobi.subagent import _run_agent_entry

        with patch("bobi.subagent.spawn_adhoc") as spawn, \
             patch("bobi.subagent.pin_brain_from_root"), \
             patch("bobi.brain.instructions.render_team_instructions"):
            _run_agent_entry({
                "task": "t", "cwd": "/tmp", "workflow_name": "adhoc",
                "root": str(bobi_install.repo_path), "persistent": True,
            })

        assert spawn.call_args.kwargs["fresh"] is False

    def test_entry_forwards_fresh_when_present(self, bobi_install):
        from bobi.subagent import _run_agent_entry

        with patch("bobi.subagent.spawn_adhoc") as spawn, \
             patch("bobi.subagent.pin_brain_from_root"), \
             patch("bobi.brain.instructions.render_team_instructions"):
            _run_agent_entry({
                "task": "t", "cwd": "/tmp", "workflow_name": "adhoc",
                "root": str(bobi_install.repo_path), "persistent": True,
                "fresh": True,
            })

        assert spawn.call_args.kwargs["fresh"] is True


class TestFreshFlag:
    """`--fresh` is the operator-facing half: the re-dispatch path is a human
    typing a command, so the flag has to exist and reach the launcher."""

    def test_flag_reaches_launch_agent(self, bobi_install):
        from click.testing import CliRunner

        from bobi.cli import main
        from tests.conftest import TEST_AGENT_NAME

        with patch("bobi.subagent.launch_agent", return_value="wf-adhoc-1") as mock:
            result = CliRunner().invoke(main, [
                "agent", TEST_AGENT_NAME, "subagents", "launch",
                "-w", "adhoc", "--role", "engineer",
                "--task", "work the checklist at plans/x.md", "--fresh",
            ])

        assert result.exit_code == 0, result.output
        assert mock.call_args[1]["fresh"] is True

    def test_absent_flag_leaves_the_retry_contract_alone(self, bobi_install):
        from click.testing import CliRunner

        from bobi.cli import main
        from tests.conftest import TEST_AGENT_NAME

        with patch("bobi.subagent.launch_agent", return_value="wf-adhoc-1") as mock:
            result = CliRunner().invoke(main, [
                "agent", TEST_AGENT_NAME, "subagents", "launch",
                "-w", "adhoc", "--role", "engineer", "--task", "t",
            ])

        assert result.exit_code == 0, result.output
        assert mock.call_args[1]["fresh"] is False
