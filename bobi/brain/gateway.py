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

This module also hosts the pieces BOTH engines share: the base-url pin
helpers and the :class:`GatewayAwareEngine` mixin that flips ``provider`` /
``capabilities`` on the pin. ``kind: gateway`` remains an accepted alias for
the claude-engine configuration (``BRAIN_KIND_ALIASES``); ``GatewayBrain`` is
the matching import alias.
"""

from __future__ import annotations

import os

from bobi.brain.base import BrainCapabilities

# Process pins mirroring BOBI_BRAIN / BOBI_BRAIN_MODEL: seeded from agent.yaml
# ``brain.base_url`` / ``brain.small_model`` by the same preparation sites
# (``bobi.env.pin_brain_from_root`` for spawned agents, ``set_process_brain``
# at process startup) so a stale parent value never leaks across installs.
GATEWAY_BASE_URL_ENV = "BOBI_GATEWAY_BASE_URL"
GATEWAY_SMALL_MODEL_ENV = "BOBI_GATEWAY_SMALL_MODEL"


def __getattr__(name: str):
    # Deprecated: gateway mode no longer has its own factory class (#789).
    # Resolved lazily so this module needn't import the engine at load time
    # (the engines import THIS module at the top of their files).
    if name == "GatewayBrain":
        from bobi.brain.claude import ClaudeBrain

        return ClaudeBrain
    raise AttributeError(name)


def gateway_base_url() -> str:
    """The pinned gateway base URL, or "" when the team runs natively.

    The single runtime signal for gateway mode: both engines consult it at
    session construction, and the pin sites guarantee it is set exactly for
    claude/codex teams with a configured ``brain.base_url``.
    """
    return os.environ.get(GATEWAY_BASE_URL_ENV, "")


def require_gateway_base_url() -> str:
    """The pinned gateway base URL, raising the actionable config error
    when it is missing or is the declared-but-unresolved sentinel.

    Both engines call this at gateway-session construction, so an operator
    whose ``${VAR}`` stopped resolving sees "set brain.base_url in agent.yaml"
    instead of DNS noise from the sentinel host. ``validate`` catches the
    config-file case; the pin sites and ``get_brain``'s alias guard catch
    env-pin gaps before sessions get here.
    """
    from bobi.brain import GATEWAY_UNRESOLVED_BASE_URL

    base_url = gateway_base_url()
    if not base_url or base_url == GATEWAY_UNRESOLVED_BASE_URL:
        raise RuntimeError(
            "gateway session requested but no base URL is pinned - set "
            "brain.base_url in agent.yaml (and ensure its ${VAR} resolves)."
        )
    return base_url


class GatewayAwareEngine:
    """Mixin for engine factories: gateway mode flips the session surface.

    When the base-url pin is set, cost attribution moves to the ``gateway``
    provider (gateway spend must never blend into real vendor spend) and
    cross-model resume drops to the conservative fresh+reinject path -
    whether an arbitrary backend honors a resume under a different model is
    backend-dependent; flip only after live verification, the #649 arc.
    Effort vocabulary stays the engine's own in both modes: the engine CLI
    is what parses the value.
    """

    native_provider: str
    _EFFORTS: frozenset = frozenset()

    @property
    def provider(self) -> str:
        return "gateway" if gateway_base_url() else self.native_provider

    @property
    def capabilities(self) -> BrainCapabilities:
        return BrainCapabilities(
            cross_model_resume=not gateway_base_url(),
            efforts=self._EFFORTS,
        )


def _gateway_session_env() -> dict[str, str]:
    """The ANTHROPIC_* overrides for one claude-engine gateway session.

    ``ANTHROPIC_API_KEY`` is force-blanked: an ambient real Anthropic key must
    never be sent to a gateway. The main model rides the explicit ``--model``
    chain (``resolve_model`` / ``with_default_model_option``), which stays
    authoritative; only the small/fast model needs an env default so the CLI's
    background calls never reference a Claude alias the gateway doesn't serve.
    """
    from bobi.brain import get_process_brain_model

    env = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": require_gateway_base_url(),
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
