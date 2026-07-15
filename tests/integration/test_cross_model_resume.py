"""Live tests for cross-model session continuation (#642).

Proves the brain capability claims (``cross_model_resume=True``) against the
real CLIs: a session started under one model resumes under another with its
transcript intact, and a workflow spanning a model switch keeps conversation
state the fresh+reinject fallback would lose.

The Claude tests require the ``claude`` CLI. The Codex test (#649)
additionally needs ``BOBI_CODEX_XMODEL=<modelA>,<modelB>`` naming two models
the local account may use (e.g. ``gpt-5.4,gpt-5.5``) - the usable set depends
on the account's auth mode, so it cannot be hardcoded. All skipped in CI.
"""

import os
import shutil

import pytest
import yaml

from bobi.sdk import SessionRegistry
from .conftest import _drain, requires_claude

pytestmark = pytest.mark.claude


@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(180)
class TestBrainCrossModelResume:
    """The brain-level primitive: resume the same session on a new model."""

    async def test_resume_under_different_model_keeps_transcript(self):
        from bobi.brain import get_brain

        brain = get_brain("claude")
        assert brain.capabilities.cross_model_resume is True

        first = brain.make_session(
            cwd="/tmp",
            system_prompt="You are a test assistant. Reply concisely.",
            options={"model": "haiku", "max_turns": 3},
        )
        try:
            await first.connect(
                "Remember the code word: PANGOLIN. Reply with just: OK"
            )
            _, result = await _drain(first)
        finally:
            await first.disconnect()
        assert result is not None and not result.is_error
        session_id = result.session_id
        assert session_id

        # system_prompt=None keeps the Claude Code preset, whose system prompt
        # names the running model - the ground truth for "did the switch
        # actually happen" rather than just which model usage was reported.
        second = brain.make_session(
            cwd="/tmp",
            system_prompt=None,
            resume=session_id,
            options={"model": "sonnet", "max_turns": 3},
        )
        try:
            await second.connect(None)
            await second.query(
                "What was the code word, and which model are you powered by "
                "per your system prompt? Reply exactly as: "
                "WORD=<word> MODEL=<model name>"
            )
            text, result = await _drain(second)
        finally:
            await second.disconnect()

        assert result is not None and not result.is_error
        assert "PANGOLIN" in text.upper(), (
            f"transcript lost across the model switch: {text!r}"
        )
        # The turn must actually have run on the NEW model, not silently kept
        # the old one.
        assert "sonnet" in text.lower(), text
        assert "haiku" not in text.lower(), text


@requires_claude
@pytest.mark.timeout(300)
class TestWorkflowCrossModelContinuation:
    """A workflow step model switch keeps conversation state end to end."""

    def test_step_model_switch_keeps_conversation(self, bobi_env, clean_session):
        from bobi.workflow.orchestrator import make_session_name, run_workflow
        from bobi.workflow.schema import HandoffContract, StepDef, Workflow

        session_name = make_session_name("xmodel", "test-repo", "301")
        clean_session(session_name)

        # The code word lives ONLY in the step-1 conversation: the handoff
        # carries just an ack, so the fresh+reinject fallback could not
        # recover it. Step 2 succeeding proves the native continuation.
        wf = Workflow(name="xmodel", steps=[
            StepDef(
                name="seed", prompt=(
                    "The code word is PANGOLIN. Do not write it to any file "
                    "or repeat it yet. Write your handoff file with exactly "
                    "one field: ack: yes"
                ),
                model="haiku", timeout=90,
                handoff=HandoffContract(required=["ack"]),
            ),
            StepDef(
                name="recall", prompt=(
                    "State the code word from earlier in this conversation. "
                    "Write your handoff file with exactly one field: "
                    "word: <the code word>"
                ),
                model="sonnet", timeout=90,
                handoff=HandoffContract(required=["word"]),
            ),
        ])

        result = run_workflow(
            wf, task="Cross-model continuation test #301", repo="test-repo",
            cwd=str(bobi_env.project_path), run_key="301",
            timeout=240, interactive=False,
        )

        assert result is True
        handoff = yaml.safe_load(
            SessionRegistry().handoff_path(session_name, "recall").read_text()
        )
        assert "PANGOLIN" in str(handoff.get("word", "")).upper(), handoff


_codex_models = os.environ.get("BOBI_CODEX_XMODEL", "")

requires_codex_xmodel = pytest.mark.skipif(
    not shutil.which("codex") or "," not in _codex_models,
    reason="needs the codex CLI and BOBI_CODEX_XMODEL=<modelA>,<modelB>",
)


@requires_codex_xmodel
@pytest.mark.asyncio
@pytest.mark.timeout(240)
class TestCodexCrossModelResume:
    """`codex exec resume -m` switches the thread's model (#649)."""

    async def test_resume_under_different_model_keeps_transcript(self, tmp_path):
        from bobi.brain import get_brain

        model_a, model_b = (m.strip() for m in _codex_models.split(",", 1))
        brain = get_brain("codex")
        assert brain.capabilities.cross_model_resume is True

        first = brain.make_session(
            cwd=str(tmp_path),
            system_prompt="You are a test assistant. Reply concisely.",
            options={"model": model_a},
        )
        await first.connect(
            "Remember the code word: PANGOLIN. Reply with just: OK"
        )
        _, result = await _drain(first)
        await first.disconnect()
        assert result is not None and not result.is_error
        thread_id = result.session_id
        assert thread_id

        second = brain.make_session(
            cwd=str(tmp_path),
            system_prompt="You are a test assistant. Reply concisely.",
            resume=thread_id,
            options={"model": model_b},
        )
        await second.connect(None)
        await second.query(
            "What was the code word? Reply with just the word."
        )
        text, result = await _drain(second)
        await second.disconnect()

        assert result is not None and not result.is_error
        assert "PANGOLIN" in text.upper(), (
            f"transcript lost across the model switch: {text!r}"
        )
        # Ground truth that the switch happened: the rollout records the
        # model per turn_context.
        import json
        from pathlib import Path
        rollouts = sorted(
            Path.home().glob(f".codex/sessions/**/*{thread_id}*.jsonl")
        )
        assert rollouts, "no codex rollout found for the thread"
        models = []
        for line in rollouts[-1].read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "turn_context":
                models.append(event.get("payload", {}).get("model"))
        assert model_a in models and model_b in models, models
