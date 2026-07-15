"""Unit tests for the Codex brain adapter (epic #485, Phase 2 MVP).

The ``codex exec`` subprocess is replaced with a fake runner that replays a
scripted NDJSON event stream, so the conversion to normalized brain messages,
the resume/thread handling, usage mapping, and error paths are exercised without
a real codex binary.
"""

import sys

import pytest

from bobi.brain import get_brain
from bobi.brain.base import AssistantText, TurnResult
from bobi.brain.codex import (
    CodexBrain,
    _CodexSession,
    _instructions,
    _spawn_codex,
)


MAX_ARG_STRLEN = 128 * 1024


def _runner_of(events, sink=None):
    """A fake codex runner: records (argv, cwd, stdin) and replays `events`."""
    async def _run(argv, cwd, stdin_text=None):
        if sink is not None:
            sink.append((argv, cwd, stdin_text))
        for ev in events:
            yield ev
    return _run


async def _drain(session):
    return [m async for m in session.receive_response()]


# --- registry / factory -----------------------------------------------------

def test_codex_registered():
    b = get_brain("codex")
    assert isinstance(b, CodexBrain)
    assert b.provider == "openai"


def test_instructions_extraction():
    assert _instructions({"preset": "x", "append": "be good"}) == "be good"
    assert _instructions("raw text") == "raw text"
    assert _instructions(None) == ""


# --- happy turn -------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn_converts_messages_and_captures_thread():
    events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"id": "i0", "type": "agent_message", "text": "working"}},
        {"type": "item.completed",
         "item": {"id": "i1", "type": "file_change", "changes": []}},
        {"type": "item.completed",
         "item": {"id": "i2", "type": "agent_message", "text": "done."}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 1002, "cached_input_tokens": 1000,
                   "output_tokens": 9}},
    ]
    s = _CodexSession(cwd="/tmp/x", instructions="SYS", runner=_runner_of(events))
    await s.connect("hello")
    out = await _drain(s)

    texts = [m.text for m in out if isinstance(m, AssistantText) and m.text]
    assert texts == ["working", "done."]          # file_change is dropped
    # Codex usage must NOT reach the rotation metric (no usage-carrying message):
    # its per-turn aggregate storms rotation. Cost still rides the TurnResult. #485
    assert not any(isinstance(m, AssistantText) and m.usage for m in out)
    result = out[-1]
    assert isinstance(result, TurnResult)
    assert result.session_id == "th-1"
    assert result.is_error is False
    # codex reports input_tokens INCLUSIVE of cached_input_tokens (its
    # non_cached_input() is input - cached); record both as-is - the old
    # sum double-counted every cache read. #760
    assert result.costs[0].input_tokens == 1002
    assert result.costs[0].cached_input_tokens == 1000
    assert s._thread_id == "th-1"


@pytest.mark.asyncio
async def test_fresh_turn_prepends_instructions_then_resume_does_not():
    sink = []
    events = [
        {"type": "thread.started", "thread_id": "th-9"},
        {"type": "turn.completed", "usage": {}},
    ]
    s = _CodexSession(cwd="/w", instructions="SYSTEM", runner=_runner_of(events, sink))
    await s.connect("first")
    await _drain(s)
    # Fresh thread: instructions prepended, plain `codex exec` (no resume).
    fresh_argv = sink[0][0]
    assert fresh_argv[:2] == ["codex", "exec"]
    assert "resume" not in fresh_argv
    assert fresh_argv[-1] == "-"
    assert sink[0][1] == "/w"
    assert sink[0][2] == "SYSTEM\n\nfirst"

    # Next turn resumes the captured thread and does NOT re-send instructions.
    await s.query("second")
    await _drain(s)
    resume_argv = sink[1][0]
    assert resume_argv[:4] == ["codex", "exec", "resume", "th-9"]
    assert resume_argv[-1] == "-"
    assert sink[1][2] == "second"


@pytest.mark.asyncio
async def test_large_fresh_prompt_uses_stdin_not_argv():
    sink = []
    events = [{"type": "turn.completed", "usage": {}}]
    prompt = "x" * (MAX_ARG_STRLEN + 1)
    s = _CodexSession(cwd="/w", instructions="SYSTEM", runner=_runner_of(events, sink))

    await s.connect(prompt)
    await _drain(s)

    argv, _cwd, stdin_text = sink[0]
    assert argv[-1] == "-"
    assert all(prompt not in arg for arg in argv)
    assert stdin_text == "SYSTEM\n\n" + prompt
    assert len(stdin_text) > MAX_ARG_STRLEN


