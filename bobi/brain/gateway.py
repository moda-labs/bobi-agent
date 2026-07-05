"""Anthropic-compatible gateway brain (#655, epic #548).

Points the Claude CLI at an Anthropic-compatible endpoint (LiteLLM, Ollama's
Anthropic-compat API, any ``/v1/messages`` proxy) so a team can run on local
or self-hosted models. The session machinery is ClaudeBrain's - same SDK,
same CLI, same message normalization; the only difference is the per-session
environment: ``ANTHROPIC_BASE_URL`` (from ``brain.base_url``), the small/fast
model default, and never letting an ambient real ``ANTHROPIC_API_KEY`` reach
the gateway.

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
    never be sent to a gateway. The main model rides the explicit ``--model``
    chain (``resolve_model`` / ``with_default_model_option``), which stays
    authoritative; only the small/fast model needs an env default so the CLI's
    background calls never reference a Claude alias the gateway doesn't serve.

    Raises when the gateway brain is active without a pinned base URL - the
    session would otherwise silently dial real Anthropic carrying the
    gateway's credentials. ``validate_config`` catches the config-file case;
    this catches env-pin gaps (an operator ``BOBI_BRAIN=gateway`` override, a
    ``${VAR}`` that resolved empty at spawn).
    """
    from bobi.brain import get_process_brain_model

    base_url = os.environ.get(GATEWAY_BASE_URL_ENV, "")
    if not base_url:
        raise RuntimeError(
            "gateway brain selected but no base URL is pinned - set "
            "brain.base_url in agent.yaml (and ensure its ${VAR} resolves)."
        )
    env = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": base_url,
    }
    small = (os.environ.get(GATEWAY_SMALL_MODEL_ENV, "")
             or get_process_brain_model())
    if small:
        env["ANTHROPIC_SMALL_FAST_MODEL"] = small
    return env


def _with_gateway_env(options: dict | None) -> dict:
    """Return *options* with the gateway env merged OVER any caller ``env``.

    The gateway values must win: callers pass full environment copies here
    (the MCP preflight probe passes ``agent_spawn_env()``), and a real
    ``ANTHROPIC_API_KEY`` or stale ``ANTHROPIC_BASE_URL`` in that copy must
    not defeat the routing or the key blank.
    """
    extra = dict(options or {})
    extra["env"] = {**(extra.get("env") or {}), **_gateway_session_env()}
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
        self, *, options: dict | None = None, **kwargs: Any,
    ) -> BrainSession:
        return super().make_session(options=_with_gateway_env(options), **kwargs)

    def stream_once(
        self, *, options: dict | None = None, **kwargs: Any,
    ) -> AsyncIterator[BrainMessage]:
        return super().stream_once(options=_with_gateway_env(options), **kwargs)
