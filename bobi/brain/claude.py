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
import json
import logging
import os
from typing import Any, AsyncIterator

from bobi.brain.base import (
    AssistantText,
    BrainCapabilities,
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
# The SDK defaults ``max_buffer_size`` to 1 MB and raises ``CLIJSONDecodeError``
# on the FIRST NDJSON message above it, which permanently kills the reader task
# for that connection (#719 / #718). A single tool result over 1 MB — e.g. an
# agent told to ``Read`` a multi-MB file, or a ~3 MB image that base64-inlines to
# ~4 MB — is enough to take a session down. Set a generous explicit ceiling so
# legitimate large messages pass; it is still a bound (not unlimited) so a
# genuinely runaway line is caught rather than OOMing the process.
DEFAULT_MAX_BUFFER_SIZE = 64 * 1024 * 1024  # 64 MB
# The SDK's own default, used as an absolute floor for the operator override so
# the knob can only raise the ceiling, never drop it back into the kill zone.
_SDK_DEFAULT_MAX_BUFFER_SIZE = 1024 * 1024  # 1 MB


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

    def __init__(self, options: Any, provider: str = "anthropic") -> None:
        self._options = options
        # Instance label follows the factory (GatewayBrain sessions attribute
        # their costs to "gateway", not real Anthropic spend).
        self.provider = provider
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
    costs = _model_usage_to_costs(getattr(msg, "model_usage", None))

    deferred = None
    dtu = getattr(msg, "deferred_tool_use", None)
    if dtu is not None:
        deferred = DeferredTool(
            name=getattr(dtu, "name", ""), input=getattr(dtu, "input", None)
        )

    error_kind, error_message, max_turns, turn_count = _terminal_error(msg)

    is_error = bool(getattr(msg, "is_error", False) or error_kind)

    return TurnResult(
        session_id=getattr(msg, "session_id", "") or "",
        is_error=is_error,
        error_kind=error_kind,
        error_message=error_message,
        max_turns=max_turns,
        turn_count=turn_count,
        api_error_status=getattr(msg, "api_error_status", None),
        total_cost_usd=getattr(msg, "total_cost_usd", 0.0) or 0.0,
        duration_ms=getattr(msg, "duration_ms", 0) or 0,
        num_turns=getattr(msg, "num_turns", 0) or 0,
        result_text=getattr(msg, "result", "") or "",
        deferred_tool=deferred,
        costs=costs,
    )


def _model_usage_to_costs(model_usage: Any) -> list[BrainCost]:
    """Normalize Claude SDK per-model usage into stored token facts.

    The SDK's real shape is ``dict[model, usage]``. Older tests and call sites
    also exercise a list-of-objects shape, so keep both. Anthropic reports
    prompt-cache reads/writes as separate fields; for display parity the
    recorded input volume is the full context input, while cache reads stay
    split for downstream renderers.
    """
    if not model_usage:
        return []

    if isinstance(model_usage, dict):
        return [
            _one_model_usage_to_cost(model, usage)
            for model, usage in model_usage.items()
        ]

    items = model_usage if isinstance(model_usage, list) else [model_usage]
    return [_one_model_usage_to_cost("", usage) for usage in items]


def _one_model_usage_to_cost(model: str, usage: Any) -> BrainCost:
    raw_input = _usage_int(usage, "input_tokens")
    cache_read = _usage_int(usage, "cache_read_input_tokens")
    cache_creation = _usage_int(usage, "cache_creation_input_tokens")
    return BrainCost(
        model=model or _usage_str(usage, "model"),
        input_tokens=raw_input + cache_read + cache_creation,
        cached_input_tokens=cache_read,
        output_tokens=_usage_int(usage, "output_tokens"),
    )


def _usage_int(usage: Any, key: str) -> int:
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    return value if isinstance(value, int) else 0


def _usage_str(usage: Any, key: str) -> str:
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    return value if isinstance(value, str) else ""


def _terminal_error(msg: Any) -> tuple[str, str, int | None, int | None]:
    """Return provider-neutral terminal error details for known SDK failures."""
    stop_reason = str(getattr(msg, "stop_reason", "") or "")
    max_turns, turn_count = _max_turns_from_errors(getattr(msg, "errors", None))

    if stop_reason == "max_turns_reached" or max_turns is not None:
        kind = "max_turns_reached"
        message = _render_max_turns_error(max_turns, turn_count)
        return kind, message, max_turns, turn_count

    return "", "", None, None


def _max_turns_from_errors(errors: Any) -> tuple[int | None, int | None]:
    if not errors:
        return None, None
    items = errors if isinstance(errors, (list, tuple)) else [errors]
    for item in items:
        parsed = item
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except (TypeError, ValueError):
                continue
        if not isinstance(parsed, dict):
            continue
        attachment = parsed.get("attachment")
        if (
            parsed.get("type") == "attachment"
            and isinstance(attachment, dict)
            and attachment.get("type") == "max_turns_reached"
        ):
            return (
                _int_or_none(
                    attachment.get("maxTurns", attachment.get("max_turns"))
                ),
                _int_or_none(
                    attachment.get("turnCount", attachment.get("turn_count"))
                ),
            )
    return None, None


def _render_max_turns_error(max_turns: int | None,
                            turn_count: int | None) -> str:
    details = []
    if max_turns is not None:
        details.append(f"max={max_turns}")
    if turn_count is not None:
        details.append(f"turns={turn_count}")
    if details:
        return f"max_turns_reached ({', '.join(details)})"
    return "max_turns_reached"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _max_buffer_size() -> int:
    """The NDJSON read-buffer ceiling for a Claude session (#719).

    Operator-overridable via ``BOBI_CLAUDE_MAX_BUFFER_SIZE`` (bytes); defaults to
    :data:`DEFAULT_MAX_BUFFER_SIZE`. Guards against the SDK's 1 MB default
    silently killing any session that reads a single >1 MB message.

    Floored at the SDK's own 1 MB default: this knob exists only to RAISE the
    ceiling, so a misconfigured tiny/zero value (``_env_int`` clamps to >=1)
    cannot silently recreate the very failure this guards against.
    """
    configured = _env_int("BOBI_CLAUDE_MAX_BUFFER_SIZE", DEFAULT_MAX_BUFFER_SIZE)
    return max(configured, _SDK_DEFAULT_MAX_BUFFER_SIZE)


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
    # The Claude CLI accepts --resume together with a different --model, so a
    # session's transcript continues under the new model (#642; verified live
    # by tests/integration/test_cross_model_resume.py).
    capabilities = BrainCapabilities(cross_model_resume=True)

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

        from bobi.brain import with_default_effort_option, with_default_model_option

        extra = with_default_effort_option(with_default_model_option(options))
        # Defaults every call site shared; an explicit value in ``options`` wins.
        extra.setdefault("permission_mode", "bypassPermissions")
        # Never inherit the SDK's 1 MB max_buffer_size default — a single >1 MB
        # message (large Read, inlined image) would kill the session (#719).
        extra.setdefault("max_buffer_size", _max_buffer_size())
        kwargs = dict(cwd=cwd, cli_path=get_cli_path(), resume=resume, **extra)
        # Only pass system_prompt when the caller set one — the MCP probe builds
        # a session with no prompt, and forcing system_prompt=None would override
        # the SDK's own default.
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        return _ClaudeSession(ClaudeAgentOptions(**kwargs), provider=self.provider)

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
        from bobi.brain import resolve_model_option, with_default_effort_option

        extra = with_default_effort_option(options)
        model = resolve_model_option(model)
        extra.setdefault("permission_mode", "bypassPermissions")
        extra.setdefault("include_partial_messages", True)
        # Match the persistent-session guard: a >1 MB message must not kill the
        # one-shot stream either (#719).
        extra.setdefault("max_buffer_size", _max_buffer_size())
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
