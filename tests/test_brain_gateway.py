"""Unit tests for claude-engine gateway mode (#655, #789).

Gateway mode is endpoint config on the claude engine (``kind: claude`` +
``brain.base_url``; ``kind: gateway`` stays an accepted alias): the tests
cover alias resolution, the per-session ANTHROPIC_* env injection (base URL,
small-model default, the ANTHROPIC_API_KEY blank and its precedence over
caller-supplied env), the process pins that carry ``brain.base_url`` /
``brain.small_model`` to sessions, the declared-gateway fail-loud guards, and
the provider label that keeps gateway costs out of real Anthropic spend.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bobi.brain import (
    GATEWAY_BASE_URL_ENV,
    GATEWAY_SMALL_MODEL_ENV,
    GATEWAY_WIRE_API_ENV,
    ClaudeBrain,
    GatewayBrain,
    get_brain,
    pin_process_brain,
    set_process_brain,
    set_process_brain_from_config,
)
from bobi.brain.gateway import _gateway_session_env, with_gateway_env

_PIN_VARS = ("BOBI_BRAIN", "BOBI_BRAIN_MODEL",
             GATEWAY_BASE_URL_ENV, GATEWAY_SMALL_MODEL_ENV,
             GATEWAY_WIRE_API_ENV)


@pytest.fixture(autouse=True)
def clean_brain_env(monkeypatch):
    """Clear every pin var; monkeypatch restores originals at teardown even
    when the test body writes os.environ directly (set_process_brain does)."""
    for var in _PIN_VARS:
        monkeypatch.delenv(var, raising=False)


# --- alias resolution / capabilities -----------------------------------------


def test_gateway_alias_resolves_to_claude_engine():
    brain = get_brain("gateway")
    assert isinstance(brain, ClaudeBrain)
    assert brain.name == "claude"
    # the deprecated import alias points at the same engine class
    assert GatewayBrain is ClaudeBrain


def test_ambient_gateway_alias_requires_base_url_pin(monkeypatch):
    """BOBI_BRAIN=gateway promises a gateway; without the base-url pin the
    engine would silently dial real Anthropic - refuse at resolution (the
    operator-override gap the old session-time guard covered)."""
    monkeypatch.setenv("BOBI_BRAIN", "gateway")
    with pytest.raises(RuntimeError, match="base URL"):
        get_brain()
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    assert get_brain().name == "claude"


def test_gateway_mode_flips_provider_and_cross_model_resume(monkeypatch):
    brain = get_brain("claude")
    assert brain.provider == "anthropic"
    # Native claude resumes across models (#642, verified live by
    # tests/integration/test_cross_model_resume.py).
    assert brain.capabilities.cross_model_resume is True

    # Whether an Anthropic-compat backend honors --resume with a different
    # --model is backend-dependent; flip only after live verification (#649
    # arc). Model switches therefore go fresh+reinject, and gateway costs
    # must never blend into real Anthropic spend.
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    assert brain.provider == "gateway"
    assert brain.capabilities.cross_model_resume is False
    # the effort vocabulary is the CLI's in both modes
    assert "max" in brain.capabilities.efforts


# --- per-session env injection ------------------------------------------------


def test_session_env_carries_base_url_and_small_model_default(monkeypatch):
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "qwen3:14b")

    env = _gateway_session_env()

    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    # the main model rides --model (resolve_model chain), never env
    assert "ANTHROPIC_MODEL" not in env
    # small model defaults to the main model so the CLI's background/fast
    # calls never reference a Claude alias the gateway doesn't serve
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "qwen3:14b"


def test_session_env_small_model_override(monkeypatch):
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "qwen3:14b")
    monkeypatch.setenv(GATEWAY_SMALL_MODEL_ENV, "qwen3:4b")

    assert _gateway_session_env()["ANTHROPIC_SMALL_FAST_MODEL"] == "qwen3:4b"


def test_session_env_always_blanks_anthropic_api_key(monkeypatch):
    """An ambient real Anthropic key must never be sent to a gateway; auth is
    ANTHROPIC_AUTH_TOKEN only (inherited by the CLI subprocess untouched)."""
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")

    env = _gateway_session_env()

    assert env["ANTHROPIC_API_KEY"] == ""
    assert "ANTHROPIC_AUTH_TOKEN" not in env  # passthrough, not managed here


def test_session_env_fails_loud_without_base_url_pin():
    """Direct-call safety: the helper refuses to build a gateway env that
    would silently dial real Anthropic carrying the gateway's credentials."""
    with pytest.raises(RuntimeError, match="base URL"):
        _gateway_session_env()