@pytest.mark.asyncio
async def test_large_resume_prompt_uses_stdin_not_argv():
    sink = []
    events = [{"type": "turn.completed", "usage": {}}]
    prompt = "x" * (MAX_ARG_STRLEN + 1)
    s = _CodexSession(
        cwd="/w", instructions="SYSTEM", resume="th-big",
        runner=_runner_of(events, sink),
    )

    await s.query(prompt)
    await _drain(s)

    argv, _cwd, stdin_text = sink[0]
    assert argv[:4] == ["codex", "exec", "resume", "th-big"]
    assert argv[-1] == "-"
    assert all(prompt not in arg for arg in argv)
    assert stdin_text == prompt
    assert len(stdin_text) > MAX_ARG_STRLEN


@pytest.mark.asyncio
async def test_no_pending_input_yields_noop_result():
    """A reconnect-style drain with nothing queued must still yield a result."""
    s = _CodexSession(cwd="/w", instructions="", resume="th-keep",
                      runner=_runner_of([]))
    out = await _drain(s)
    assert len(out) == 1
    assert isinstance(out[0], TurnResult)
    assert out[0].session_id == "th-keep"
    assert out[0].is_error is False


@pytest.mark.asyncio
async def test_turn_failed_surfaces_error():
    events = [
        {"type": "thread.started", "thread_id": "th-e"},
        {"type": "turn.failed", "error": {"message": "model overloaded"}},
    ]
    s = _CodexSession(cwd="/w", instructions="", runner=_runner_of(events))
    await s.connect("go")
    out = await _drain(s)
    assert isinstance(out[-1], TurnResult)
    assert out[-1].is_error is True
    assert out[-1].result_text == "model overloaded"


@pytest.mark.asyncio
async def test_spawn_codex_accepts_large_ndjson_events(tmp_path):
    """Codex can emit one JSON event line larger than asyncio's 64 KiB default."""
    script = (
        "import json\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'x' * 70000}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)\n"
    )

    events = [ev async for ev in _spawn_codex([sys.executable, "-c", script], str(tmp_path))]

    assert events[0]["type"] == "item.completed"
    assert events[0]["item"]["text"] == "x" * 70000
    assert events[1]["type"] == "turn.completed"


@pytest.mark.asyncio
async def test_spawn_codex_writes_and_closes_large_stdin(tmp_path):
    script = (
        "import json, sys\n"
        "prompt = sys.stdin.read()\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': str(len(prompt))}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)\n"
    )
    prompt = "x" * (MAX_ARG_STRLEN + 1)

    events = [
        ev async for ev in _spawn_codex(
            [sys.executable, "-c", script, "-"], str(tmp_path), prompt,
        )
    ]

    assert events[0]["type"] == "item.completed"
    assert events[0]["item"]["text"] == str(len(prompt))
    assert events[1]["type"] == "turn.completed"


@pytest.mark.asyncio
async def test_spawn_codex_raises_on_nonzero_exit_with_stderr(tmp_path):
    script = "import sys\nprint('tool exploded', file=sys.stderr)\nsys.exit(7)\n"

    with pytest.raises(RuntimeError, match="codex subprocess exited 7: tool exploded"):
        [ev async for ev in _spawn_codex([sys.executable, "-c", script], str(tmp_path))]


@pytest.mark.asyncio
async def test_spawn_codex_close_after_terminal_event_does_not_raise(tmp_path):
    script = (
        "import json, time\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)\n"
        "time.sleep(10)\n"
    )

    stream = _spawn_codex([sys.executable, "-c", script], str(tmp_path))
    ev = await stream.__anext__()
    assert ev["type"] == "turn.completed"
    await stream.aclose()


@pytest.mark.asyncio
async def test_spawn_codex_close_kills_sigterm_resistant_child(tmp_path):
    script = (
        "import json, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)\n"
        "time.sleep(10)\n"
    )

    stream = _spawn_codex([sys.executable, "-c", script], str(tmp_path))
    ev = await stream.__anext__()
    assert ev["type"] == "turn.completed"
    await stream.aclose()


@pytest.mark.asyncio
async def test_stream_ends_without_terminal_is_error():
    events = [{"type": "thread.started", "thread_id": "th-x"}]  # no turn.completed
    s = _CodexSession(cwd="/w", instructions="", runner=_runner_of(events))
    await s.connect("go")
    out = await _drain(s)
    assert out[-1].is_error is True
    assert "without completing" in out[-1].result_text


