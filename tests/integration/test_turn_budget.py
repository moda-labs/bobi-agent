"""Turn-budget e2e (#845): the cap is honest, configurable, and survivable.

Two real engineer sessions were killed on turn 201 by a hardcoded
``max_turns=200`` and reported as ``error="turn failed"`` - a literal fallback
the workflow drain substituted for the diagnosis the brain had already made.
Nothing in bobi's own session log or ``state.json`` recorded the real cause;
only the vendor CLI's private transcript did.

These tests drive the REAL orchestrator, registry, session log and stub brain
end to end and pin the three properties that failure needed:

1. a cap kill names itself (``max_turns_reached (max=…, turns=…)``) in
   ``state.json``, in the ``agent/session.failed`` payload and in the session
   log's ``stop`` record - never ``"turn failed"``;
2. a configured cap actually reaches the brain session; and
3. a cap kill is RESUMED rather than thrown away, so the run still finishes.

The stub brain is the right double here: the mechanism under test is bobi's
handling of a terminal ``TurnResult``, and ``__stub__:maxturns`` reproduces that
message's exact shape (including the empty ``result_text`` that made the bug
invisible). The one thing a stub cannot prove - that the claude CLI really
emits this shape - is already covered by ``tests/test_brain.py``'s
``max_turns_reached`` fixtures, taken from a real transcript.
"""

import json
import time

import pytest
import yaml

from bobi.brain import DEFAULT_MAX_TURNS
from bobi.sdk import SessionRegistry, _sessions_dir


@pytest.fixture
def captured_lifecycle(monkeypatch):
    """Record ``_emit_lifecycle_event`` payloads (no event server in the lane).

    The bus POST is the only seam stubbed: the orchestrator, brain, registry
    and session log are all real, so the durable half of every assertion below
    is read back off disk.
    """
    events = []

    def _capture(event_type, payload, blocking=False):
        events.append((event_type, payload))
        return True

    monkeypatch.setattr(
        "bobi.workflow.orchestrator._emit_lifecycle_event", _capture,
    )
    return events


def _one(events, event_type):
    matches = [p for t, p in events if t == event_type]
    assert matches, f"no {event_type} emitted; got {[t for t, _ in events]}"
    return matches[-1]


def _log_records(session_name, event):
    log_path = _sessions_dir() / session_name / "log.jsonl"
    assert log_path.exists(), f"no session log at {log_path}"
    return [
        entry for entry in
        (json.loads(line) for line in log_path.read_text().splitlines())
        if entry.get("event") == event
    ]


def _run_capped_workflow(bobi_env, run_key, *, step_max_turns=0):
    """Run a one-step workflow whose step is killed by the turn cap."""
    from bobi.workflow.orchestrator import make_session_name, run_workflow
    from bobi.workflow.schema import HandoffContract, StepDef, Workflow

    wf = Workflow(name="tcap", steps=[
        StepDef(
            name="build", prompt="__stub__:maxturns",
            max_turns=step_max_turns, timeout=60,
            handoff=HandoffContract(required=["status"]),
        ),
    ])
    session_name = make_session_name("tcap", "test-repo", run_key)
    result = run_workflow(
        wf, task="turn budget test", repo="test-repo",
        cwd=str(bobi_env.project_path), run_key=run_key,
        timeout=120, interactive=False,
    )
    return result, session_name


@pytest.mark.timeout(120)
class TestTurnCapIsNamed:
    """A cap kill reports the brain's diagnosis, not a literal fallback."""

    def test_terminal_error_names_max_turns_reached(
            self, stub_bobi_env, captured_lifecycle, monkeypatch):
        # Resume disabled, so the FIRST cap kill is terminal and its reporting
        # is what lands on disk. (Resume itself is proven below.)
        monkeypatch.setattr(
            "bobi.workflow.orchestrator.MAX_TURN_BUDGET_RESUMES", 0,
        )

        result, session_name = _run_capped_workflow(
            stub_bobi_env, "845a", step_max_turns=7,
        )
        assert result is False

        expected = "max_turns_reached (max=7, turns=8)"

        # 1. The durable record an operator reads first.
        state = json.loads(
            (_sessions_dir() / session_name / "state.json").read_text()
        )
        assert state["status"] == "failed"
        assert state["error"] == expected
        assert "turn failed" not in state["error"]

        # 2. The lifecycle payloads the manager and Slack render.
        failed = _one(captured_lifecycle, "agent/session.failed")
        assert failed["error"] == expected
        assert "unknown error" not in failed["error"]
        step_failed = _one(captured_lifecycle, "agent/step.failed")
        assert step_failed["error"] == expected
        assert step_failed["step"] == "build"

        # 3. The session log is self-sufficient: the `stop` record carries every
        # terminal fact, so diagnosing this never needs the CLI's transcript.
        stop = _log_records(session_name, "stop")[-1]
        assert stop["is_error"] is True
        assert stop["error_kind"] == "max_turns_reached"
        assert stop["error_message"] == expected
        assert stop["num_turns"] == 8
        for field in ("api_error_status", "duration_ms"):
            assert field in stop, f"stop record is missing {field}"


