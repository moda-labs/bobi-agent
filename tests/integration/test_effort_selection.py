"""Live tests for reasoning-effort selection (#778).

Proves the effort dial actually reaches the real vendor CLIs - the part a
mocked brain cannot: the claude CLI is spawned carrying ``--effort``, the
codex runner receives the effort config override, and an effort-only workflow
step change keeps the conversation (the resume-guard exemption) end to end.

The Claude tests require the ``claude`` CLI; the Codex test requires the
``codex`` CLI (and burns one real turn). All skipped in CI.
"""

import json
import os
import subprocess
import time

import pytest
import yaml

from bobi.sdk import SessionRegistry, _sessions_dir
from .conftest import _drain, requires_claude, requires_codex

# No module-level claude mark: the codex leg below must not be deselected by
# a `-m "not claude"` run - it needs the codex CLI, not the claude one.
claude_marked = pytest.mark.claude


def _child_process_args() -> list[str]:
    """argv strings of this process's direct children (the SDK-spawned CLI)."""
    out = subprocess.run(
        ["ps", "-axo", "ppid=,args="], capture_output=True, text=True,
    ).stdout
    me = str(os.getpid())
    args = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] == me:
            args.append(parts[1])
    return args


@claude_marked
@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(180)
class TestClaudeEffortSelection:
    """The effort option reaches the live claude CLI process."""

    async def test_effort_flag_reaches_spawned_cli(self):
        from bobi.brain import get_brain

        brain = get_brain("claude")
        session = brain.make_session(
            cwd="/tmp",
            system_prompt="You are a test assistant. Reply concisely.",
            options={"effort": "low", "max_turns": 3},
        )
        try:
            await session.connect("Reply with exactly: OK")
            # The persistent CLI subprocess is alive now - the ground truth
            # for "the dial reached the real session" is its own argv (the
            # claude CLI warns-and-ignores bad values and records effort
            # nowhere else observable, so argv IS the wire).
            children = _child_process_args()
            assert any(
                "--effort" in argv and "low" in argv for argv in children
            ), f"no child claude process carries --effort low: {children}"
            _, result = await _drain(session)
        finally:
            await session.disconnect()
        assert result is not None and not result.is_error


@claude_marked
@requires_claude
@pytest.mark.timeout(300)
class TestWorkflowEffortContinuation:
    """An effort-only step change keeps conversation state end to end (#778):
    effort is exempt from the resume guard, so the session must reconnect
    natively - the code word below survives ONLY via the live transcript."""

    def test_effort_only_switch_keeps_conversation(self, bobi_env, clean_session):
        from bobi.workflow.orchestrator import make_session_name, run_workflow
        from bobi.workflow.schema import HandoffContract, StepDef, Workflow

        session_name = make_session_name("xeffort", "test-repo", "302")
        clean_session(session_name)

        wf = Workflow(name="xeffort", steps=[
            StepDef(
                name="seed", prompt=(
                    "The code word is PANGOLIN. Do not write it to any file "
                    "or repeat it yet. Write your handoff file with exactly "
                    "one field: ack: yes"
                ),
                effort="low", timeout=90,
                handoff=HandoffContract(required=["ack"]),
            ),
            StepDef(
                name="recall", prompt=(
                    "State the code word from earlier in this conversation. "
                    "Write your handoff file with exactly one field: "
                    "word: <the code word>"
                ),
                effort="medium", timeout=90,
                handoff=HandoffContract(required=["word"]),
            ),
        ])

        result = run_workflow(
            wf, task="Effort continuation test #302", repo="test-repo",
            cwd=str(bobi_env.project_path), run_key="302",
            timeout=240, interactive=False,
        )

        assert result is True
        handoff = yaml.safe_load(
            SessionRegistry().handoff_path(session_name, "recall").read_text()
        )
        assert "PANGOLIN" in str(handoff.get("word", "")).upper(), handoff


@requires_codex
@pytest.mark.asyncio
@pytest.mark.timeout(240)
class TestCodexEffortSelection:
    """``-c model_reasoning_effort=...`` lands in the real codex turn."""

    async def test_effort_reaches_spawned_cli(self, tmp_path):
        from bobi.brain import get_brain
        from bobi.brain.codex import _spawn_codex

        spawned_argv = []

        def recording_runner(argv, cwd, stdin_text=None):
            spawned_argv.append(argv)
            return _spawn_codex(argv, cwd, stdin_text)

        brain = get_brain("codex")
        session = brain.make_session(
            cwd=str(tmp_path),
            system_prompt="You are a test assistant. Reply concisely.",
            options={"effort": "low"},
        )
        session._runner = recording_runner
        try:
            await session.connect("Reply with exactly: OK")
            _, result = await _drain(session)
        finally:
            await session.disconnect()

        assert result is not None and not result.is_error
        assert result.session_id
        assert any(
            argv[index:index + 2]
            == ["-c", "model_reasoning_effort=low"]
            for argv in spawned_argv
            for index in range(len(argv) - 1)
        ), spawned_argv


@pytest.mark.timeout(120)
class TestCliEffortSeam:
    """The full CLI seam on the stub brain, so it runs in CI: --effort on
    ``subagents launch`` -> detached subprocess args blob -> workflow
    orchestrator -> brain session options. The ``__stub__:options`` directive
    makes the stub reply with the options ``make_session`` received, and the
    session log records that reply."""

    def test_launch_flags_reach_detached_session_options(
            self, stub_bobi_env, stub_cli_run):
        # No clean_session here: it drags in the claude-env fixture, and this
        # module's stub home is fresh (module-scoped), so 303 cannot pre-exist.
        session_name = "wf-adhoc-test-repo-303"

        result = stub_cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--role", "engineer", "--id", "303",
            "--task", "__stub__:options",
            "--model", "stub-model-x", "--effort", "xhigh",
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        session_dir = _sessions_dir() / session_name
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state_path = session_dir / "state.json"
            if state_path.exists():
                if json.loads(state_path.read_text()).get("status") == "completed":
                    break
            time.sleep(2)
        else:
            pytest.fail("detached stub agent did not complete within 90s")

        # The stub's options-echo reply is logged as a `response` event.
        echoed = None
        for line in (session_dir / "log.jsonl").read_text().splitlines():
            entry = json.loads(line)
            if entry.get("event") == "response" and "effort" in entry.get("text", ""):
                echoed = json.loads(entry["text"])
        assert echoed is not None, "no options echo found in the session log"
        assert echoed["effort"] == "xhigh"
        assert echoed["model"] == "stub-model-x"

    def test_as_check_rejects_effort_flag(self, stub_bobi_env, stub_cli_run):
        result = stub_cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--role", "monitor", "--as-check",
            "--task", "check", "--effort", "low",
            timeout=10,
        )
        assert result.returncode != 0
        assert "--as-check" in result.stderr
