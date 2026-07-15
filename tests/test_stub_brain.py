"""Unit coverage for the programmable stub brain (the shared test double).

Proves the contract both the public integration suites and the private deploy
e2e rely on: the env gate, the terminal-marker shape (AssistantText* then one
TurnResult, then stop), and each scripted directive.
"""

from __future__ import annotations

import asyncio

import pytest

from bobi.brain import get_brain
from bobi.brain.base import AssistantText, TurnResult
from bobi.brain.stub import STUB_BRAIN_ENV, StubBrain


@pytest.fixture
def stub_enabled(monkeypatch):
    monkeypatch.setenv(STUB_BRAIN_ENV, "1")


async def _drain(session, prompt=None):
    """One turn: connect/query then collect the normalized stream."""
    await session.connect(prompt)
    return [m async for m in session.receive_response()]


def test_registered_but_gated(monkeypatch):
    # Resolvable via the registry (both test surfaces share this brain)...
    monkeypatch.delenv(STUB_BRAIN_ENV, raising=False)
    monkeypatch.setenv("BOBI_BRAIN", "stub")
    brain = get_brain()
    assert isinstance(brain, StubBrain)
    # ...but inert without the explicit acknowledgement.
    with pytest.raises(RuntimeError, match="test-only"):
        brain.make_session(cwd=None, system_prompt=None)


def test_default_turn_completes_idle(stub_enabled):
    brain = get_brain("stub")
    session = brain.make_session(cwd=None, system_prompt=None)
    msgs = asyncio.run(_drain(session, "boot up"))

    # AssistantText(s) then exactly one terminal TurnResult, then the iterator
    # stops - the shape the turn loop needs to flip a manager to idle.
    assert isinstance(msgs[-1], TurnResult)
    assert msgs[-1].is_error is False
    assert sum(isinstance(m, TurnResult) for m in msgs) == 1
    assert any(isinstance(m, AssistantText) for m in msgs)


def test_reply_directive_sets_assistant_text(stub_enabled):
    session = get_brain("stub").make_session(cwd=None, system_prompt=None)
    msgs = asyncio.run(_drain(session, "please __stub__:reply:pong now"))
    texts = [m.text for m in msgs if isinstance(m, AssistantText)]
    assert texts == ["pong"]
    assert msgs[-1].result_text == "pong"


def test_options_directive_echoes_session_options(stub_enabled):
    """`__stub__:options` replies with the scalar options make_session got -
    the observability seam e2e tests use to prove a launch flag (model,
    effort) actually reached the session (#778)."""
    import json

    session = get_brain("stub").make_session(
        cwd=None, system_prompt=None,
        options={"model": "stub-m", "effort": "xhigh", "max_turns": 200,
                 "hooks": object()},
    )
    msgs = asyncio.run(_drain(session, "__stub__:options"))
    texts = [m.text for m in msgs if isinstance(m, AssistantText)]
    echoed = json.loads(texts[0])
    assert echoed == {"model": "stub-m", "effort": "xhigh", "max_turns": 200}


def test_error_directive_flags_turn(stub_enabled):
    session = get_brain("stub").make_session(cwd=None, system_prompt=None)
    msgs = asyncio.run(_drain(session, "__stub__:error"))
    assert isinstance(msgs[-1], TurnResult)
    assert msgs[-1].is_error is True


def test_hang_directive_stalls_until_cancelled(stub_enabled):
    session = get_brain("stub").make_session(cwd=None, system_prompt=None)

    async def run():
        await session.connect("__stub__:hang:5")
        agen = session.receive_response()
        # The turn must NOT complete promptly - a hang keeps the manager in-turn
        # (running -> wedged). 0.3s is ample to prove it did not yield a result.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agen.__anext__(), timeout=0.3)
        await agen.aclose()

    asyncio.run(run())


def test_resume_token_threads_through_session_id(stub_enabled):
    session = get_brain("stub").make_session(
        cwd=None, system_prompt=None, resume="carried-id")
    msgs = asyncio.run(_drain(session, "hello"))
    assert msgs[-1].session_id == "carried-id"


def test_exit_directive_terminates_process(stub_enabled):
    # __stub__:exit hard-exits the process (supervisor crash path). Prove it in
    # a child so the test runner survives.
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import asyncio, os
        os.environ["BOBI_STUB_BRAIN"] = "1"
        from bobi.brain import get_brain
        async def go():
            s = get_brain("stub").make_session(cwd=None, system_prompt=None)
            await s.connect("__stub__:exit:7")
            async for _ in s.receive_response():
                pass
        asyncio.run(go())
        """
    )
    rc = subprocess.run([sys.executable, "-c", code]).returncode
    assert rc == 7
