"""Unit tests for codex-engine gateway mode (#777, #789).

Gateway mode is endpoint config on the codex engine (``kind: codex`` +
``brain.base_url``; ``kind: gateway-openai`` stays an accepted alias): the
tests cover alias resolution, the ``bobi_gateway`` provider overrides riding
every fresh and resumed invocation, the model/effort chain staying on the
single shared path (the #789 effort-drop regression), and the engine-specific
process pins (``wire_api``, never ``small_model``).
"""

import os

import pytest

from bobi.brain import (
    GATEWAY_BASE_URL_ENV,
    GATEWAY_SMALL_MODEL_ENV,
    GATEWAY_WIRE_API_ENV,
    CodexBrain,
    GatewayOpenAIBrain,
    get_brain,
    pin_process_brain,
    set_process_brain,
    set_process_brain_from_config,
)
from bobi.brain.gateway_openai import gateway_openai_overrides

_PIN_VARS = (
    "BOBI_BRAIN",
    "BOBI_BRAIN_MODEL",
    "BOBI_BRAIN_EFFORT",
    GATEWAY_BASE_URL_ENV,
    GATEWAY_SMALL_MODEL_ENV,
    GATEWAY_WIRE_API_ENV,
)


@pytest.fixture(autouse=True)
def clean_brain_env(monkeypatch):
    for var in _PIN_VARS:
        monkeypatch.delenv(var, raising=False)


def test_gateway_openai_alias_resolves_to_codex_engine(monkeypatch):
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:9000/v1")
    brain = get_brain("gateway-openai")
    assert isinstance(brain, CodexBrain)
    assert brain.name == "codex"
    # the deprecated import alias points at the same engine class
    assert GatewayOpenAIBrain is CodexBrain


def test_gateway_openai_alias_requires_base_url_pin(monkeypatch):
    monkeypatch.setenv("BOBI_BRAIN", "gateway-openai")
    with pytest.raises(RuntimeError, match="base URL"):
        get_brain()
    with pytest.raises(RuntimeError, match="base URL"):
        get_brain("gateway-openai")
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:9000/v1")
    assert get_brain().name == "codex"
    assert get_brain("gateway-openai").name == "codex"


def test_gateway_mode_flips_provider_and_cross_model_resume(monkeypatch):
    brain = get_brain("codex")
    assert brain.provider == "openai"
    # Native codex switches a thread's model with the transcript intact
    # (verified live 2026-07-04 on codex-cli 0.142.2, #649).
    assert brain.capabilities.cross_model_resume is True

    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:9000/v1")
    assert brain.provider == "gateway"
    assert brain.capabilities.cross_model_resume is False
    # the effort vocabulary is the engine's in both modes (#789): validate
    # checks gateway teams against what codex's config actually parses
    assert "xhigh" in brain.capabilities.efforts
    assert "max" not in brain.capabilities.efforts


def test_overrides_require_pinned_base_url():
    with pytest.raises(RuntimeError, match="base URL"):
        gateway_openai_overrides()


def test_overrides_pin_custom_provider_without_openai_key(monkeypatch):
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:9000/v1")
    monkeypatch.setenv(GATEWAY_WIRE_API_ENV, "responses")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")

    overrides = gateway_openai_overrides()

    assert 'model_provider="bobi_gateway"' in overrides
    assert 'model_providers.bobi_gateway.base_url="http://localhost:9000/v1"' in overrides
    assert 'model_providers.bobi_gateway.env_key="BOBI_GATEWAY_API_KEY"' in overrides
    assert 'model_providers.bobi_gateway.wire_api="responses"' in overrides
    assert not any("OPENAI_API_KEY" in item for item in overrides)


