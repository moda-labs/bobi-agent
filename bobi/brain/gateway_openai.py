"""OpenAI-compatible gateway brain backed by the Codex CLI (#777).

Codex natively supports custom OpenAI-compatible providers via per-invocation
``-c`` overrides. This brain keeps the normal Codex session machinery but pins
one provider named ``bobi_gateway`` for every fresh and resumed turn, so no
process-wide ``~/.codex/config.toml`` provider state is required.
"""

from __future__ import annotations

import json
import os
from typing import Any

from bobi.brain.base import BrainCapabilities, BrainSession
from bobi.brain.codex import CodexBrain, _CodexSession, _instructions

GATEWAY_WIRE_API_ENV = "BOBI_GATEWAY_WIRE_API"
_PROVIDER_ID = "bobi_gateway"
_GATEWAY_API_KEY_ENV = "BOBI_GATEWAY_API_KEY"


def _toml_string(value: str) -> str:
    """Return *value* quoted for a Codex ``-c key=value`` TOML override."""
    return json.dumps(value)


def _gateway_openai_overrides() -> list[str]:
    """Provider overrides for one gateway-openai Codex invocation."""
    from bobi.brain import GATEWAY_BASE_URL_ENV

    base_url = os.environ.get(GATEWAY_BASE_URL_ENV, "")
    if not base_url:
        raise RuntimeError(
            "gateway-openai brain selected but no base URL is pinned - set "
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


class GatewayOpenAIBrain(CodexBrain):
    """Factory for Codex sessions against an OpenAI-compatible gateway."""

    name = "gateway-openai"
    provider = "gateway"
    capabilities = BrainCapabilities(cross_model_resume=False)

    def make_session(
        self,
        *,
        cwd: str | None,
        system_prompt: Any,
        resume: str | None = None,
        options: dict | None = None,
    ) -> BrainSession:
        from bobi.brain import resolve_model_option
        from bobi.brain.codex_config import (
            codex_home, config_has_managed_block, write_codex_config,
        )

        opts = options or {}
        model = resolve_model_option(str(opts.get("model", "") or ""))
        mcp_servers = opts.get("mcp_servers") or {}
        home = codex_home()
        if mcp_servers or config_has_managed_block(home):
            write_codex_config(mcp_servers, home)
        return _CodexSession(
            cwd=cwd or ".",
            instructions=_instructions(system_prompt),
            resume=resume,
            model=model,
            mcp_servers=mcp_servers,
            mcp_env=opts.get("env") if isinstance(opts.get("env"), dict) else None,
            config_overrides=_gateway_openai_overrides(),
            provider=self.provider,
        )
