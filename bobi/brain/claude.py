"""Claude Code brain adapter (epic #485, Phase 1).

Wraps ``claude-agent-sdk`` behind the provider-agnostic :mod:`bobi.brain`
contract. This is a *behavior-preserving* translation: it builds the same
``ClaudeAgentOptions`` the call sites built inline, drives the same
``ClaudeSDKClient`` lifecycle, and converts the SDK's ``AssistantMessage`` /
``ResultMessage`` into normalized :class:`~bobi.brain.base.AssistantText` /
:class:`~bobi.brain.base.TurnResult`.

All ``claude_agent_sdk`` imports are deliberately lazy (inside methods) so the
heavy SDK import stays off the framework's import path and so tests that
monkeypatch ``claude_agent_sdk.ClaudeSDKClient`` continue to take effect.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator

from bobi.brain.base import (
    AssistantText,
    BrainCost,
    BrainMessage,
    BrainSession,
    DeferredTool,
    StreamDelta,
    TurnResult,
)

log = logging.getLogger(__name__)

DEFAULT_INITIALIZE_TIMEOUT_MS = 180_000
DEFAULT_CONNECT_ATTEMPTS = 3
DEFAULT_CONNECT_BACKOFF_SECONDS = 2.0


def _delta_text(event: Any) -> str:
    """Pull the text out of one raw Anthropic streaming event, or ''.

    The canonical home for the vendor-specific partial-stream shape
    (``content_block_delta`` / ``text_delta``); ``setup.llm`` re-exports it.
    """
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text", "")
    return ""


class _ClaudeSession:
    """A :class:`BrainSession` backed by one ``ClaudeSDKClient``."""

    provider = "anthropic"

    def __init__(self, options: Any) -> None:
        self._options = options
        self._client = self._new_client()

    def _new_client(self) -> Any:
        from claude_agent_sdk import ClaudeSDKClient

        return ClaudeSDKClient(self._options)

    async def connect(self, prompt: str | None = None) -> None:
        _configure_initialize_timeout()
        attempts = _env_int("BOBI_CLAUDE_CONNECT_ATTEMPTS", DEFAULT_CONNECT_ATTEMPTS)
        backoff = _env_float(
            "BOBI_CLAUDE_CONNECT_BACKOFF_SECONDS",
            DEFAULT_CONNECT_BACKOFF_SECONDS,
        )

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._client = self._new_client()
            try:
                await self._connect_once(prompt)
                return
            except Exception as exc:
                last_error = exc
                should_retry = attempt < attempts and _is_initialize_timeout(exc)
                if not should_retry:
                    raise
                try:
                    await self._client.disconnect()
                except Exception:
                    log.debug("Claude connect cleanup failed", exc_info=True)
                log.warning(
                    "Claude initialize timed out during connect; retrying "
                    "(attempt %s/%s)",
                    attempt + 1,
                    attempts,
                )
                if backoff > 0:
                    await asyncio.sleep(backoff * attempt)

        if last_error is not None:
            raise last_error

    async def _connect_once(self, prompt: str | None = None) -> None:
        # Match the historical call shape: a bare connect() when there is no
        # connect-prompt (the SDK defaults prompt to None), an explicit
        # connect(prompt) otherwise. Keeps no-arg fakes/clients working.
        if prompt is None:
            await self._client.connect()
        else:
            await self._client.connect(prompt)

    async def query(self, text: str) -> None:
        await self._client.query(text)

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def get_mcp_status(self) -> dict:
        """Passthrough to the SDK's MCP status probe (preflight only).

        Not part of the BrainSession protocol — an optional capability the MCP
        preflight uses; brains without an equivalent simply won't offer it.
        """
        return await self._client.get_mcp_status()

    async def receive_response(self) -> AsyncIterator[BrainMessage]:
        """Translate one turn's SDK messages into normalized brain messages."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                text_parts = [
                    b.text for b in msg.content if isinstance(b, TextBlock)
                ]
                yield AssistantText(
                    text="\n".join(text_parts) if text_parts else "",
                    usage=getattr(msg, "usage", None),
                )
            elif isinstance(msg, ResultMessage):
                yield _result_to_turn(msg)
            # Other SDK message types carry no signal the call sites consume.