@pytest.mark.timeout(120)
class TestConfiguredCapIsHonored:
    """The cap reaching the brain session is the configured one, not a literal."""

    def _echo_options(self, bobi_env, run_key, **step_kwargs):
        from bobi.workflow.orchestrator import make_session_name, run_workflow
        from bobi.workflow.schema import StepDef, Workflow

        wf = Workflow(name="topt", steps=[
            StepDef(name="probe", prompt="__stub__:options", timeout=60,
                    **step_kwargs),
        ])
        session_name = make_session_name("topt", "test-repo", run_key)
        assert run_workflow(
            wf, task="__stub__:options", repo="test-repo",
            cwd=str(bobi_env.project_path), run_key=run_key,
            timeout=120, interactive=False,
        ) is True
        echoes = [
            json.loads(r["text"]) for r in
            _log_records(session_name, "response")
            if r.get("text", "").startswith("{")
        ]
        assert echoes, "the stub never echoed its session options"
        return echoes[-1]

    def test_framework_default_is_used_when_unconfigured(
            self, stub_bobi_env, captured_lifecycle):
        echoed = self._echo_options(stub_bobi_env, "845b")
        assert echoed["max_turns"] == DEFAULT_MAX_TURNS
        # The number the two dead sessions ran under is gone from the default.
        assert echoed["max_turns"] > 200

    def test_step_override_wins(self, stub_bobi_env, captured_lifecycle):
        # The single-step case: this override is also the step that constructs
        # the session. The multi-step case below is the one real workflows hit.
        echoed = self._echo_options(stub_bobi_env, "845c", max_turns=2500)
        assert echoed["max_turns"] == 2500

    def test_step_override_wins_on_a_later_step(
            self, stub_bobi_env, captured_lifecycle):
        """A step-level cap must apply to the step that DECLARES it.

        The regression test for the BLOCKING #845 review finding. Identical to
        test_step_override_wins except the override sits on the SECOND prompt
        step, with both steps on the same agent/model/effort - exactly the
        arrangement that used to stop the session from being rebuilt, so the
        second step silently ran under the first step's cap. Every multi-step
        workflow in the fleet has that shape (issue-lifecycle.yaml), as does
        the worked example in docs/WORKFLOW_ENGINE.md.

        Fails against the previous head (129d2bc) with `assert 1000 == 2500`
        - the framework default the FIRST step resolved.
        """
        from bobi.workflow.orchestrator import make_session_name, run_workflow
        from bobi.workflow.schema import StepDef, Workflow

        wf = Workflow(name="topt2", steps=[
            StepDef(name="first", prompt="__stub__:options", timeout=60),
            StepDef(name="second", prompt="__stub__:options", timeout=60,
                    max_turns=2500),
        ])
        session_name = make_session_name("topt2", "test-repo", "845c2")
        assert run_workflow(
            wf, task="__stub__:options", repo="test-repo",
            cwd=str(stub_bobi_env.project_path), run_key="845c2",
            timeout=120, interactive=False,
        ) is True
        echoes = [
            json.loads(r["text"]) for r in
            _log_records(session_name, "response")
            if r.get("text", "").startswith("{")
        ]
        assert len(echoes) >= 2, "both steps should have echoed their options"
        # The first step resolved the framework default; the second declared
        # its own cap and must run under it.
        assert echoes[0]["max_turns"] == DEFAULT_MAX_TURNS
        assert echoes[-1]["max_turns"] == 2500

    def test_a_cap_change_resumes_rather_than_starting_fresh(
            self, stub_bobi_env, captured_lifecycle):
        """The reason the fix above is cheap: continuity is not the price.

        The cap is a construction-time CLI flag, so honoring a per-step
        override means rebuilding the session - which is what made the gap look
        like a design trade against the conversational continuity #642/#778
        protect. It is not one. A cap change never changes the MODEL, so
        ``continuation_token`` returns the saved session id (same model on both
        sides always continues) and the rebuild resumes the SAME transcript,
        exactly as an effort-only change already does.

        Asserted on the session id the run records rather than on a log
        message, because a fresh session would have minted a different one.
        The cap assertion is what keeps this honest: without it the test would
        pass vacuously on the previous head, where nothing rebuilt at all.
        """
        from bobi.workflow.orchestrator import make_session_name, run_workflow
        from bobi.workflow.schema import StepDef, Workflow

        wf = Workflow(name="topt3", steps=[
            StepDef(name="first", prompt="__stub__:options", timeout=60),
            StepDef(name="second", prompt="__stub__:options", timeout=60,
                    max_turns=2500),
        ])
        session_name = make_session_name("topt3", "test-repo", "845c3")
        assert run_workflow(
            wf, task="__stub__:options", repo="test-repo",
            cwd=str(stub_bobi_env.project_path), run_key="845c3",
            timeout=120, interactive=False,
        ) is True

        # A rebuild really did happen (else this test proves nothing).
        echoes = [
            json.loads(r["text"]) for r in
            _log_records(session_name, "response")
            if r.get("text", "").startswith("{")
        ]
        assert echoes[-1]["max_turns"] == 2500

        # ...and it resumed rather than starting over.
        ids = [
            r["session_id"] for r in _log_records(session_name, "stop")
            if r.get("session_id")
        ]
        assert len(ids) >= 2, f"expected a stop record per step, got {ids}"
        assert len(set(ids)) == 1, (
            f"the cap change started a FRESH session instead of resuming the "
            f"transcript: {ids}"
        )

    def test_role_config_beats_the_framework_default(
            self, stub_bobi_env, captured_lifecycle):
        from bobi import paths

        # The installed image is read-only; an operator tuning a role's cap
        # edits it in place, so do exactly that and restore mode + content.
        agent_yaml = paths.agent_yaml_path(stub_bobi_env.project_path)
        original = agent_yaml.read_text()
        original_mode = agent_yaml.stat().st_mode
        raw = yaml.safe_load(original)
        raw["roles"] = {"engineer": {"max_turns": 4242}}

        def _write(text):
            # The runtime re-locks the installed manifest, so unlock per write.
            agent_yaml.chmod(0o644)
            agent_yaml.write_text(text)

        _write(yaml.dump(raw))
        try:
            echoed = self._echo_options(
                stub_bobi_env, "845d", agent="engineer",
            )
        finally:
            _write(original)
            agent_yaml.chmod(original_mode)
        assert echoed["max_turns"] == 4242


