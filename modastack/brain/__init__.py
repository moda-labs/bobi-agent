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

import os

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
from modastack.brain.codex import CodexBrain

# Registry of available brains by kind. Gemini/Grok adapters register here as
# they land (#485 phase 4).
_BRAINS: dict[str, BrainFactory] = {
    "claude": ClaudeBrain(),
    "codex": CodexBrain(),
}

DEFAULT_BRAIN = "claude"

# Env var carrying the team's configured brain kind. Set once at the agent
# process entry from ``agent.yaml`` ``brain.kind`` (see ``set_process_brain``)
# so it propagates to subprocess agents — the same pattern as ``MODASTACK_AUTH``.
BRAIN_ENV = "MODASTACK_BRAIN"


def set_process_brain(kind: str | None) -> None:
    """Record the team's brain kind for this process tree (and its children).

    A no-op for an empty/None kind (keeps the framework default). An explicit
    ``MODASTACK_BRAIN`` already in the environment is left untouched so an
    operator override wins over agent.yaml.
    """
    if kind and not os.environ.get(BRAIN_ENV):
        os.environ[BRAIN_ENV] = kind


def get_brain(kind: str | None = None) -> BrainFactory:
    """Resolve a brain kind to its factory.

    Precedence: explicit ``kind`` arg → ``MODASTACK_BRAIN`` env (the team's
    configured brain) → ``claude``. Raises ``ValueError`` for an unknown kind so
    a typo in ``agent.yaml`` ``brain.kind`` fails loud at session construction
    rather than silently falling back.
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
    "get_brain",
    "set_process_brain",
]
