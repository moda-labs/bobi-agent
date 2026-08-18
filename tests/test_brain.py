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
    monkeypatch.delenv("BOBI_BRAIN_EFFORT", raising=False)


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


# --- reasoning-effort selection (#778) - the model chain's sibling ----------


def test_config_parses_effort(tmp_path):
    """agent.yaml `brain.effort` + `roles.<role>.effort` round-trip (#778)."""
    from bobi.config import Config
    from bobi import paths

    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: t\nbrain:\n  kind: codex\n  effort: high\n"
        "roles:\n  monitor:\n    effort: low\n  planner: {}\n"
    )
    cfg = Config.load(tmp_path)
    assert cfg.brain_effort == "high"
    assert cfg.role_effort("monitor") == "low"
    assert cfg.role_effort("planner") == ""     # role entry without an effort
    assert cfg.role_effort("engineer") == ""    # unknown role
    # Absent config → empty, everything falls through.
    paths.agent_yaml_path(tmp_path).write_text("agent: t\n")
    assert Config.load(tmp_path).brain_effort == ""
    assert Config.load(tmp_path).role_effort("monitor") == ""


def test_resolve_effort_precedence(monkeypatch):
    """explicit > roles.<role>.effort > process default > "" (#778)."""
    from bobi.brain import resolve_effort
    from bobi.config import Config

    cfg = Config(roles={"monitor": {"effort": "low"}})

    assert resolve_effort(cfg, role="monitor", explicit="xhigh") == "xhigh"
    assert resolve_effort(cfg, role="monitor") == "low"
    assert resolve_effort(cfg, role="engineer") == ""   # unconfigured → unchanged
    assert resolve_effort(None, role="monitor") == ""

    monkeypatch.setenv("BOBI_BRAIN_EFFORT", "medium")
    assert resolve_effort(cfg, role="monitor") == "low"      # role beats team default
    assert resolve_effort(cfg, role="engineer") == "medium"  # falls to team default
    assert resolve_effort(None) == "medium"


# --- turn budget (#845) - the third dial, and the honest error it names ----


def test_config_parses_max_turns(tmp_path):
    """agent.yaml `brain.max_turns` + `roles.<role>.max_turns` round-trip."""
    from bobi.config import Config
    from bobi import paths

    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: t\nbrain:\n  kind: claude\n  max_turns: 1500\n"
        "roles:\n  monitor:\n    max_turns: 8\n  planner: {}\n"
    )
    cfg = Config.load(tmp_path)
    assert cfg.brain_max_turns == 1500
    assert cfg.role_max_turns("monitor") == 8
    assert cfg.role_max_turns("planner") == 0   # role entry without a cap
    assert cfg.role_max_turns("engineer") == 0  # unknown role
    # Absent config → 0 ("unconfigured"), everything falls through.
    paths.agent_yaml_path(tmp_path).write_text("agent: t\n")
    assert Config.load(tmp_path).brain_max_turns == 0
    assert Config.load(tmp_path).role_max_turns("monitor") == 0


def test_config_max_turns_rejects_unusable_values(tmp_path):
    """A cap is a positive int; anything else reads as unconfigured, not 0 turns.

    An unresolved ``${VAR}``, a typo, or a non-positive number must fall
    through to the configured chain rather than pin a session to no turns at
    all.
    """
    from bobi.config import Config
    from bobi import paths

    paths.package_dir(tmp_path).mkdir(parents=True)
    # `yes`/`no`/`true` are YAML BOOLS, and 1.5 is a float. A bool where a
    # count belongs is a typo; honoring it as 1/0 would pin a session to one
    # turn or none, which is worse than falling through to the default.
    for value in ("''", "'not-a-number'", "0", "-5", "'${MISSING_VAR}'",
                  "yes", "no", "true", "1.5"):
        paths.agent_yaml_path(tmp_path).write_text(
            f"agent: t\nbrain:\n  max_turns: {value}\n"
        )
        assert Config.load(tmp_path).brain_max_turns == 0, value
    # A quoted integer (what ${VAR} interpolation yields) still counts.
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: t\nbrain:\n  max_turns: '750'\n"
    )
    assert Config.load(tmp_path).brain_max_turns == 750


