"""Anthropic-compatible gateway endpoint support for the claude engine (#655, #789).

Gateway mode points the Claude CLI at an Anthropic-compatible endpoint
(LiteLLM, Ollama's Anthropic-compat API, any ``/v1/messages`` proxy) so a team
can run on local or self-hosted models. It is endpoint CONFIG on the claude
engine (``kind: claude`` + ``brain.base_url``), not a separate brain: the
session machinery is ClaudeBrain's - same SDK, same CLI, same message
normalization; the only difference is the per-session environment merged in by
``ClaudeBrain`` when the base-url pin is set: ``ANTHROPIC_BASE_URL`` (from
``brain.base_url``), the small/fast model default, and never letting an
ambient real ``ANTHROPIC_API_KEY`` reach the gateway.

Gateway auth is ``ANTHROPIC_AUTH_TOKEN`` only, sourced from the runtime
``.env`` or the parent environment and inherited by the CLI subprocess
untouched. Ollama needs none; LiteLLM typically wants its master key.

``kind: gateway`` remains an accepted alias for this configuration
(``BRAIN_KIND_ALIASES``); ``GatewayBrain`` below is the matching import alias.
"""

from __future__ import annotations

import os

from bobi.brain.claude import ClaudeBrain

# Process pins mirroring BOBI_BRAIN / BOBI_BRAIN_MODEL: seeded from agent.yaml
# ``brain.base_url`` / ``brain.small_model`` by the same preparation sites
# (``bobi.env.pin_brain_from_root`` for spawned agents, ``set_process_brain``
# at process startup) so a stale parent value never leaks across installs.
GATEWAY_BASE_URL_ENV = "BOBI_GATEWAY_BASE_URL"
GATEWAY_SMALL_MODEL_ENV = "BOBI_GATEWAY_SMALL_MODEL"

# Deprecated: gateway mode no longer has its own factory class (#789). Kept so
# `from bobi.brain import GatewayBrain` keeps resolving for external code.
GatewayBrain = ClaudeBrain


def gateway_base_url() -> str:
    """The pinned gateway base URL, or "" when the team runs natively.

    The single runtime signal for gateway mode: both engines consult it at
    session construction, and the pin sites guarantee it is set exactly for
    claude/codex teams with a configured ``brain.base_url``.
    """
    return os.environ.get(GATEWAY_BASE_URL_ENV, "")


def _gateway_session_env() -> dict[str, str]:
    """The ANTHROPIC_* overrides for one gateway session.

    ``ANTHROPIC_API_KEY`` is force-blanked: an ambient real Anthropic key must
    never be sent to a gateway. The main model rides the explicit ``--model``
    chain (``resolve_model`` / ``with_default_model_option``), which stays
    authoritative; only the small/fast model needs an env default so the CLI's
    background calls never reference a Claude alias the gateway doesn't serve.

    Raises when called without a pinned base URL - the session would otherwise
    silently dial real Anthropic carrying the gateway's credentials.
    ``validate_config`` catches the config-file case; the pin sites
    (``_require_declared_gateway_url``) and the ambient-alias guard in
    ``get_brain`` catch env-pin gaps before sessions get here.
    """
    from bobi.brain import get_process_brain_model

    base_url = gateway_base_url()
    if not base_url:
        raise RuntimeError(
            "gateway session requested but no base URL is pinned - set "
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


def with_gateway_env(options: dict | None) -> dict:
    """Return *options* with the gateway env merged OVER any caller ``env``.

    The gateway values must win: callers pass full environment copies here
    (the MCP preflight probe passes ``agent_spawn_env()``), and a real
    ``ANTHROPIC_API_KEY`` or stale ``ANTHROPIC_BASE_URL`` in that copy must
    not defeat the routing or the key blank.
    """
    extra = dict(options or {})
    extra["env"] = {**(extra.get("env") or {}), **_gateway_session_env()}
    return extra
