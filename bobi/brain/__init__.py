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

from bobi.brain.base import (
    AssistantText,
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

# Registry of available brains by kind. Gemini/Grok adapters register here as
# they land (#485 phase 4).
_BRAINS: dict[str, BrainFactory] = {
    "claude": ClaudeBrain(),
    "codex": CodexBrain(),
}

DEFAULT_BRAIN = "claude"

# Env var carrying the active process brain kind. The process entrypoint seeds
# it from ``agent.yaml`` ``brain.kind`` (see ``set_process_brain``). Launched
# child agents get a stricter root-bound value from ``child_agent_env()`` so a
# stale ambient value from another installation cannot leak across sessions.
BRAIN_ENV = "BOBI_BRAIN"
BRAIN_MODEL_ENV = "BOBI_BRAIN_MODEL"


def set_process_brain(kind: str | None, model: str | None = None) -> None:
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
    model_matches_active_brain = (
        (kind and existing_kind == kind)
        or (not kind and existing_kind in ("", DEFAULT_BRAIN))
    )
    if model and model_matches_active_brain and not os.environ.get(BRAIN_MODEL_ENV):
        os.environ[BRAIN_MODEL_ENV] = model


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
    "BrainCost",
    "BrainFactory",
    "BrainMessage",
    "BrainSession",
    "ClaudeBrain",
    "CodexBrain",
    "DeferredTool",
    "StreamDelta",
    "TurnResult",
    "DEFAULT_BRAIN",
    "BRAIN_ENV",
    "BRAIN_MODEL_ENV",
    "get_brain",
    "set_process_brain",
]