def test_resolve_max_turns_precedence():
    """explicit > roles.<role>.max_turns > brain.max_turns > default (#845).

    Unlike model/effort there is no provider default to fall through to, so
    the framework default is the floor of the chain, never "".
    """
    from bobi.brain import DEFAULT_MAX_TURNS, resolve_max_turns
    from bobi.config import Config

    cfg = Config(brain={"max_turns": 1500}, roles={"monitor": {"max_turns": 8}})

    assert resolve_max_turns(cfg, role="monitor", explicit=42) == 42
    assert resolve_max_turns(cfg, role="monitor") == 8
    assert resolve_max_turns(cfg, role="engineer") == 1500  # team default
    assert resolve_max_turns(Config(), role="engineer") == DEFAULT_MAX_TURNS
    assert resolve_max_turns(None) == DEFAULT_MAX_TURNS
    # An unusable explicit value defers to config instead of capping at 0.
    assert resolve_max_turns(cfg, role="monitor", explicit=0) == 8
    assert resolve_max_turns(cfg, role="monitor", explicit=-1) == 8


def test_default_max_turns_is_clear_of_real_agent_runs():
    """The default must not be the 200 that killed two engineer sessions.

    Both died on turn 201 hours inside their 6h timeouts; the cap is a
    runaway-loop backstop, so it sits well above observed honest work.
    """
    from bobi.brain import DEFAULT_MAX_TURNS

    assert DEFAULT_MAX_TURNS >= 1000


def test_turn_error_text_prefers_the_brains_diagnosis():
    """The composition that replaced the literal "turn failed" (#845)."""
    from bobi.brain import ERROR_KIND_MAX_TURNS, TurnResult, turn_error_text

    # A successful turn has no error text at all.
    assert turn_error_text(is_error=False) == ""

    # The turn-cap shape: error_message set, result_text EMPTY. This exact
    # combination used to render as the literal fallback "turn failed".
    capped = TurnResult(
        is_error=True,
        error_kind=ERROR_KIND_MAX_TURNS,
        error_message="max_turns_reached (max=1000, turns=1001)",
        result_text="",
    )
    assert capped.error_text() == "max_turns_reached (max=1000, turns=1001)"
    assert "turn failed" not in capped.error_text()

    # Both present → both reported, diagnosis first.
    assert turn_error_text(
        is_error=True, error_message="max_turns_reached", result_text="ran out",
    ) == "max_turns_reached: ran out"

    # Only result_text → use it.
    assert turn_error_text(is_error=True, result_text="boom") == "boom"

    # Nothing but a status: still self-describing, never "unknown error".
    last_resort = turn_error_text(is_error=True, api_error_status=529)
    assert "529" in last_resort and "unknown error" not in last_resort

    # A classified kind with is_error unset is still a failure (the rule the
    # adapters use to derive is_error).
    assert TurnResult(
        is_error=False, error_kind=ERROR_KIND_MAX_TURNS,
        error_message="max_turns_reached (max=8, turns=9)",
    ).error_text() == "max_turns_reached (max=8, turns=9)"


def test_set_process_brain_pins_effort():
    from bobi.brain import (
        BRAIN_ENV,
        get_process_brain_effort,
        set_process_brain,
    )

    effort_env = "BOBI_BRAIN_EFFORT"
    saved = os.environ.pop(BRAIN_ENV, None)
    saved_effort = os.environ.pop(effort_env, None)
    try:
        set_process_brain("", effort="high")  # effort-only tunes default Claude
        assert get_process_brain_effort() == "high"
        set_process_brain("", effort="low")   # first writer wins
        assert get_process_brain_effort() == "high"
        os.environ.pop(effort_env)
        os.environ[BRAIN_ENV] = "codex"
        set_process_brain("", effort="high")  # default-brain config ≠ active brain
        assert get_process_brain_effort() == ""
    finally:
        if saved is None:
            os.environ.pop(BRAIN_ENV, None)
        else:
            os.environ[BRAIN_ENV] = saved
        if saved_effort is None:
            os.environ.pop(effort_env, None)
        else:
            os.environ[effort_env] = saved_effort


def test_pin_process_brain_pins_and_clears_effort():
    from bobi.brain import pin_process_brain

    env: dict[str, str] = {"BOBI_BRAIN_EFFORT": "stale"}
    pin_process_brain("codex", "gpt-5.6", env, effort="high")
    assert env["BOBI_BRAIN_EFFORT"] == "high"
    pin_process_brain("codex", "gpt-5.6", env)
    assert "BOBI_BRAIN_EFFORT" not in env  # unset config clears a stale pin


