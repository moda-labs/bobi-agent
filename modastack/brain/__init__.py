"""Pluggable agent brain — provider-agnostic session interface (epic #485).

The framework drives every agent through a *brain*: a client that connects,
takes queries, and streams back messages. Today the only brain is Claude Code
(``claude-agent-sdk``). This package is the seam that lets a team pick a
different agentic CLI (Codex, Gemini, Grok) without the runtime hardcoding any
one vendor SDK — see ``docs/specs/pluggable-brain.md``.

``base`` defines the provider-agnostic contract (the ``BrainSession`` /
``BrainFactory`` protocols + normalized stream messages); per-brain adapters
(``claude``) translate a vendor SDK/CLI into it. ``get_brain`` resolves a brain
kind to its factory; Phase 1 ships only ``claude``.
"""

from __future__ import annotations

from modastack.brain.base import (
    AssistantText,
    BrainCost,
    BrainFactory,
    BrainMessage,
    BrainSession,
    DeferredTool,
    StreamDelta,
    TurnResult,
)
from modastack.brain.claude import ClaudeBrain

# Registry of available brains by kind. Phase 1: claude only. Codex/Gemini/Grok
# adapters register here as they land (#485 phases 2–4).
_BRAINS: dict[str, BrainFactory] = {
    "claude": ClaudeBrain(),
}

DEFAULT_BRAIN = "claude"


def get_brain(kind: str | None = None) -> BrainFactory:
    """Resolve a brain kind to its factory (defaults to ``claude``).

    Raises ``ValueError`` for an unknown kind so a typo in ``agent.yaml``
    ``brain.kind`` fails loud at session construction rather than silently
    falling back.
    """
    name = kind or DEFAULT_BRAIN
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
    "DeferredTool",
    "StreamDelta",
    "TurnResult",
    "DEFAULT_BRAIN",
    "get_brain",
]
