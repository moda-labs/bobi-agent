"""OpenAI-compatible gateway endpoint support for the codex engine (#777, #789).

Codex natively supports custom OpenAI-compatible providers via per-invocation
``-c`` overrides. Gateway mode keeps the normal Codex session machinery but
pins one provider named ``bobi_gateway`` for every fresh and resumed turn, so
no process-wide ``~/.codex/config.toml`` provider state is required. It is
endpoint CONFIG on the codex engine (``kind: codex`` + ``brain.base_url``),
not a separate brain: ``CodexBrain`` injects these overrides whenever the
gateway base-url pin is set.

``kind: gateway-openai`` remains an accepted alias for this configuration
(``BRAIN_KIND_ALIASES``); ``GatewayOpenAIBrain`` below is the matching import
alias.
"""

from __future__ import annotations

import json
import os

from bobi.brain.codex import CodexBrain

GATEWAY_WIRE_API_ENV = "BOBI_GATEWAY_WIRE_API"
_PROVIDER_ID = "bobi_gateway"
_GATEWAY_API_KEY_ENV = "BOBI_GATEWAY_API_KEY"

# Deprecated: gateway mode no longer has its own factory class (#789). Kept so
# `from bobi.brain import GatewayOpenAIBrain` keeps resolving for external code.
GatewayOpenAIBrain = CodexBrain


def _toml_string(value: str) -> str:
    """Return *value* quoted for a Codex ``-c key=value`` TOML override."""
    return json.dumps(value)


def gateway_openai_overrides() -> list[str]:
    """Provider overrides for one gateway-mode Codex invocation.

    Raises when called without a pinned base URL - see
    ``gateway._gateway_session_env`` for the guard rationale; the pin sites
    and ``get_brain``'s ambient-alias check catch the gap before sessions
    get here.
    """
    from bobi.brain.gateway import gateway_base_url

    base_url = gateway_base_url()
    if not base_url:
        raise RuntimeError(
            "gateway session requested but no base URL is pinned - set "
            "brain.base_url in agent.yaml (and ensure its ${VAR} resolves)."
        )
    wire_api = os.environ.get(GATEWAY_WIRE_API_ENV, "") or "chat"
    return [
        f"model_provider={_toml_string(_PROVIDER_ID)}",
        f"model_providers.{_PROVIDER_ID}.name={_toml_string('bobi gateway')}",
        f"model_providers.{_PROVIDER_ID}.base_url={_toml_string(base_url)}",
        f"model_providers.{_PROVIDER_ID}.env_key={_toml_string(_GATEWAY_API_KEY_ENV)}",
        f"model_providers.{_PROVIDER_ID}.wire_api={_toml_string(wire_api)}",
    ]