def test_claude_brain_uses_env_effort_default(monkeypatch):
    captured, _options = _capture_options()
    monkeypatch.setenv("BOBI_BRAIN_EFFORT", "medium")
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        ClaudeBrain().make_session(cwd="/tmp", system_prompt=None)

    assert captured["effort"] == "medium"


def test_claude_brain_explicit_effort_overrides_env(monkeypatch):
    captured, _options = _capture_options()
    monkeypatch.setenv("BOBI_BRAIN_EFFORT", "medium")
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        ClaudeBrain().make_session(
            cwd="/tmp", system_prompt=None,
            options={"effort": "xhigh"},
        )

    assert captured["effort"] == "xhigh"


def test_claude_brain_omits_unset_effort():
    """No configured effort → the option key never reaches the SDK (an empty
    string would render as a literal --effort value)."""
    captured, _options = _capture_options()
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        ClaudeBrain().make_session(
            cwd="/tmp", system_prompt=None, options={"effort": ""},
        )

    assert "effort" not in captured


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


# --- max_buffer_size: never inherit the SDK's session-killing 1 MB default ---
# (#719 / #718)


def _capture_options():
    captured = {}

    def _options(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    return captured, _options


def test_make_session_sets_generous_max_buffer_size(monkeypatch):
    from bobi.brain.claude import DEFAULT_MAX_BUFFER_SIZE

    monkeypatch.delenv("BOBI_CLAUDE_MAX_BUFFER_SIZE", raising=False)
    captured, _options = _capture_options()
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        ClaudeBrain().make_session(cwd="/tmp", system_prompt=None)

    assert captured["max_buffer_size"] == DEFAULT_MAX_BUFFER_SIZE
    # Anything above the SDK's fatal 1 MB default is the whole point.
    assert captured["max_buffer_size"] > 1024 * 1024


def test_make_session_max_buffer_size_env_override(monkeypatch):
    monkeypatch.setenv("BOBI_CLAUDE_MAX_BUFFER_SIZE", str(8 * 1024 * 1024))
    captured, _options = _capture_options()
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        ClaudeBrain().make_session(cwd="/tmp", system_prompt=None)

    assert captured["max_buffer_size"] == 8 * 1024 * 1024


def test_make_session_explicit_max_buffer_size_wins(monkeypatch):
    captured, _options = _capture_options()
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        ClaudeBrain().make_session(
            cwd="/tmp", system_prompt=None,
            options={"max_buffer_size": 123},
        )

    assert captured["max_buffer_size"] == 123


def test_max_buffer_size_is_floored_at_the_sdk_default(monkeypatch):
    """A misconfigured tiny/zero override must not silently recreate the 1 MB
    kill window — the knob only raises the ceiling."""
    from bobi.brain.claude import _max_buffer_size, _SDK_DEFAULT_MAX_BUFFER_SIZE

    monkeypatch.setenv("BOBI_CLAUDE_MAX_BUFFER_SIZE", "0")
    assert _max_buffer_size() == _SDK_DEFAULT_MAX_BUFFER_SIZE

    monkeypatch.setenv("BOBI_CLAUDE_MAX_BUFFER_SIZE", "1024")  # 1 KB
    assert _max_buffer_size() == _SDK_DEFAULT_MAX_BUFFER_SIZE


@pytest.mark.asyncio
async def test_stream_once_sets_generous_max_buffer_size(monkeypatch):
    from bobi.brain.claude import DEFAULT_MAX_BUFFER_SIZE

    monkeypatch.delenv("BOBI_CLAUDE_MAX_BUFFER_SIZE", raising=False)
    monkeypatch.setattr("bobi.brain.claude.get_cli_path", lambda: "/usr/bin/claude")
    captured = {}

    def _options(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    async def _fake_query(prompt, options):
        return
        yield  # make this an async generator

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=_options,
        ResultMessage=ResultMessage,
        StreamEvent=type("StreamEvent", (), {}),
        TextBlock=TextBlock,
        query=_fake_query,
    )}):
        async for _ in ClaudeBrain().stream_once(
            system_prompt=None, user_prompt="hi",
        ):
            pass

    assert captured["max_buffer_size"] == DEFAULT_MAX_BUFFER_SIZE


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
    """A list of usage objects still populates the BrainCost list."""

    class _MU:
        def __init__(self, model, i, o):
            self.model, self.input_tokens, self.output_tokens = model, i, o

    msg = _result(model_usage=[_MU("claude-opus-4-8", 10, 3), _MU("haiku", 1, 1)])
    turn = _result_to_turn(msg)
    assert [(c.model, c.input_tokens, c.output_tokens) for c in turn.costs] == [
        ("claude-opus-4-8", 10, 3),
        ("haiku", 1, 1),
    ]


