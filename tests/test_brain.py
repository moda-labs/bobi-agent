"""Unit tests for the pluggable brain layer (epic #485, Phase 1).

Covers the brain registry/selector and the Claude adapter's behavior-preserving
normalization: SDK ``AssistantMessage``/``ResultMessage`` → normalized
``AssistantText``/``TurnResult``, including the model-usage → cost breakdown and
deferred-tool translation the call sites rely on.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from bobi.brain import (
    AssistantText,
    BrainSession,
    ClaudeBrain,
    DEFAULT_BRAIN,
    TurnResult,
    get_brain,
)
from bobi.brain.claude import _ClaudeSession, _result_to_turn


@pytest.fixture(autouse=True)
def default_brain_env(monkeypatch):
    monkeypatch.delenv("BOBI_BRAIN", raising=False)
    monkeypatch.delenv("BOBI_BRAIN_MODEL", raising=False)


# --- registry / selector ---------------------------------------------------


def test_default_brain_is_claude():
    assert DEFAULT_BRAIN == "claude"
    assert get_brain().name == "claude"
    assert get_brain(None).provider == "anthropic"


def test_explicit_claude_kind():
    assert isinstance(get_brain("claude"), ClaudeBrain)


def test_unknown_brain_kind_fails_loud():
    with pytest.raises(ValueError, match="unknown brain kind"):
        get_brain("gpt-9")


def test_get_brain_resolves_from_env(monkeypatch):
    """An explicit kind wins; otherwise BOBI_BRAIN; otherwise the default."""
    from bobi.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "claude")
    assert get_brain().name == "claude"          # env supplies it
    assert get_brain("claude").name == "claude"  # explicit arg also fine
    monkeypatch.delenv(BRAIN_ENV, raising=False)
    assert get_brain().name == "claude"          # falls back to DEFAULT_BRAIN


def test_set_process_brain():
    from bobi.brain import (
        BRAIN_ENV,
        get_process_brain_model,
        set_process_brain,
    )

    model_env = "BOBI_BRAIN_MODEL"

    # set_process_brain mutates os.environ directly (so it propagates to child
    # processes), so monkeypatch can't track it — save/restore explicitly.
    saved = os.environ.pop(BRAIN_ENV, None)
    saved_model = os.environ.pop(model_env, None)
    try:
        set_process_brain("")          # empty → no-op (keep framework default)
        assert BRAIN_ENV not in os.environ
        assert get_process_brain_model() == ""
        set_process_brain("", "sonnet")  # model-only config tunes default Claude
        assert BRAIN_ENV not in os.environ
        assert get_process_brain_model() == "sonnet"
        os.environ.pop(model_env)
        set_process_brain("codex", "gpt-5-codex")     # sets it
        assert os.environ[BRAIN_ENV] == "codex"
        assert get_process_brain_model() == "gpt-5-codex"
        set_process_brain("claude", "opus")  # already-set env is NOT overridden
        assert os.environ[BRAIN_ENV] == "codex"
        assert get_process_brain_model() == "gpt-5-codex"
        os.environ.pop(BRAIN_ENV)
        os.environ.pop(model_env)
        os.environ[BRAIN_ENV] = "claude"
        set_process_brain("codex", "gpt-5-codex")  # operator brain override wins
        assert os.environ[BRAIN_ENV] == "claude"
        assert get_process_brain_model() == ""
        os.environ.pop(BRAIN_ENV)
        os.environ[BRAIN_ENV] = "codex"
        set_process_brain("", "sonnet")  # model-only default does not cross brains
        assert os.environ[BRAIN_ENV] == "codex"
        assert get_process_brain_model() == ""
    finally:
        if saved is None:
            os.environ.pop(BRAIN_ENV, None)
        else:
            os.environ[BRAIN_ENV] = saved
        if saved_model is None:
            os.environ.pop(model_env, None)
        else:
            os.environ[model_env] = saved_model


def test_config_parses_brain(tmp_path):
    """agent.yaml `brain:` round-trips into Config + the brain_kind helper."""
    from bobi.config import Config
    from bobi import paths

    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: t\nbrain:\n  kind: codex\n  model: gpt-5-codex\n"
    )
    cfg = Config.load(tmp_path)
    assert cfg.brain == {"kind": "codex", "model": "gpt-5-codex"}
    assert cfg.brain_kind == "codex"
    assert cfg.brain_model == "gpt-5-codex"
    # Absent brain → empty + the framework default downstream.
    paths.agent_yaml_path(tmp_path).write_text("agent: t\n")
    assert Config.load(tmp_path).brain_kind == ""
    assert Config.load(tmp_path).brain_model == ""


def test_config_parses_roles(tmp_path):
    """agent.yaml `roles:` round-trips into Config + the role_model helper."""
    from bobi.config import Config
    from bobi import paths

    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: t\nroles:\n  monitor:\n    model: haiku\n  planner: {}\n"
    )
    cfg = Config.load(tmp_path)
    assert cfg.role_model("monitor") == "haiku"
    assert cfg.role_model("planner") == ""      # role entry without a model
    assert cfg.role_model("engineer") == ""     # unknown role
    # Absent roles → empty mapping, everything falls through.
    paths.agent_yaml_path(tmp_path).write_text("agent: t\n")
    assert Config.load(tmp_path).role_model("monitor") == ""


def test_resolve_model_precedence(monkeypatch):
    """explicit > roles.<role>.model > process default > "" (#617)."""
    from bobi.brain import resolve_model
    from bobi.config import Config

    cfg = Config(roles={"monitor": {"model": "haiku"}})

    assert resolve_model(cfg, role="monitor", explicit="opus") == "opus"
    assert resolve_model(cfg, role="monitor") == "haiku"
    assert resolve_model(cfg, role="engineer") == ""   # unconfigured → unchanged
    assert resolve_model(None, role="monitor") == ""

    monkeypatch.setenv("BOBI_BRAIN_MODEL", "sonnet")
    assert resolve_model(cfg, role="monitor") == "haiku"    # role beats team default
    assert resolve_model(cfg, role="engineer") == "sonnet"  # falls to team default
    assert resolve_model(None) == "sonnet"


def test_claude_brain_uses_env_model_default(monkeypatch):
    captured = {}

    def _options(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setenv("BOBI_BRAIN_MODEL", "haiku")
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        ClaudeBrain().make_session(cwd="/tmp", system_prompt=None)

    assert captured["model"] == "haiku"


def test_claude_brain_explicit_model_overrides_env(monkeypatch):
    captured = {}

    def _options(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setenv("BOBI_BRAIN_MODEL", "haiku")
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        ClaudeBrain().make_session(
            cwd="/tmp", system_prompt=None,
            options={"model": "sonnet"},
        )

    assert captured["model"] == "sonnet"


# --- ResultMessage → TurnResult normalization ------------------------------


def _result(**kw):
    base = dict(
        subtype="success",
        duration_ms=12,
        duration_api_ms=6,
        is_error=False,
        num_turns=2,
        session_id="sess-xyz",
        total_cost_usd=0.0,
        usage={},
    )
    base.update(kw)
    return ResultMessage(**base)


def test_result_to_turn_carries_core_fields():
    msg = _result(total_cost_usd=0.5, result="ok")
    turn = _result_to_turn(msg)
    assert isinstance(turn, TurnResult)
    assert turn.session_id == "sess-xyz"
    assert turn.is_error is False
    assert turn.total_cost_usd == 0.5
    assert turn.duration_ms == 12
    assert turn.num_turns == 2
    assert turn.result_text == "ok"
    assert turn.deferred_tool is None


def test_result_to_turn_normalizes_model_usage_list_of_objects():
    """The breakdown is read per-element via getattr (model/input/output) — the
    shape ``session.py`` has always consumed. A list of usage objects populates
    the BrainCost list."""

    class _MU:
        def __init__(self, model, i, o):
            self.model, self.input_tokens, self.output_tokens = model, i, o

    msg = _result(model_usage=[_MU("claude-opus-4-8", 10, 3), _MU("haiku", 1, 1)])
    turn = _result_to_turn(msg)
    assert [(c.model, c.input_tokens, c.output_tokens) for c in turn.costs] == [
        ("claude-opus-4-8", 10, 3),
        ("haiku", 1, 1),
    ]


def test_result_to_turn_dict_model_usage_preserves_legacy_noop():
    """Behavior-preservation guard (#485 Phase 1): the SDK actually types
    ``model_usage`` as ``dict[str, Any]``, but the legacy code (and this faithful
    adapter) iterate it as a list-of-objects, so a dict is wrapped as one element
    whose getattr lookups miss → an empty/zero BrainCost. This is a pre-existing
    latent bug, deliberately preserved here; fixing the dict shape is a tracked
    follow-up, not a Phase-1 behavior change."""
    msg = _result(model_usage={"claude-opus-4-8": {"input_tokens": 10, "output_tokens": 3}})
    turn = _result_to_turn(msg)
    assert len(turn.costs) == 1
    assert turn.costs[0].model == ""
    assert turn.costs[0].input_tokens == 0
    assert turn.costs[0].output_tokens == 0


def test_result_to_turn_handles_error_and_status():
    msg = _result(is_error=True, result="API Error: 529 Overloaded")
    msg.api_error_status = 529
    turn = _result_to_turn(msg)
    assert turn.is_error is True
    assert turn.api_error_status == 529
    assert turn.result_text == "API Error: 529 Overloaded"


def test_result_to_turn_translates_deferred_tool():
    msg = _result()

    class _Deferred:
        name = "AskUserQuestion"
        input = {"q": "which?"}

    msg.deferred_tool_use = _Deferred()
    turn = _result_to_turn(msg)
    assert turn.deferred_tool is not None
    assert turn.deferred_tool.name == "AskUserQuestion"
    assert turn.deferred_tool.input == {"q": "which?"}


# --- session-level message stream conversion -------------------------------


class _FakeSDKClient:
    """Stands in for ClaudeSDKClient: replays SDK messages for one turn."""

    def __init__(self, messages):
        self._messages = messages

    async def receive_response(self):
        for m in self._messages:
            yield m


def _claude_session_over(messages):
    """A _ClaudeSession whose underlying SDK client is swapped for a fake."""
    sess = _ClaudeSession.__new__(_ClaudeSession)
    sess._client = _FakeSDKClient(messages)
    return sess


@pytest.mark.asyncio
async def test_receive_response_converts_assistant_and_result():
    assistant = AssistantMessage(
        content=[TextBlock(text="hello"), TextBlock(text="world")],
        model="claude-opus-4-8",
        usage={"input_tokens": 5, "cache_read_input_tokens": 100},
    )
    out = []
    async for m in _claude_session_over([assistant, _result(total_cost_usd=0.1)]).receive_response():
        out.append(m)

    assert isinstance(out[0], AssistantText)
    assert out[0].text == "hello\nworld"
    assert out[0].usage == {"input_tokens": 5, "cache_read_input_tokens": 100}
    assert isinstance(out[1], TurnResult)
    assert out[1].total_cost_usd == 0.1


@pytest.mark.asyncio
async def test_assistant_without_text_still_carries_usage():
    """An assistant message with no TextBlocks yields empty text but keeps usage
    (the rotation metric reads usage even on a text-less step)."""
    assistant = AssistantMessage(content=[], model="m", usage={"input_tokens": 7})
    out = [m async for m in _claude_session_over([assistant]).receive_response()]
    assert out[0].text == ""
    assert out[0].usage == {"input_tokens": 7}


def test_claude_session_satisfies_brain_session_protocol():
    sess = _claude_session_over([])
    assert isinstance(sess, BrainSession)
    assert sess.provider == "anthropic"


@pytest.mark.asyncio
async def test_claude_connect_retries_initialize_timeout(monkeypatch):
    """Startup initialize timeouts are transient under CPU/IO contention."""
    clients = []

    class _ConnectClient:
        def __init__(self, options):
            self.options = options
            self.disconnected = False
            clients.append(self)

        async def connect(self):
            if len(clients) == 1:
                raise Exception("Control request timeout: initialize")

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", _ConnectClient)
    monkeypatch.setenv("BOBI_CLAUDE_CONNECT_ATTEMPTS", "2")
    monkeypatch.setenv("BOBI_CLAUDE_CONNECT_BACKOFF_SECONDS", "0")

    sess = _ClaudeSession(options=object())
    await sess.connect()

    assert len(clients) == 2
    assert clients[0].disconnected is True
    assert sess._client is clients[1]


@pytest.mark.asyncio
async def test_claude_connect_does_not_retry_non_initialize_error(monkeypatch):
    clients = []

    class _ConnectClient:
        def __init__(self, options):
            clients.append(self)

        async def connect(self):
            raise RuntimeError("auth failed")

        async def disconnect(self):
            pass

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", _ConnectClient)
    monkeypatch.setenv("BOBI_CLAUDE_CONNECT_ATTEMPTS", "3")
    monkeypatch.setenv("BOBI_CLAUDE_CONNECT_BACKOFF_SECONDS", "0")

    sess = _ClaudeSession(options=object())
    with pytest.raises(RuntimeError, match="auth failed"):
        await sess.connect()

    assert len(clients) == 1


@pytest.mark.asyncio
async def test_claude_connect_sets_default_initialize_timeout(monkeypatch):
    class _ConnectClient:
        def __init__(self, options):
            pass

        async def connect(self):
            pass

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", _ConnectClient)
    monkeypatch.delenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOBI_CLAUDE_INITIALIZE_TIMEOUT_MS", raising=False)

    sess = _ClaudeSession(options=object())
    await sess.connect()

    assert os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] == "180000"


@pytest.mark.asyncio
async def test_claude_connect_preserves_explicit_initialize_timeout(monkeypatch):
    class _ConnectClient:
        def __init__(self, options):
            pass

        async def connect(self):
            pass

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", _ConnectClient)
    monkeypatch.setenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "240000")
    monkeypatch.setenv("BOBI_CLAUDE_INITIALIZE_TIMEOUT_MS", "180000")

    sess = _ClaudeSession(options=object())
    await sess.connect()

    assert os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] == "240000"