@pytest.mark.asyncio
async def test_provider_overrides_and_effort_ride_fresh_and_resume_argv(
    monkeypatch, tmp_path,
):
    """The #789 regression: the old GatewayOpenAIBrain re-implemented
    make_session and silently dropped the resolved effort. Model AND effort
    now ride the single CodexBrain path alongside the provider overrides,
    on the fresh and the resumed invocation alike."""
    sink = []
    events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.completed", "usage": {}},
    ]

    async def _runner(argv, cwd, stdin_text=None):
        sink.append((argv, cwd, stdin_text))
        for ev in events:
            yield ev

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv(GATEWAY_BASE_URL_ENV, "http://localhost:9000/v1")
    monkeypatch.setenv(GATEWAY_WIRE_API_ENV, "chat")
    monkeypatch.setenv("BOBI_BRAIN_MODEL", "gateway-model")
    monkeypatch.setenv("BOBI_BRAIN_EFFORT", "xhigh")
    session = CodexBrain().make_session(cwd="/w", system_prompt="SYS")
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
        assert "model_reasoning_effort=xhigh" in argv
        assert 'model_provider="bobi_gateway"' in argv
        assert 'model_providers.bobi_gateway.env_key="BOBI_GATEWAY_API_KEY"' in argv
        assert 'model_providers.bobi_gateway.wire_api="chat"' in argv
        assert argv[-1] == "-"
    assert fresh_argv[:2] == ["codex", "exec"]
    assert "resume" not in fresh_argv
    assert resume_argv[:4] == ["codex", "exec", "resume", "th-1"]


def test_native_codex_session_has_no_provider_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    session = CodexBrain().make_session(cwd="/w", system_prompt="SYS")
    assert session.provider == "openai"
    assert session._config_overrides == []


def test_pin_process_brain_pins_base_and_wire_but_not_small_model():
    env = {
        GATEWAY_BASE_URL_ENV: "http://stale",
        GATEWAY_SMALL_MODEL_ENV: "stale-small",
        GATEWAY_WIRE_API_ENV: "responses",
    }

    pin_process_brain(
        "codex", "gpt-5.5", env,
        gateway_base_url="http://localhost:9000/v1",
        gateway_small_model="should-not-pin",
        gateway_wire_api="chat",
    )

    assert env["BOBI_BRAIN"] == "codex"
    assert env[GATEWAY_BASE_URL_ENV] == "http://localhost:9000/v1"
    assert env[GATEWAY_WIRE_API_ENV] == "chat"
    assert GATEWAY_SMALL_MODEL_ENV not in env

    # a native codex team clears all gateway pins
    pin_process_brain("codex", "gpt-5", env)
    assert GATEWAY_BASE_URL_ENV not in env
    assert GATEWAY_WIRE_API_ENV not in env


def test_pin_process_brain_accepts_alias_kind():
    env: dict = {}
    pin_process_brain(
        "gateway-openai", "gpt-5.5", env,
        gateway_base_url="http://localhost:9000/v1",
        gateway_wire_api="responses",
    )
    # the config's spelling is pinned verbatim; readers normalize
    assert env["BOBI_BRAIN"] == "gateway-openai"
    assert env[GATEWAY_BASE_URL_ENV] == "http://localhost:9000/v1"
    assert env[GATEWAY_WIRE_API_ENV] == "responses"
    assert GATEWAY_SMALL_MODEL_ENV not in env


def test_pin_process_brain_alias_requires_base_url():
    with pytest.raises(RuntimeError, match="base_url"):
        pin_process_brain("gateway-openai", "gpt-5.5", {})


def test_set_process_brain_codex_gateway_pins():
    os.environ[GATEWAY_SMALL_MODEL_ENV] = "stale-small"
    set_process_brain(
        "codex", "gpt-5.5",
        gateway_base_url="http://localhost:9000/v1",
        gateway_wire_api="responses",
        gateway_small_model="ignored",
    )
    assert os.environ["BOBI_BRAIN"] == "codex"
    assert os.environ["BOBI_BRAIN_MODEL"] == "gpt-5.5"
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:9000/v1"
    assert os.environ[GATEWAY_WIRE_API_ENV] == "responses"
    assert GATEWAY_SMALL_MODEL_ENV not in os.environ


def test_set_process_brain_claude_gateway_clears_stale_wire_api():
    os.environ[GATEWAY_WIRE_API_ENV] = "responses"

    set_process_brain(
        "claude", "qwen3:14b",
        gateway_base_url="http://localhost:4000",
        gateway_small_model="qwen3:4b",
    )

    assert os.environ["BOBI_BRAIN"] == "claude"
    assert os.environ[GATEWAY_BASE_URL_ENV] == "http://localhost:4000"
    assert os.environ[GATEWAY_SMALL_MODEL_ENV] == "qwen3:4b"
    assert GATEWAY_WIRE_API_ENV not in os.environ


def test_set_process_brain_from_config_alias_kind():
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