def test_result_to_turn_normalizes_dict_model_usage():
    """Claude's SDK reports model_usage as model -> usage; keep those token
    facts so Claude has parity with the token-volume surfaces."""
    msg = _result(model_usage={
        "claude-opus-4-8": {"input_tokens": 10, "output_tokens": 3}
    })
    turn = _result_to_turn(msg)
    assert len(turn.costs) == 1
    assert turn.costs[0].model == "claude-opus-4-8"
    assert turn.costs[0].input_tokens == 10
    assert turn.costs[0].output_tokens == 3


def test_result_to_turn_includes_claude_cache_tokens_in_input_volume():
    msg = _result(model_usage={
        "claude-opus-4-8": {
            "input_tokens": 2,
            "cache_read_input_tokens": 422_468,
            "cache_creation_input_tokens": 1_262,
            "output_tokens": 3_432,
        }
    })
    turn = _result_to_turn(msg)
    assert len(turn.costs) == 1
    assert turn.costs[0].model == "claude-opus-4-8"
    assert turn.costs[0].input_tokens == 423_732
    assert turn.costs[0].cached_input_tokens == 422_468
    assert turn.costs[0].output_tokens == 3_432


def test_result_to_turn_normalizes_sdk_camel_case_model_usage():
    """The shape claude-agent-sdk 0.2.128 actually reports (#935).

    ``claude_agent_sdk.types.ModelUsage`` is passed through verbatim from the
    CLI's ``modelUsage`` field, so its keys are camelCase. Reading only the
    legacy snake_case spellings recorded every token counter as zero while
    ``total_cost_usd`` still arrived, which is how the GTM registry ended up
    with dollars and no tokens.
    """
    msg = _result(model_usage={
        "claude-opus-4-8": {
            "inputTokens": 2,
            "cacheCreationInputTokens": 24_576,
            "cacheReadInputTokens": 15_282,
            "outputTokens": 10,
            "webSearchRequests": 0,
            "costUSD": 0.253661,
            "contextWindow": 200_000,
            "maxOutputTokens": 32_000,
            "canonicalModel": "claude-opus-4-8",
            "provider": "firstParty",
        }
    })
    turn = _result_to_turn(msg)
    assert len(turn.costs) == 1
    cost = turn.costs[0]
    assert cost.model == "claude-opus-4-8"
    # Observed smoke payload: 2 + 24,576 + 15,282 + 10 == 39,870 total.
    assert cost.input_tokens == 39_860
    assert cost.cached_input_tokens == 15_282
    assert cost.output_tokens == 10
    assert cost.input_tokens + cost.output_tokens == 39_870


def test_result_to_turn_normalizes_camel_case_object_usage():
    """An object-like usage value with camelCase attributes normalizes too."""
    usage = SimpleNamespace(
        inputTokens=5,
        outputTokens=7,
        cacheReadInputTokens=11,
        cacheCreationInputTokens=13,
        canonicalModel="claude-haiku-4-5",
    )
    turn = _result_to_turn(_result(model_usage=[usage]))
    assert len(turn.costs) == 1
    assert turn.costs[0].model == "claude-haiku-4-5"
    assert turn.costs[0].input_tokens == 29
    assert turn.costs[0].cached_input_tokens == 11
    assert turn.costs[0].output_tokens == 7