def test_with_gateway_env_wins_over_caller_env(monkeypatch):
    """Callers pass full environment copies (the MCP preflight probe passes
    agent_spawn_env()); a real ANTHROPIC_API_KEY or stale base URL in that
    copy must not defeat the blank or the routing."""
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    merged = with_gateway_env({"env": {
        "ANTHROPIC_API_KEY": "sk-ant-real-from-os-environ-copy",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "SOME_TOOL_VAR": "kept",
    }})
    assert merged["env"]["ANTHROPIC_API_KEY"] == ""
    assert merged["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert merged["env"]["SOME_TOOL_VAR"] == "kept"


def test_make_session_injects_gateway_env_when_pinned(monkeypatch):
    captured = {}

    def _options(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:11434")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "qwen3:14b")
    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        session = ClaudeBrain().make_session(cwd="/tmp", system_prompt=None)

    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert captured["env"]["ANTHROPIC_API_KEY"] == ""
    # the explicit --model chain stays authoritative and unchanged
    assert captured["model"] == "qwen3:14b"
    # gateway costs must never blend into real Anthropic spend
    assert session.provider == "gateway"


def test_make_session_native_without_pin(monkeypatch):
    captured = {}

    def _options(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=_options,
    )}):
        session = ClaudeBrain().make_session(cwd="/tmp", system_prompt=None)

    assert "env" not in captured
    assert session.provider == "anthropic"