def _result_to_turn(msg: Any) -> TurnResult:
    """Normalize an SDK ``ResultMessage`` into a :class:`TurnResult`."""
    costs: list[BrainCost] = []
    model_usage = getattr(msg, "model_usage", None)
    # NOTE (#485 follow-up): this mirrors the legacy session.py handling exactly
    # — it reads each element via getattr(model/input_tokens/output_tokens). The
    # SDK actually types model_usage as ``dict[str, Any]`` (model -> usage), so a
    # real dict is wrapped as a single element whose getattr lookups miss, giving
    # an empty/zero breakdown. Preserved verbatim for Phase 1 (zero behavior
    # change); fixing the dict shape is a tracked follow-up, not done here.
    if model_usage:
        for m in model_usage if isinstance(model_usage, list) else [model_usage]:
            costs.append(
                BrainCost(
                    model=getattr(m, "model", "") or "",
                    input_tokens=getattr(m, "input_tokens", 0) or 0,
                    output_tokens=getattr(m, "output_tokens", 0) or 0,
                )
            )

    deferred = None
    dtu = getattr(msg, "deferred_tool_use", None)
    if dtu is not None:
        deferred = DeferredTool(
            name=getattr(dtu, "name", ""), input=getattr(dtu, "input", None)
        )

    return TurnResult(
        session_id=getattr(msg, "session_id", "") or "",
        is_error=bool(getattr(msg, "is_error", False)),
        api_error_status=getattr(msg, "api_error_status", None),
        total_cost_usd=getattr(msg, "total_cost_usd", 0.0) or 0.0,
        duration_ms=getattr(msg, "duration_ms", 0) or 0,
        num_turns=getattr(msg, "num_turns", 0) or 0,
        result_text=getattr(msg, "result", "") or "",
        deferred_tool=deferred,
        costs=costs,
    )


def _configure_initialize_timeout() -> None:
    """Raise the SDK initialize deadline unless the operator set it already.

    The Claude SDK reads ``CLAUDE_CODE_STREAM_CLOSE_TIMEOUT`` during
    ``connect()`` and uses it as the initialize control-request timeout. Keep
    that public SDK knob authoritative, while giving Bobi a clearer alias.
    """
    if os.environ.get("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"):
        return
    timeout_ms = _env_int(
        "BOBI_CLAUDE_INITIALIZE_TIMEOUT_MS",
        DEFAULT_INITIALIZE_TIMEOUT_MS,
    )
    os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = str(timeout_ms)


def _is_initialize_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return "control request timeout" in text and "initialize" in text


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    return max(value, 1)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    return max(value, 0.0)


class ClaudeBrain:
    """Factory for Claude Code sessions (the default brain)."""

    name = "claude"
    provider = "anthropic"

    def make_session(
        self,
        *,
        cwd: str | None,
        system_prompt: Any,
        resume: str | None = None,
        options: dict | None = None,
    ) -> BrainSession:
        from claude_agent_sdk import ClaudeAgentOptions

        from bobi.sdk import get_cli_path

        from bobi.brain import with_default_model_option

        extra = with_default_model_option(options)
        # Defaults every call site shared; an explicit value in ``options`` wins.
        extra.setdefault("permission_mode", "bypassPermissions")
        kwargs = dict(cwd=cwd, cli_path=get_cli_path(), resume=resume, **extra)
        # Only pass system_prompt when the caller set one — the MCP probe builds
        # a session with no prompt, and forcing system_prompt=None would override
        # the SDK's own default.
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        return _ClaudeSession(ClaudeAgentOptions(**kwargs))

    async def stream_once(
        self,
        *,
        system_prompt: Any,
        user_prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        options: dict | None = None,
    ) -> AsyncIterator[BrainMessage]:
        """One-shot streaming completion (the stateless setup/digestion path).

        No persistent session, no resume — a fresh ``query()`` per call, yielding
        normalized ``StreamDelta`` partials, an ``AssistantText`` fallback (when
        partials never arrive), and a closing ``TurnResult``.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            StreamEvent,
            TextBlock,
            query,
        )

        from bobi.sdk import get_cli_path

        _configure_initialize_timeout()
        attempts = _env_int("BOBI_CLAUDE_CONNECT_ATTEMPTS", DEFAULT_CONNECT_ATTEMPTS)
        backoff = _env_float(
            "BOBI_CLAUDE_CONNECT_BACKOFF_SECONDS",
            DEFAULT_CONNECT_BACKOFF_SECONDS,
        )
        from bobi.brain import resolve_model_option

        extra = dict(options or {})
        model = resolve_model_option(model)
        extra.setdefault("permission_mode", "bypassPermissions")
        extra.setdefault("include_partial_messages", True)
        opts = ClaudeAgentOptions(
            cwd=cwd,
            model=model,
            cli_path=get_cli_path(),
            system_prompt=system_prompt,
            **extra,
        )

        for attempt in range(1, attempts + 1):
            yielded_message = False
            try:
                async for msg in query(prompt=user_prompt, options=opts):
                    yielded_message = True
                    if isinstance(msg, StreamEvent):
                        yield StreamDelta(text=_delta_text(msg.event))
                    elif isinstance(msg, AssistantMessage):
                        yield AssistantText(
                            text="\n".join(
                                b.text for b in msg.content if isinstance(b, TextBlock)
                            ),
                            usage=getattr(msg, "usage", None),
                        )
                    elif isinstance(msg, ResultMessage):
                        yield _result_to_turn(msg)
                return
            except Exception as exc:
                should_retry = (
                    not yielded_message
                    and attempt < attempts
                    and _is_initialize_timeout(exc)
                )
                if not should_retry:
                    raise
                log.warning(
                    "Claude initialize timed out during stream; retrying "
                    "(attempt %s/%s)",
                    attempt + 1,
                    attempts,
                )
                if backoff > 0:
                    await asyncio.sleep(backoff * attempt)