def test_result_to_turn_prefers_snake_case_when_both_spellings_present():
    """A mixed dict must not double-count: one spelling wins per field."""
    turn = _result_to_turn(_result(model_usage={
        "claude-opus-4-8": {
            "input_tokens": 100,
            "inputTokens": 100,
            "output_tokens": 5,
            "outputTokens": 5,
            "cache_read_input_tokens": 20,
            "cacheReadInputTokens": 20,
        }
    }))
    assert turn.costs[0].input_tokens == 120
    assert turn.costs[0].cached_input_tokens == 20
    assert turn.costs[0].output_tokens == 5


def test_result_to_turn_handles_error_and_status():
    msg = _result(is_error=True, result="API Error: 529 Overloaded")
    msg.api_error_status = 529
    turn = _result_to_turn(msg)
    assert turn.is_error is True
    assert turn.api_error_status == 529
    assert turn.result_text == "API Error: 529 Overloaded"


def test_result_to_turn_normalizes_max_turns_error():
    msg = _result(
        is_error=True,
        result=None,
        stop_reason="max_turns_reached",
        errors=[
            '{"type":"attachment","attachment":{"type":"max_turns_reached",'
            '"maxTurns":8,"turnCount":9}}',
        ],
    )
    turn = _result_to_turn(msg)
    assert turn.is_error is True
    assert turn.error_kind == "max_turns_reached"
    assert turn.max_turns == 8
    assert turn.turn_count == 9
    assert turn.error_message == "max_turns_reached (max=8, turns=9)"


def test_result_to_turn_marks_max_turns_as_error_without_error_flag():
    msg = _result(
        is_error=False,
        result=None,
        stop_reason="max_turns_reached",
        errors={
            "type": "attachment",
            "attachment": {
                "type": "max_turns_reached",
                "max_turns": 8,
                "turn_count": 9,
            },
        },
    )
    turn = _result_to_turn(msg)
    assert turn.is_error is True
    assert turn.error_kind == "max_turns_reached"
    assert turn.error_message == "max_turns_reached (max=8, turns=9)"


def test_result_to_turn_falls_back_to_transcript_max_turns_attachment(
    tmp_path, monkeypatch
):
    claude_dir = tmp_path / "claude"
    transcript = claude_dir / "projects" / "proj" / "sess-curator.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"assistant","message":{"content":"not json"}}\n'
        '{"type":"attachment","attachment":{"type":"max_turns_reached",'
        '"maxTurns":10,"turnCount":11}}\n'
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    msg = _result(
        is_error=True,
        result=None,
        session_id="sess-curator",
        errors=[],
    )
    turn = _result_to_turn(msg)
    assert turn.is_error is True
    assert turn.error_kind == "max_turns_reached"
    assert turn.max_turns == 10
    assert turn.turn_count == 11
    assert turn.error_message == "max_turns_reached (max=10, turns=11)"


def test_result_to_turn_falls_back_to_transcript_when_error_flag_missing(
    tmp_path, monkeypatch
):
    claude_dir = tmp_path / "claude"
    transcript = claude_dir / "projects" / "proj" / "sess-curator.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"attachment","attachment":{"type":"max_turns_reached",'
        '"maxTurns":10,"turnCount":11}}\n'
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    turn = _result_to_turn(_result(
        is_error=False,
        result=None,
        session_id="sess-curator",
        errors=[],
    ))
    assert turn.is_error is True
    assert turn.error_kind == "max_turns_reached"
    assert turn.error_message == "max_turns_reached (max=10, turns=11)"


def test_result_to_turn_handles_transcript_max_turns_without_counts(
    tmp_path, monkeypatch
):
    claude_dir = tmp_path / "claude"
    transcript = claude_dir / "projects" / "proj" / "sess-curator.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"attachment","attachment":{"type":"max_turns_reached"}}\n'
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    turn = _result_to_turn(_result(
        is_error=True,
        result=None,
        session_id="sess-curator",
        errors=[],
    ))
    assert turn.is_error is True
    assert turn.error_kind == "max_turns_reached"
    assert turn.max_turns is None
    assert turn.turn_count is None
    assert turn.error_message == "max_turns_reached"


def test_result_to_turn_does_not_use_transcript_for_successful_result(
    tmp_path, monkeypatch
):
    claude_dir = tmp_path / "claude"
    transcript = claude_dir / "projects" / "proj" / "sess-curator.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"attachment","attachment":{"type":"max_turns_reached",'
        '"maxTurns":10,"turnCount":11}}\n'
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    turn = _result_to_turn(_result(session_id="sess-curator", result="ok"))
    assert turn.is_error is False
    assert turn.error_kind == ""
    assert turn.result_text == "ok"


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


