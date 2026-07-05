"""Anthropic-compatible gateway brain (#655, epic #548).

Points the Claude CLI at an Anthropic-compatible endpoint (LiteLLM, Ollama's
Anthropic-compat API, any ``/v1/messages`` proxy) so a team can run on local
or self-hosted models. The session machinery is ClaudeBrain's - same SDK,
same CLI, same message normalization; the only difference is the per-session
environment: ``ANTHROPIC_BASE_URL`` (from ``brain.base_url``), the model
defaults, and never letting an ambient real ``ANTHROPIC_API_KEY`` reach the
gateway.

Gateway auth is ``ANTHROPIC_AUTH_TOKEN`` only, sourced from the runtime
``.env`` or the parent environment and inherited by the CLI subprocess
untouched. Ollama needs none; LiteLLM typically wants its master key.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

from bobi.brain.base import BrainCapabilities, BrainMessage, BrainSession
from bobi.brain.claude import ClaudeBrain

# Process pins mirroring BOBI_BRAIN / BOBI_BRAIN_MODEL: seeded from agent.yaml
# ``brain.base_url`` / ``brain.small_model`` by the same preparation sites
# (``bobi.env.pin_brain_from_root`` for spawned agents, ``set_process_brain``
# at process startup) so a stale parent value never leaks across installs.
GATEWAY_BASE_URL_ENV = "BOBI_GATEWAY_BASE_URL"
GATEWAY_SMALL_MODEL_ENV = "BOBI_GATEWAY_SMALL_MODEL"


def _gateway_session_env() -> dict[str, str]:
    """The ANTHROPIC_* overrides for one gateway session.

    ``ANTHROPIC_API_KEY`` is force-blanked: an ambient real Anthropic key must
    never be sent to a gateway. ``ANTHROPIC_MODEL`` / ``ANTHROPIC_SMALL_FAST_MODEL``
    are CLI defaults only - the explicit ``--model`` chain (``resolve_model``)
    stays authoritative and unchanged.
    """
    from bobi.brain import get_process_brain_model

    env = {"ANTHROPIC_API_KEY": ""}
    base_url = os.environ.get(GATEWAY_BASE_URL_ENV, "")
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    model = get_process_brain_model()
    if model:
        env["ANTHROPIC_MODEL"] = model
    small = os.environ.get(GATEWAY_SMALL_MODEL_ENV, "") or model
    if small:
        env["ANTHROPIC_SMALL_FAST_MODEL"] = small
    return env


def _with_gateway_env(options: dict | None) -> dict:
    """Return *options* with the gateway env merged under any caller ``env``."""
    extra = dict(options or {})
    extra["env"] = {**_gateway_session_env(), **(extra.get("env") or {})}
    return extra


class GatewayBrain(ClaudeBrain):
    """Factory for Claude CLI sessions against an Anthropic-compatible gateway."""

    name = "gateway"
    provider = "gateway"
    # Whether an Anthropic-compat backend honors --resume with a different
    # --model is backend-dependent; ship conservative (fresh+reinject on model
    # switches) and flip only after a live verification, the #649 arc.
    capabilities = BrainCapabilities(cross_model_resume=False)

    def make_session(
        self,
        *,
        cwd: str | None,
        system_prompt: Any,
        resume: str | None = None,
        options: dict | None = None,
    ) -> BrainSession:
        return super().make_session(
            cwd=cwd,
            system_prompt=system_prompt,
            resume=resume,
            options=_with_gateway_env(options),
        )

    def stream_once(
        self,
        *,
        system_prompt: Any,
        user_prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        options: dict | None = None,
    ) -> AsyncIterator[BrainMessage]:
        return super().stream_once(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            cwd=cwd,
            options=_with_gateway_env(options),
        )