@pytest.mark.asyncio
async def test_stream_once_injects_gateway_env(monkeypatch):
    seen = {}

    async def _query(*, prompt, options):
        seen["env"] = options.env
        yield SimpleNamespace()  # ignored non-Result message

    fake_sdk = MagicMock(
        query=_query,
        ClaudeAgentOptions=lambda **kw: SimpleNamespace(**kw),
        AssistantMessage=type("AM", (), {}),
        ResultMessage=type("RM", (), {}),
        StreamEvent=type("SE", (), {}),
        TextBlock=type("TB", (), {}),
    )
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    monkeypatch.setattr("bobi.sdk.get_cli_path", lambda: "/usr/bin/claude")
    with patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}):
        async for _ in ClaudeBrain().stream_once(
            system_prompt="sys", user_prompt="hi", cwd="/tmp",
        ):
            pass

    assert seen["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert seen["env"]["ANTHROPIC_API_KEY"] == ""


# --- process pins -------------------------------------------------------------


def test_pin_process_brain_base_url_drives_gateway_pins():
    env = {GATEWAY_BASE_URL_ENV: "http://stale", GATEWAY_SMALL_MODEL_ENV: "old"}

    # the current spelling: engine kind + base_url
    pin_process_brain(
        "claude", "qwen3:14b", env,
        gateway_base_url="http://localhost:4000",
        gateway_small_model="qwen3:4b",
    )
    assert env["BOBI_BRAIN"] == "claude"
    assert env[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    assert env[GATEWAY_SMALL_MODEL_ENV] == "qwen3:4b"

    # a native team clears stale gateway pins
    pin_process_brain("claude", "opus", env)
    assert GATEWAY_BASE_URL_ENV not in env
    assert GATEWAY_SMALL_MODEL_ENV not in env


def test_pin_process_brain_accepts_alias_kind():
    env: dict = {}
    pin_process_brain(
        "gateway", "qwen3:14b", env,
        gateway_base_url="http://localhost:4000",
        gateway_small_model="qwen3:4b",
    )
    # the config's spelling is pinned verbatim; readers normalize
    assert env["BOBI_BRAIN"] == "gateway"
    assert env[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    assert env[GATEWAY_SMALL_MODEL_ENV] == "qwen3:4b"


def test_pin_process_brain_declared_gateway_requires_base_url():
    """A declared gateway whose ${VAR} resolved empty must fail the spawn
    loud, not pin a native session that dials the real vendor with gateway
    credentials. The alias spelling declares implicitly."""
    with pytest.raises(RuntimeError, match="base_url"):
        pin_process_brain("gateway", "m", {})
    with pytest.raises(RuntimeError, match="base_url"):
        pin_process_brain("claude", "m", {},
                          gateway_base_url="", gateway_declared=True)


def test_set_process_brain_gateway_pins():
    set_process_brain(
        "claude", "qwen3:14b",
        gateway_base_url="http://localhost:4000",
        gateway_small_model="qwen3:4b",
    )
    assert os.environ["BOBI_BRAIN"] == "claude"
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    assert os.environ[GATEWAY_SMALL_MODEL_ENV] == "qwen3:4b"

    # an already-set NON-EMPTY value is left alone (operator override wins)
    set_process_brain("claude", "other", gateway_base_url="http://elsewhere")
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"


def test_set_process_brain_treats_empty_pin_as_unset(monkeypatch):
    """A templated-but-unfilled BOBI_GATEWAY_BASE_URL= (empty) must not block
    the configured endpoint - empty means unset, matching the model pin."""
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "")
    set_process_brain("claude", "m", gateway_base_url="http://localhost:4000")
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"


def test_set_process_brain_alias_matches_engine_override(monkeypatch):
    """`kind: gateway` and BOBI_BRAIN=claude are the same ENGINE - the config
    still tunes the process (gateway mode is endpoint config, not a different
    brain), so old-spelling and new-spelling configs behave identically."""
    monkeypatch.setenv("BOBI_BRAIN", "claude")
    set_process_brain(
        "gateway", "qwen3:14b", gateway_base_url="http://localhost:4000",
    )
    assert os.environ["BOBI_BRAIN"] == "claude"
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    assert os.environ["BOBI_BRAIN_MODEL"] == "qwen3:14b"


def test_set_process_brain_gateway_does_not_cross_engines(monkeypatch):
    """An operator override to a DIFFERENT engine must not inherit the
    config's gateway endpoint - same rule as the model pin."""
    monkeypatch.setenv("BOBI_BRAIN", "codex")
    set_process_brain(
        "gateway", "qwen3:14b", gateway_base_url="http://localhost:4000",
    )
    assert os.environ["BOBI_BRAIN"] == "codex"
    assert GATEWAY_BASE_URL_ENV not in os.environ
    assert "BOBI_BRAIN_MODEL" not in os.environ


def test_set_process_brain_from_config():
    """The one config-to-pins expansion shared by every startup site."""
    from bobi.config import Config

    cfg = Config(brain={"kind": "claude", "model": "qwen3:14b",
                        "base_url": "http://localhost:4000",
                        "small_model": "qwen3:4b"})
    set_process_brain_from_config(cfg)
    assert os.environ["BOBI_BRAIN"] == "claude"
    assert os.environ["BOBI_BRAIN_MODEL"] == "qwen3:14b"
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    assert os.environ[GATEWAY_SMALL_MODEL_ENV] == "qwen3:4b"


def test_set_process_brain_from_config_declared_empty_base_url_raises():
    """Presence-based declaration: a base_url key whose ${VAR} resolved empty
    fails startup loud instead of running the engine natively."""
    from bobi.config import Config

    cfg = Config(brain={"kind": "claude", "base_url": ""})
    with pytest.raises(RuntimeError, match="base_url"):
        set_process_brain_from_config(cfg)
