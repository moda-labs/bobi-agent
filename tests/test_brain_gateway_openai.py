"""Unit tests for the OpenAI-compatible gateway brain (#777)."""

import os

import pytest

from bobi.brain import (
    GATEWAY_BASE_URL_ENV,
    GATEWAY_SMALL_MODEL_ENV,
    GATEWAY_WIRE_API_ENV,
    GatewayOpenAIBrain,
    get_brain,
    pin_process_brain,
    set_process_brain,
    set_process_brain_from_config,
)
from bobi.brain.gateway_openai import _gateway_openai_overrides

_PIN_VARS = (
    "BOBI_BRAIN",
    "BOBI_BRAIN_MODEL",
    GATEWAY_BASE_URL_ENV,
    GATEWAY_SMALL_MODEL_ENV,
    GATEWAY_WIRE_API_ENV,
)


@pytest.fixture(autouse=True)
def clean_brain_env(monkeypatch):
    for var in _PIN_VARS:
        monkeypatch.delenv(var, raising=False)


def test_gateway_openai_registered():
    brain = get_brain("gateway-openai")
    assert isinstance(brain, GatewayOpenAIBrain)
    assert brain.name == "gateway-openai"
    assert brain.provider == "gateway"
    assert brain.capabilities.cross_model_resume is False


def test_overrides_require_pinned_base_url():
    with pytest.raises(RuntimeError, match="base URL"):
        _gateway_openai_overrides()


def test_overrides_pin_custom_provider_without_openai_key(monkeypatch):
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:9000/v1")
    monkeypatch.setenv(GATEWAY_WIRE_API_ENV, "responses")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")

    overrides = _gateway_openai_overrides()

    assert 'model_provider="bobi_gateway"' in overrides
    assert 'model_providers.bobi_gateway.base_url="http://localhost:9000/v1"' in overrides
    assert 'model_providers.bobi_gateway.env_key="BOBI_GATEWAY_API_KEY"' in overrides
    assert 'model_providers.bobi_gateway.wire_api="responses"' in overrides
    assert not any("OPENAI_API_KEY" in item for item in overrides)


@pytest.mark.asyncio
async def test_provider_overrides_ride_fresh_and_resume_argv(monkeypatch):
    sink = []
    events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.completed", "usage": {}},
    ]

    async def _runner(argv, cwd, stdin_text=None):
        sink.append((argv, cwd, stdin_text))
        for ev in events:
            yield ev

    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:9000/v1")
    monkeypatch.setenv(GATEWAY_WIRE_API_ENV, "chat")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "gateway-model")
    session = GatewayOpenAIBrain().make_session(cwd="/w", system_prompt="SYS")
    session._runner = _runner
    assert session.provider == "gateway"

    await session.connect("first")
    [m async for m in session.receive_response()]
    await session.query("second")
    [m async for m in session.receive_response()]

    fresh_argv = sink[0][0]
    resume_argv = sink[1][0]
    for argv in (fresh_argv, resume_argv):
        assert "-m" in argv
        assert "gateway-model" in argv
        assert "-c" in argv
        assert 'model_provider="bobi_gateway"' in argv
        assert 'model_providers.bobi_gateway.env_key="BOBI_GATEWAY_API_KEY"' in argv
        assert 'model_providers.bobi_gateway.wire_api="chat"' in argv
        assert argv[-1] == "-"
    assert fresh_argv[:2] == ["codex", "exec"]
    assert "resume" not in fresh_argv
    assert resume_argv[:4] == ["codex", "exec", "resume", "th-1"]


def test_pin_process_brain_pins_base_and_wire_but_not_small_model():
    env = {
        GATEWAY_BASE_URL_ENV: "http://stale",
        GATEWAY_SMALL_MODEL_ENV: "stale-small",
        GATEWAY_WIRE_API_ENV: "responses",
    }

    pin_process_brain(
        "gateway-openai", "gpt-5.5", env,
        gateway_base_url="http://localhost:9000/v1",
        gateway_small_model="should-not-pin",
        gateway_wire_api="chat",
    )

    assert env[GATEWAY_BASE_URL_ENV] == "http://localhost:9000/v1"
    assert env[GATEWAY_WIRE_API_ENV] == "chat"
    assert GATEWAY_SMALL_MODEL_ENV not in env

    pin_process_brain(
        "codex", "gpt-5", env,
        gateway_base_url="http://localhost:9000/v1",
        gateway_wire_api="chat",
    )
    assert GATEWAY_BASE_URL_ENV not in env
    assert GATEWAY_WIRE_API_ENV not in env


def test_set_process_brain_gateway_openai_pins():
    os.environ[GATEWAY_SMALL_MODEL_ENV] = "stale-small"
    set_process_brain(
        "gateway-openai", "gpt-5.5",
        gateway_base_url="http://localhost:9000/v1",
        gateway_wire_api="responses",
        gateway_small_model="ignored",
    )
    assert os.environ["BOBI_BRAIN"] == "gateway-openai"
    assert os.environ["BOBI_BRAIN_MODEL"] == "gpt-5.5"
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:9000/v1"
    assert os.environ[GATEWAY_WIRE_API_ENV] == "responses"
    assert GATEWAY_SMALL_MODEL_ENV not in os.environ


def test_set_process_brain_gateway_clears_stale_wire_api():
    os.environ[GATEWAY_WIRE_API_ENV] = "responses"

    set_process_brain(
        "gateway", "qwen3:14b",
        gateway_base_url="http://localhost:4000",
        gateway_small_model="qwen3:4b",
    )

    assert os.environ["BOBI_BRAIN"] == "gateway"
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    assert os.environ[GATEWAY_SMALL_MODEL_ENV] == "qwen3:4b"
    assert GATEWAY_WIRE_API_ENV not in os.environ


def test_set_process_brain_from_config_gateway_openai():
    from bobi.config import Config

    cfg = Config(brain={
        "kind": "gateway-openai",
        "model": "gpt-5.5",
        "base_url": "http://localhost:9000/v1",
        "wire_api": "responses",
        "small_model": "ignored",
    })
    set_process_brain_from_config(cfg)

    assert os.environ["BOBI_BRAIN"] == "gateway-openai"
    assert os.environ["BOBI_BRAIN_MODEL"] == "gpt-5.5"
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:9000/v1"
    assert os.environ[GATEWAY_WIRE_API_ENV] == "responses"
    assert GATEWAY_SMALL_MODEL_ENV not in os.environ