@pytest.mark.asyncio
async def test_env_model_default_adds_flag(monkeypatch):
    sink = []
    events = [{"type": "turn.completed", "usage": {}}]
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "gpt-5-codex")

    s = CodexBrain().make_session(cwd="/w", system_prompt={"append": "S"})
    s._runner = _runner_of(events, sink)
    await s.connect("hi")
    await _drain(s)

    assert "-m" in sink[0][0] and "gpt-5-codex" in sink[0][0]

@pytest.mark.asyncio
async def test_model_override_adds_flag(monkeypatch):
    sink = []
    events = [{"type": "turn.completed", "usage": {}}]
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "gpt-5-codex")
    b = CodexBrain()
    s = b.make_session(cwd="/w", system_prompt={"append": "S"},
                       options={"model": "o3"})
    s._runner = _runner_of(events, sink)
    await s.connect("hi")
    await _drain(s)
    assert "-m" in sink[0][0] and "o3" in sink[0][0]
    assert "gpt-5-codex" not in sink[0][0]


@pytest.mark.asyncio
async def test_env_effort_default_adds_config_flag(monkeypatch):
    sink = []
    events = [{"type": "turn.completed", "usage": {}}]
    monkeypatch.setenv("BOBI_BRAIN_EFFORT", "high")

    s = CodexBrain().make_session(cwd="/w", system_prompt={"append": "S"})
    s._runner = _runner_of(events, sink)
    await s.connect("hi")
    await _drain(s)

    argv = sink[0][0]
    assert "-c" in argv and "model_reasoning_effort=high" in argv


@pytest.mark.asyncio
async def test_effort_override_adds_config_flag(monkeypatch):
    sink = []
    events = [{"type": "turn.completed", "usage": {}}]
    monkeypatch.setenv("BOBI_BRAIN_EFFORT", "medium")
    s = CodexBrain().make_session(cwd="/w", system_prompt={"append": "S"},
                                  options={"effort": "xhigh"})
    s._runner = _runner_of(events, sink)
    await s.connect("hi")
    await _drain(s)
    argv = sink[0][0]
    assert "model_reasoning_effort=xhigh" in argv
    assert "model_reasoning_effort=medium" not in argv


@pytest.mark.asyncio
async def test_no_effort_omits_config_flag(monkeypatch):
    sink = []
    events = [{"type": "turn.completed", "usage": {}}]
    monkeypatch.delenv("BOBI_BRAIN_EFFORT", raising=False)
    s = CodexBrain().make_session(cwd="/w", system_prompt={"append": "S"})
    s._runner = _runner_of(events, sink)
    await s.connect("hi")
    await _drain(s)
    assert not any(
        str(a).startswith("model_reasoning_effort=") for a in sink[0][0]
    )


# --- MCP config rendering (#428 Stage 4) ------------------------------------


def test_make_session_renders_mcp_config_toml(tmp_path, monkeypatch):
    """A codex session with `mcp_servers` in options renders ~/.codex/config.toml
    (Codex reads MCP from disk, not the CLI). Uses CODEX_HOME to redirect."""
    import tomllib

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    CodexBrain().make_session(
        cwd="/w", system_prompt={"append": "S"},
        options={"mcp_servers": {
            "weather": {"type": "stdio", "command": "/opt/weather"},
        }},
    )
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["mcp_servers"]["weather"]["command"] == "/opt/weather"


def test_make_session_without_mcp_writes_nothing(tmp_path, monkeypatch):
    """No `mcp_servers` in options → no config.toml touched (no MCP need)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    CodexBrain().make_session(cwd="/w", system_prompt={"append": "S"}, options={})
    assert not (tmp_path / "config.toml").exists()


def test_make_session_render_failure_propagates(tmp_path, monkeypatch):
    """A config-render error surfaces rather than silently degrading: a codex team
    that declares MCP but can't render config.toml would otherwise pass preflight
    (the probe checks the in-memory spec) and then run MCP-less at runtime."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(
        "bobi.brain.codex_config.write_codex_config",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        CodexBrain().make_session(
            cwd="/w", system_prompt={"append": "S"},
            options={"mcp_servers": {"x": {"command": "/x"}}})


def test_make_session_clears_stale_managed_block(tmp_path, monkeypatch):
    """A codex team that dropped its MCP deps clears a previously-rendered block
    (no options.mcp_servers, but a bobi-managed block on disk)."""
    import tomllib
    from bobi.brain import codex_config

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # Prior render leaves a managed block on the durable config.
    codex_config.write_codex_config({"old": {"command": "/old"}}, home=tmp_path)
    assert codex_config.MANAGED_BEGIN in (tmp_path / "config.toml").read_text()

    CodexBrain().make_session(cwd="/w", system_prompt={"append": "S"}, options={})
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert "mcp_servers" not in data