@pytest.mark.timeout(180)
class TestTurnCapIsResumable:
    """A cap kill restarts the step on the saved transcript instead of ending
    the run - the only outcome that would have saved the 227 minutes of work
    the first dead session lost."""

    def test_capped_step_resumes_and_completes(
            self, stub_bobi_env, captured_lifecycle):
        # The step prompt carries the directive, so the first turn is capped.
        # The resume nudge does not, so the continued session completes - the
        # same asymmetry a real resumed CLI process has (fresh turn budget).
        result, session_name = _run_capped_workflow(
            stub_bobi_env, "845e", step_max_turns=7,
        )

        registry = SessionRegistry()
        handoff = registry.handoff_path(session_name, "build")
        # The stub writes no handoff, so the run still fails validation - what
        # is under test is that it got PAST the cap to the handoff contract at
        # all, instead of dying at the drain.
        assert result is False
        assert not handoff.exists()

        resumes = _log_records(session_name, "turn_budget_resume")
        assert len(resumes) == 1, resumes
        assert resumes[0]["step"] == "build"
        assert resumes[0]["max_turns"] == 7
        assert resumes[0]["error"] == "max_turns_reached (max=7, turns=8)"

        # It failed on the handoff contract, NOT on the turn cap.
        failed = _one(captured_lifecycle, "agent/session.failed")
        assert "max_turns_reached" not in failed["error"]
        assert "handoff" in failed["error"].lower(), failed["error"]

    def test_resume_is_bounded(self, stub_bobi_env, captured_lifecycle,
                               monkeypatch):
        """A step that keeps hitting the cap gives up with the honest error."""
        from bobi.workflow import orchestrator

        monkeypatch.setattr(orchestrator, "MAX_TURN_BUDGET_RESUMES", 2)
        # Every continuation carries the directive too, so the cap fires on
        # every attempt - the runaway-loop case the backstop exists for.
        monkeypatch.setattr(
            orchestrator, "_turn_budget_resume_prompt",
            lambda step, final_try: "__stub__:maxturns continue",
            raising=False,
        )

        result, session_name = _run_capped_workflow(
            stub_bobi_env, "845f", step_max_turns=7,
        )
        assert result is False
        assert len(_log_records(session_name, "turn_budget_resume")) == 2
        failed = _one(captured_lifecycle, "agent/session.failed")
        assert failed["error"] == "max_turns_reached (max=7, turns=8)"
