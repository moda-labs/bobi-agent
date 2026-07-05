"""Live tests for cross-model session continuation (#642).

Proves the ClaudeBrain capability claim (``cross_model_resume=True``) against
the real CLI: a session started under one model resumes under another with its
transcript intact, and a workflow spanning a model switch keeps conversation
state the fresh+reinject fallback would lose.

Requires the ``claude`` CLI. Skipped in CI.
"""

import pytest
import yaml

from bobi.sdk import SessionRegistry
from .conftest import requires_claude

pytestmark = pytest.mark.claude


async def _drain(client):
    """Drain one turn; return (final_text, turn_result)."""
    from bobi.brain import AssistantText, TurnResult

    text, result = "", None
    async for msg in client.receive_response():
        if isinstance(msg, AssistantText) and msg.text:
            text = msg.text
        elif isinstance(msg, TurnResult):
            result = msg
    return text, result


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
        # actually happen" (TurnResult.costs carries no model name on this
        # path: the SDK's dict-shaped model_usage is a known legacy no-op).
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
            SessionRegistry.handoff_path(session_name, "recall").read_text()
        )
        assert "PANGOLIN" in str(handoff.get("word", "")).upper(), handoff