@pytest.mark.asyncio
async def test_claude_stream_once_sets_default_initialize_timeout(monkeypatch):
    async def _query(*, prompt, options):
        assert os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] == "180000"
        yield _result(result="ok")

    monkeypatch.setattr("claude_agent_sdk.query", _query)
    monkeypatch.setattr("bobi.sdk.get_cli_path", lambda: "/usr/bin/claude")
    monkeypatch.delenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOBI_CLAUDE_INITIALIZE_TIMEOUT_MS", raising=False)

    out = []
    async for msg in ClaudeBrain().stream_once(
        system_prompt="sys",
        user_prompt="hello",
        cwd="/tmp",
    ):
        out.append(msg)

    assert isinstance(out[-1], TurnResult)


@pytest.mark.asyncio
async def test_claude_stream_once_retries_initialize_timeout_before_output(
    monkeypatch,
):
    calls = 0

    async def _query(*, prompt, options):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Exception("Control request timeout: initialize")
        yield _result(result="ok")

    monkeypatch.setattr("claude_agent_sdk.query", _query)
    monkeypatch.setattr("bobi.sdk.get_cli_path", lambda: "/usr/bin/claude")
    monkeypatch.setenv("BOBI_CLAUDE_CONNECT_ATTEMPTS", "2")
    monkeypatch.setenv("BOBI_CLAUDE_CONNECT_BACKOFF_SECONDS", "0")

    out = []
    async for msg in ClaudeBrain().stream_once(
        system_prompt="sys",
        user_prompt="hello",
        cwd="/tmp",
    ):
        out.append(msg)

    assert calls == 2
    assert isinstance(out[-1], TurnResult)


