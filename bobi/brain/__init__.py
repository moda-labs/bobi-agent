"""Pluggable agent brain — provider-agnostic session interface (epic #485).

The framework drives every agent through a *brain*: a client that connects,
takes queries, and streams back messages. Today the only brain is Claude Code
(``claude-agent-sdk``). This package is the seam that lets a team pick a
different agentic CLI (Codex, Gemini, Grok) without the runtime hardcoding any
one vendor SDK - see issue #485.

``base`` defines the provider-agnostic contract (the ``BrainSession`` /
``BrainFactory`` protocols + normalized stream messages); per-brain adapters
(``claude``) translate a vendor SDK/CLI into it. ``get_brain`` resolves a brain
kind to its factory; Phase 1 ships only ``claude``.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

from bobi.brain.base import (
    AssistantText,
    BrainCapabilities,
    BrainCost,
    BrainFactory,
    BrainMessage,
    BrainSession,
    DeferredTool,
    StreamDelta,
    TurnResult,
)
from bobi.brain.claude import ClaudeBrain
from bobi.brain.codex import CodexBrain
from bobi.brain.gateway import (
    GATEWAY_BASE_URL_ENV,
    GATEWAY_SMALL_MODEL_ENV,
    GatewayBrain,
)

# Registry of available brains by kind. Gemini/Grok adapters register here as
# they land (#485 phase 4).
_BRAINS: dict[str, BrainFactory] = {
    "claude": ClaudeBrain(),
    "codex": CodexBrain(),
    "gateway": GatewayBrain(),
}

DEFAULT_BRAIN = "claude"

# Env var carrying the active process brain kind. The process entrypoint seeds
# it from ``agent.yaml`` ``brain.kind`` (see ``set_process_brain``). Launched
# child agents get a stricter root-bound value from ``child_agent_env()`` so a
# stale ambient value from another installation cannot leak across sessions.
BRAIN_ENV = "BOBI_BRAIN"
_BRAIN_MODEL_ENV = "BOBI_BRAIN_MODEL"
# Compatibility for older external code that imported the constant directly.
# Bobi internals should use the helpers below so model env handling stays here.
BRAIN_MODEL_ENV = _BRAIN_MODEL_ENV


def get_process_brain_model(
    env: MutableMapping[str, str] | None = None,
) -> str:
    """Return the configured default model for the selected process brain."""
    lookup = os.environ if env is None else env
    return lookup.get(_BRAIN_MODEL_ENV, "")


def _set_process_brain_model(
    model: str | None,
    env: MutableMapping[str, str] | None = None,
) -> None:
    """Pin or clear the process brain model in *env*.

    Keeping the env var name private to this module prevents the model
    selection contract from being reimplemented across brain adapters and
    launch paths.
    """
    target = os.environ if env is None else env
    if model:
        target[_BRAIN_MODEL_ENV] = model
    else:
        target.pop(_BRAIN_MODEL_ENV, None)


def with_default_model_option(options: dict | None) -> dict:
    """Return *options* with the process default model filled in if absent."""
    extra = dict(options or {})
    if not extra.get("model"):
        model = get_process_brain_model()
        if model:
            extra["model"] = model
    return extra


def resolve_model_option(model: str | None) -> str:
    """Return an explicit model or the process default model."""
    return str(model or "") or get_process_brain_model()


def resolve_model(cfg, role: str | None = None, explicit: str | None = None) -> str:
    """Resolve the model for an agent launch (#617).

    Precedence: *explicit* (a launch flag or caller override) >
    ``roles.<role>.model`` from team config > the process default
    (``brain.model``, pinned as ``BOBI_BRAIN_MODEL``) > "" (the provider
    default). The role lookup is the only step ``resolve_model_option``
    does not already own, so everything else delegates to it.

    *cfg* is duck-typed (anything with ``role_model()``) so this module stays
    import-free of ``bobi.config``.
    """
    chosen = str(explicit or "")
    if not chosen and role and cfg is not None:
        chosen = cfg.role_model(role)
    return resolve_model_option(chosen)


def _pin_env(
    target: MutableMapping[str, str], key: str, value: str | None,
) -> None:
    """Set *key* in *target*, or clear it for an empty value."""
    if value:
        target[key] = value
    else:
        target.pop(key, None)


def pin_process_brain(
    kind: str | None,
    model: str | None,
    env: MutableMapping[str, str] | None = None,
    *,
    gateway_base_url: str = "",
    gateway_small_model: str = "",
) -> None:
    """Pin the process brain kind and model into *env*, clearing stale values.

    The gateway pins carry ``brain.base_url`` / ``brain.small_model`` for a
    ``kind: gateway`` team (#655); for any other kind they are cleared so a
    stale parent gateway endpoint never leaks into another team's sessions.
    """
    target = os.environ if env is None else env
    _pin_env(target, BRAIN_ENV, kind)
    _set_process_brain_model(model, env=target)
    is_gateway = kind == "gateway"
    _pin_env(target, GATEWAY_BASE_URL_ENV,
             gateway_base_url if is_gateway else "")
    _pin_env(target, GATEWAY_SMALL_MODEL_ENV,
             gateway_small_model if is_gateway else "")


def set_process_brain(
    kind: str | None,
    model: str | None = None,
    *,
    gateway_base_url: str = "",
    gateway_small_model: str = "",
) -> None:
    """Record the team's brain kind for the current process.

    A no-op for an empty/None kind (keeps the framework default). At top-level
    process startup, an explicit ``BOBI_BRAIN`` already in the environment
    is left untouched so an operator override can select the current process's
    brain. Detached child launches do not rely on this ambient inheritance:
    ``bobi.env.child_agent_env()`` rewrites the child's value from the
    verified installation root.
    """
    existing_kind = os.environ.get(BRAIN_ENV, "")
    if kind and not existing_kind:
        os.environ[BRAIN_ENV] = kind
        existing_kind = kind
    # The model and gateway pins only apply when the configured brain IS the
    # active one - a model-only config tunes the default brain, but neither it
    # nor a gateway endpoint may cross onto an operator-overridden brain.
    config_matches_active_brain = (
        (kind and existing_kind == kind)
        or (not kind and existing_kind in ("", DEFAULT_BRAIN))
    )
    if model and config_matches_active_brain and not get_process_brain_model():
        _set_process_brain_model(model)
    if kind == "gateway" and config_matches_active_brain:
        if gateway_base_url and GATEWAY_BASE_URL_ENV not in os.environ:
            os.environ[GATEWAY_BASE_URL_ENV] = gateway_base_url
        if gateway_small_model and GATEWAY_SMALL_MODEL_ENV not in os.environ:
            os.environ[GATEWAY_SMALL_MODEL_ENV] = gateway_small_model


def continuation_token(
    brain: BrainFactory,
    *,
    session_id: str,
    from_model: str,
    to_model: str,
) -> str:
    """The resume token for continuing *session_id* under *to_model*, or "".

    The single place that decides continue-vs-fresh for every resume site
    (#642): the workflow orchestrator's resume and mid-run model switches, and
    ``load_resumable_session_id`` for subagents. Same model always continues;
    a cross-model continuation requires the brain's ``cross_model_resume``
    capability AND a concrete target model - resuming "onto the provider
    default" cannot be expressed to the CLI (no --model to pass), so the
    session would silently keep its old model while the record says default.
    An empty *session_id* never continues. ``""`` as a model means "the
    provider default" and is a real value for mismatch purposes.

    An empty return means the caller must start fresh and re-inject whatever
    context it can reconstruct.
    """
    if not session_id:
        return ""
    if (from_model or "") == (to_model or ""):
        return session_id
    if not to_model:
        return ""
    caps = getattr(brain, "capabilities", None)
    if caps is not None and getattr(caps, "cross_model_resume", False):
        return session_id
    return ""


def get_brain(kind: str | None = None) -> BrainFactory:
    """Resolve a brain kind to its factory.

    Precedence inside the current process: explicit ``kind`` arg →
    ``BOBI_BRAIN`` env → ``claude``. For launched child agents,
    ``BOBI_BRAIN`` is prepared by ``child_agent_env()`` from the verified
    installation root, not blindly inherited from the parent process. Raises
    ``ValueError`` for an unknown kind so a typo in ``agent.yaml`` ``brain.kind``
    fails loud at session construction rather than silently falling back.
    """
    name = kind or os.environ.get(BRAIN_ENV) or DEFAULT_BRAIN
    try:
        return _BRAINS[name]
    except KeyError:
        known = ", ".join(sorted(_BRAINS))
        raise ValueError(
            f"unknown brain kind {name!r} (known: {known})"
        ) from None


__all__ = [
    "AssistantText",
    "BrainCapabilities",
    "BrainCost",
    "BrainFactory",
    "BrainMessage",
    "BrainSession",
    "ClaudeBrain",
    "CodexBrain",
    "GatewayBrain",
    "DeferredTool",
    "StreamDelta",
    "TurnResult",
    "DEFAULT_BRAIN",
    "BRAIN_ENV",
    "GATEWAY_BASE_URL_ENV",
    "GATEWAY_SMALL_MODEL_ENV",
    "continuation_token",
    "get_brain",
    "get_process_brain_model",
    "pin_process_brain",
    "resolve_model",
    "resolve_model_option",
    "set_process_brain",
    "with_default_model_option",
]