def test_claude_abort_force_kills_live_transport_process():
    class Process:
        returncode = None

        def __init__(self):
            self.is_killed = False

        def kill(self):
            self.is_killed = True

    process = Process()
    transport = SimpleNamespace(_process=process)
    sdk_client = SimpleNamespace(_transport=transport, _query=None)
    session = _ClaudeSession.__new__(_ClaudeSession)
    session._client = sdk_client

    session.abort()

    assert process.is_killed is True


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
    monkeypatch.setattr("bobi.brain.claude.get_cli_path", lambda: "/usr/bin/claude")
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
    monkeypatch.setattr("bobi.brain.claude.get_cli_path", lambda: "/usr/bin/claude")
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


def test_brains_support_cross_model_resume():
    from bobi.brain import CodexBrain
    # capabilities is a property (gateway mode flips it, #789): instance access
    assert ClaudeBrain().capabilities.cross_model_resume is True
    # Verified live: `codex exec resume -m` switches the thread's model
    # (#649; see tests/integration/test_cross_model_resume.py).
    assert CodexBrain().capabilities.cross_model_resume is True


@pytest.mark.parametrize("session_id,frm,to,capable,expected", [
    # same model always continues, capability irrelevant
    ("sid", "haiku", "haiku", False, "sid"),
    ("sid", "", "", False, "sid"),
    # cross-model requires the capability
    ("sid", "haiku", "opus", True, "sid"),
    ("sid", "haiku", "opus", False, ""),
    # '' is the provider default and a real model for mismatch purposes
    ("sid", "", "haiku", True, "sid"),
    ("sid", "", "haiku", False, ""),
    # cross-model continuation needs a CONCRETE target: "onto the provider
    # default" cannot be expressed to the CLI, so it goes fresh even when
    # the brain is capable
    ("sid", "haiku", "", True, ""),
    # no session never continues
    ("", "haiku", "haiku", True, ""),
], ids=["same-model", "same-default", "cross-capable", "cross-incapable",
        "default-to-named-capable", "default-to-named-incapable",
        "named-to-default-goes-fresh", "empty-id"])
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


class TestBrainFactoryContract:
    """Q026: the BrainFactory protocol says what the call sites require.

    ``stream_once`` was a de facto requirement for years - ``bobi.setup.llm``
    calls it on whatever brain the process is bound to - while the protocol
    declared only ``make_session``. A brain without it failed with a bare
    ``AttributeError``, laundered into ``LLMError``, naming nothing.
    """

    def test_every_registered_brain_implements_the_protocol(self):
        import inspect

        from bobi.brain import _BRAINS
        from bobi.brain.base import BrainFactory

        required = [n for n, v in vars(BrainFactory).items()
                    if not n.startswith("_") and inspect.isfunction(v)]
        assert "stream_once" in required, "the protocol must declare it"
        assert "make_session" in required

        for kind, factory in _BRAINS.items():
            for method in required:
                assert callable(getattr(factory, method, None)), (
                    f"brain {kind!r} does not implement {method}")

    @pytest.mark.asyncio
    async def test_codex_stream_once_names_itself(self):
        from bobi.brain.codex import CodexBrain

        with pytest.raises(NotImplementedError) as exc:
            async for _ in CodexBrain().stream_once(
                    system_prompt="s", user_prompt="u"):
                pass  # pragma: no cover - the first step raises

        assert "codex" in str(exc.value)

    @pytest.mark.asyncio
    async def test_setup_pour_surfaces_the_named_gap(self, monkeypatch):
        """The transport still raises LLMError - with a message that helps."""
        import bobi.brain as brain_mod
        from bobi.brain.codex import CodexBrain
        from bobi.setup import llm

        monkeypatch.setattr(brain_mod, "get_brain", lambda *a, **k: CodexBrain())
        monkeypatch.setattr("bobi.runtime_guard.prepare_brain_runtime", lambda: None)

        with pytest.raises(llm.LLMError) as exc:
            async for _ in llm._sdk_stream(system_prompt="s", user_prompt="u"):
                pass  # pragma: no cover - the first step raises

        assert "codex" in str(exc.value)
        assert "AttributeError" not in str(exc.value)
