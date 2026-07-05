"""Unit tests for the Anthropic-compatible gateway brain (#655, epic #548).

The gateway brain is ClaudeBrain pointed at a different endpoint: the tests
cover the registry entry, the per-session ANTHROPIC_* env injection (base URL,
model defaults, the ANTHROPIC_API_KEY blank), the process pins that carry
``brain.base_url`` / ``brain.small_model`` to sessions, and the provider label
that keeps gateway costs out of real Anthropic spend.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bobi.brain import (
    GATEWAY_BASE_URL_ENV,
    GATEWAY_SMALL_MODEL_ENV,
    GatewayBrain,
    get_brain,
    pin_process_brain,
    set_process_brain,
)
from bobi.brain.gateway import _gateway_session_env, _with_gateway_env


@pytest.fixture(autouse=True)
def clean_brain_env(monkeypatch):
    for var in ("BOBI_BRAIN", "BOBI_BRAIN_MODEL",
                GATEWAY_BASE_URL_ENV, GATEWAY_SMALL_MODEL_ENV):
        monkeypatch.delenv(var, raising=False)


# --- registry / capabilities -------------------------------------------------


def test_gateway_registered():
    brain = get_brain("gateway")
    assert isinstance(brain, GatewayBrain)
    assert brain.name == "gateway"
    assert brain.provider == "gateway"


def test_gateway_resolves_from_env(monkeypatch):
    monkeypatch.setenv("BOBI_BRAIN", "gateway")
    assert get_brain().name == "gateway"


def test_gateway_ships_conservative_cross_model_resume():
    # Whether an Anthropic-compat backend honors --resume with a different
    # --model is backend-dependent; flip only after live verification (#649
    # arc). Model switches therefore go fresh+reinject.
    assert GatewayBrain.capabilities.cross_model_resume is False


# --- per-session env injection ------------------------------------------------


def test_session_env_carries_base_url_and_model_defaults(monkeypatch):
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "qwen3:14b")

    env = _gateway_session_env()

    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert env["ANTHROPIC_MODEL"] == "qwen3:14b"
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")

    env = _gateway_session_env()

    assert env["ANTHROPIC_API_KEY"] == ""
    assert "ANTHROPIC_AUTH_TOKEN" not in env  # passthrough, not managed here


def test_session_env_without_pins_still_blanks_key():
    env = _gateway_session_env()
    assert env == {"ANTHROPIC_API_KEY": ""}


def test_with_gateway_env_caller_env_wins(monkeypatch):
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:4000")
    merged = _with_gateway_env({"env": {"ANTHROPIC_BASE_URL": "http://other"}})
    assert merged["env"]["ANTHROPIC_BASE_URL"] == "http://other"
    assert merged["env"]["ANTHROPIC_API_KEY"] == ""


def test_make_session_injects_gateway_env(monkeypatch):
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
        session = GatewayBrain().make_session(cwd="/tmp", system_prompt=None)

    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert captured["env"]["ANTHROPIC_API_KEY"] == ""
    # the explicit --model chain stays authoritative and unchanged
    assert captured["model"] == "qwen3:14b"
    # gateway costs must never blend into real Anthropic spend
    assert session.provider == "gateway"


def test_claude_make_session_keeps_anthropic_provider(monkeypatch):
    from bobi.brain import ClaudeBrain

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
        ClaudeSDKClient=MagicMock(),
        ClaudeAgentOptions=lambda **kw: SimpleNamespace(**kw),
    )}):
        session = ClaudeBrain().make_session(cwd="/tmp", system_prompt=None)

    assert session.provider == "anthropic"


@pytest.mark.asyncio
async def test_stream_once_injects_gateway_env(monkeypatch):
    from bobi.brain import TurnResult

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
        async for _ in GatewayBrain().stream_once(
            system_prompt="sys", user_prompt="hi", cwd="/tmp",
        ):
            pass

    assert seen["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert seen["env"]["ANTHROPIC_API_KEY"] == ""


# --- process pins -------------------------------------------------------------


def test_pin_process_brain_pins_and_clears_gateway_values():
    env = {GATEWAY_BASE_URL_ENV: "http://stale", GATEWAY_SMALL_MODEL_ENV: "old"}

    pin_process_brain(
        "gateway", "qwen3:14b", env,
        gateway_base_url="http://localhost:4000",
        gateway_small_model="qwen3:4b",
    )
    assert env[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    assert env[GATEWAY_SMALL_MODEL_ENV] == "qwen3:4b"

    # a non-gateway team clears stale gateway pins even if values are passed
    pin_process_brain(
        "claude", "opus", env,
        gateway_base_url="http://localhost:4000",
        gateway_small_model="qwen3:4b",
    )
    assert GATEWAY_BASE_URL_ENV not in env
    assert GATEWAY_SMALL_MODEL_ENV not in env


def test_set_process_brain_gateway_pins():
    saved = {k: os.environ.pop(k, None)
             for k in ("BOBI_BRAIN", "BOBI_BRAIN_MODEL",
                       GATEWAY_BASE_URL_ENV, GATEWAY_SMALL_MODEL_ENV)}
    try:
        set_process_brain(
            "gateway", "qwen3:14b",
            gateway_base_url="http://localhost:4000",
            gateway_small_model="qwen3:4b",
        )
        assert os.environ["BOBI_BRAIN"] == "gateway"
        assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
        assert os.environ[GATEWAY_SMALL_MODEL_ENV] == "qwen3:4b"

        # already-set values are left alone (operator override wins)
        set_process_brain(
            "gateway", "other", gateway_base_url="http://elsewhere",
        )
        assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_set_process_brain_gateway_does_not_cross_onto_overridden_brain():
    """An operator BOBI_BRAIN override must not inherit the config's gateway
    endpoint - same rule as the model pin."""
    saved = {k: os.environ.pop(k, None)
             for k in ("BOBI_BRAIN", "BOBI_BRAIN_MODEL",
                       GATEWAY_BASE_URL_ENV, GATEWAY_SMALL_MODEL_ENV)}
    try:
        os.environ["BOBI_BRAIN"] = "claude"
        set_process_brain(
            "gateway", "qwen3:14b", gateway_base_url="http://localhost:4000",
        )
        assert os.environ["BOBI_BRAIN"] == "claude"
        assert GATEWAY_BASE_URL_ENV not in os.environ
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
