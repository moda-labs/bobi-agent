"""Provider-agnostic brain contract (epic #485, Phase 1).

These types are the normalized boundary every brain adapter speaks. Call sites
(``session.py``, ``subagent.py``, ``workflow/orchestrator.py``, …) consume only
these — never a vendor SDK's message classes — so swapping the underlying CLI
is an adapter change, not a runtime change.

The field set is the union of what the call sites actually read off the SDK
today, so the Claude adapter is a faithful 1:1 translation and the migration is
behavior-preserving. ``usage`` stays a raw token dict (not a typed object) so
the context-fill math in ``session.py`` is identical across brains.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Union, runtime_checkable


@dataclass
class BrainCost:
    """One model's token usage within a turn, for cost attribution.

    Normalized from the SDK's per-model usage breakdown. The dollar figure is
    carried on :class:`TurnResult` (``total_cost_usd``) because only some brains
    report cost directly; others compute it from these token counts.

    ``cached_input_tokens`` is a SUBSET of ``input_tokens`` (codex's
    ``non_cached_input()`` is ``input - cached``), not an addition to it.
    Cache reads bill at a steep per-model discount, so folding the split away
    would make any downstream dollar estimate a large overestimate on
    cache-heavy agentic turns (#760).
    """

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass
class DeferredTool:
    """A tool call the brain suspended for out-of-band resolution.

    Mirrors the SDK's ``deferred_tool_use`` (e.g. an ``AskUserQuestion`` routed
    to a human). ``name``/``input`` are the tool name and its arguments.
    """

    name: str
    input: Any = None


# --- normalized stream messages -------------------------------------------


@dataclass
class AssistantText:
    """One assistant turn's text plus its representative per-call usage.

    ``usage`` is the raw token dict for the single API call this message
    represents (``input_tokens`` / ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens`` / …) — the rotation metric reads it
    directly, so it stays an opaque dict, not a typed object.
    """

    text: str = ""
    usage: dict | None = None


@dataclass
class TurnResult:
    """End-of-turn result: resume token, error state, cost/usage, deferrals."""

    session_id: str = ""
    is_error: bool = False
    error_kind: str = ""
    error_message: str = ""
    max_turns: int | None = None
    turn_count: int | None = None
    api_error_status: int | None = None
    total_cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    result_text: str = ""
    deferred_tool: DeferredTool | None = None
    costs: list[BrainCost] = field(default_factory=list)

    def error_text(self) -> str:
        """The honest error string for this turn, or "" when it succeeded."""
        return turn_error_text(
            is_error=self.is_error,
            error_kind=self.error_kind,
            error_message=self.error_message,
            result_text=self.result_text,
            api_error_status=self.api_error_status,
        )


# Provider-neutral cause emitted when the brain cannot authenticate.
ERROR_KIND_AUTHENTICATION = "authentication_failed"

# The brain-neutral error kind for a turn the harness cut off at its turn cap.
# One constant so the cap's terminal handling (resume, retry-vs-give-up) keys
# off the same string the adapters set.
ERROR_KIND_MAX_TURNS = "max_turns_reached"


def turn_error_text(
    *,
    is_error: bool,
    error_kind: str = "",
    error_message: str = "",
    result_text: str = "",
    api_error_status: int | None = None,
) -> str:
    """Compose the honest error string for a failed turn (#845).

    The ONE composition every call site shares (``TurnResult.error_text``,
    ``session.py``, ``subagent.py``, the workflow drain), so no path can
    reinvent a friendlier-looking lie.

    Order matters. ``error_message`` carries the brain's own diagnosis (e.g.
    ``max_turns_reached (max=1000, turns=1001)``) and is the only field
    populated on exactly the terminal paths that need naming; ``result_text``
    is EMPTY there, which is how a turn-cap kill used to reach operators as the
    literal fallback ``"turn failed"`` with the real cause discarded. The last
    resort still names the kind and API status rather than a bare literal, so
    an unrecognized failure is at least self-describing.

    A set ``error_kind`` counts as a failure even when ``is_error`` is False -
    the same rule the adapters use to derive ``is_error`` - so a brain that
    classifies a terminal condition without raising the flag still gets named.
    """
    if not (is_error or error_kind):
        return ""
    if error_message and result_text:
        return f"{error_message}: {result_text}"
    return (
        error_message
        or result_text
        or f"turn failed (kind={error_kind or 'unknown'}, "
           f"api_status={api_error_status})"
    )


@dataclass
class StreamDelta:
    """A token-level partial from the one-shot streaming path."""

    text: str = ""


BrainMessage = Union[AssistantText, TurnResult, StreamDelta]


# --- capabilities -----------------------------------------------------------


@dataclass(frozen=True)
class BrainCapabilities:
    """Static capability advertisement for one brain kind (#642).

    ``cross_model_resume``: the brain can resume an existing session under a
    different model, keeping the full transcript. When False, callers that
    need to continue context on another model must start a fresh session and
    re-inject whatever context they can reconstruct.

    ``efforts``: the reasoning-effort values this brain's CLI accepts (#778).
    Empty means "unknown" - validation falls back to the cross-vendor union
    instead of warning on everything. Advisory only: effort stays
    pass-through at session construction, so a stale set here can never
    block a value the vendor actually accepts.
    """

    cross_model_resume: bool = False
    efforts: frozenset = frozenset()


# --- the session + factory protocols --------------------------------------


@runtime_checkable
class BrainSession(Protocol):
    """A live agent session: connect, query, stream normalized messages.

    The persistent-client shape every brain adapter implements. ``provider`` is
    the cost-attribution label (e.g. ``"anthropic"``, ``"openai"``).
    """

    provider: str

    async def connect(self) -> None:
        """Open the session. Pure setup — never a turn.

        connect() deliberately takes no prompt (#1016): a connect-time prompt
        was an executable agent turn that no driver owned, which is how a
        workflow's dispatch text ran before step 0 and how a reconnect once
        drained a turn that did not exist (#799). All text enters a session
        through ``query()``, as an explicit turn.
        """

    async def query(self, text: str) -> None:
        """Send a message into the live session."""

    def receive_response(self) -> AsyncIterator[BrainMessage]:
        """Async-iterate one turn's normalized messages until its result."""

    async def disconnect(self) -> None:
        """Tear the session down (idempotent-friendly)."""

    def abort(self) -> None:
        """Synchronously force-stop resources when async cleanup is wedged."""


class BrainFactory(Protocol):
    """Builds :class:`BrainSession` instances for one brain kind."""

    name: str
    provider: str
    capabilities: BrainCapabilities

    def make_session(
        self,
        *,
        cwd: str | None,
        system_prompt: Any,
        resume: str | None = None,
        options: dict | None = None,
    ) -> BrainSession:
        """Construct a session.

        ``system_prompt`` is passed opaquely (Phase 1 keeps the call sites'
        existing system-prompt shape; normalizing it to a plain string is
        Phase 2 work). ``options`` carries brain-specific extras (the kwargs
        the call site used to hand the SDK: ``max_turns``, ``skills``,
        ``hooks``, ``mcp_servers``, …).
        """

    def stream_once(
        self,
        *,
        system_prompt: Any,
        user_prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        options: dict | None = None,
    ) -> AsyncIterator["BrainMessage"]:
        """One-shot streaming completion — no session, no resume.

        The stateless setup/digestion transport (``bobi.setup.llm``) calls this
        on whatever brain the process is bound to, so it is part of the factory
        contract, not a Claude extra. Declared here because it was a de facto
        requirement for years while only some factories implemented it: a brain
        without it failed with ``AttributeError`` at the call site instead of
        saying which brain lacked the capability. A factory that cannot serve
        the path raises :class:`NotImplementedError` naming itself.
        """