# --- capabilities + continuation (#642) --------------------------------------


def test_claude_supports_cross_model_resume():
    from bobi.brain import CodexBrain
    assert ClaudeBrain.capabilities.cross_model_resume is True
    # Unverified on Codex: `codex exec resume -m` behavior is unproven.
    assert CodexBrain.capabilities.cross_model_resume is False


@pytest.mark.parametrize("session_id,frm,to,capable,expected", [
    # same model always continues, capability irrelevant
    ("sid", "haiku", "haiku", False, "sid"),
    ("sid", "", "", False, "sid"),
    # cross-model requires the capability
    ("sid", "haiku", "opus", True, "sid"),
    ("sid", "haiku", "opus", False, ""),
    # '' is the provider default and a real model for mismatch purposes
    ("sid", "", "haiku", False, ""),
    ("sid", "haiku", "", True, "sid"),
    # no session never continues
    ("", "haiku", "haiku", True, ""),
], ids=["same-model", "same-default", "cross-capable", "cross-incapable",
        "default-to-named", "named-to-default-capable", "empty-id"])
def test_continuation_token_matrix(session_id, frm, to, capable, expected):
    from bobi.brain import BrainCapabilities, continuation_token

    class Brain:
        capabilities = BrainCapabilities(cross_model_resume=capable)

    got = continuation_token(
        Brain(), session_id=session_id, from_model=frm, to_model=to,
    )
    assert got == expected


def test_continuation_token_tolerates_capability_less_factory():
    """Test fakes and older factories without a ``capabilities`` attribute
    behave as not-capable, never as capable."""
    from bobi.brain import continuation_token

    class Bare:
        pass

    assert continuation_token(
        Bare(), session_id="sid", from_model="a", to_model="b",
    ) == ""
    assert continuation_token(
        Bare(), session_id="sid", from_model="a", to_model="a",
    ) == "sid"
