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
import platform
import shutil
import time
from collections import deque
from contextlib import suppress
from typing import Any, AsyncIterator

from bobi.brain.base import (
    ERROR_KIND_MAX_TURNS,
    AssistantText,
    BrainCost,
    BrainInvocation,
    BrainMessage,
    BrainSession,
    BrainToolExecution,
    BrainUsage,
    DeferredTool,
    StreamDelta,
    TurnResult,
    classify_brain_unavailability,
)
from bobi.brain.gateway import (
    GatewayAwareEngine,
    gateway_base_url,
    with_gateway_env,
)


def _tool_kind(name: str, *, server: bool = False) -> str:
    normalized = name.strip().lower().replace("-", "_")
    if normalized in {"read", "view", "view_file", "read_file"}:
        return "file_read"
    if normalized in {"edit", "write", "edit_file", "write_file", "apply_patch"}:
        return "file_edit"
    if normalized in {"bash", "shell", "command", "command_execution"}:
        return "shell"
    if normalized in {"websearch", "web_search", "webfetch", "web_fetch"}:
        return "web_search"
    return "server" if server else "client"
from bobi.metrics.providers import claude_usage

log = logging.getLogger(__name__)

_UNSET = object()
_CUMULATIVE_USAGE_FIELDS = (
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("cache_read_input_tokens", "cacheReadInputTokens"),
    ("cache_creation_input_tokens", "cacheCreationInputTokens"),
    ("thinking_tokens", "thinkingTokens"),
    ("web_search_requests", "webSearchRequests"),
    ("cost_usd", "costUSD"),
)

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


def get_cli_path() -> str:
    """Locate the ``claude`` CLI at call time, container-safe.

    Prefer ``PATH`` — the only thing that works in the Linux container image,
    where the pinned CLI is installed on ``PATH`` (the private deploy repo's
    CONTAINERIZED_DEPLOYMENT.md, The image). When it isn't found, fall
    back to the Homebrew location *only* on
    macOS dev machines; on every other platform fall back to the bare name so
    exec still resolves it via ``PATH`` at spawn time rather than a
    macOS-specific absolute path that doesn't exist in the container.

    Re-resolves on every call so a CLI that lands on ``PATH`` after import —
    or a test that patches the environment — is picked up.
    """
    found = shutil.which("claude")
    if found:
        return found
    if platform.system() == "Darwin":
        return "/opt/homebrew/bin/claude"
    return "claude"


def _delta_text(event: Any) -> str:
    """Pull the text out of one raw Anthropic streaming event, or ''.

    The canonical home for the vendor-specific partial-stream shape
    (``content_block_delta`` / ``text_delta``).
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
        # Instance label follows the factory's provider property (the gateway
        # base-url pin flips it, so gateway sessions attribute their costs to
        # "gateway", not real Anthropic spend).
        self.provider = provider
        self._client = self._new_client()
        self._model_usage_baseline: dict[str, dict[str, Any]] = {}
        self._total_cost_baseline = 0.0
        self._cumulative_usage_ready = not bool(getattr(options, "resume", None))

    def _new_client(self) -> Any:
        from claude_agent_sdk import ClaudeSDKClient

        return ClaudeSDKClient(self._options)

    async def connect(self) -> None:
        _configure_initialize_timeout()
        attempts = _env_int("BOBI_CLAUDE_CONNECT_ATTEMPTS", DEFAULT_CONNECT_ATTEMPTS)
        backoff = _env_float(
            "BOBI_CLAUDE_CONNECT_BACKOFF_SECONDS",
            DEFAULT_CONNECT_BACKOFF_SECONDS,
        )

        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._client = self._new_client()
            try:
                # Bare connect: setup only, no turn (#1016). The SDK defaults
                # prompt to None, which keeps no-arg fakes/clients working.
                await self._client.connect()
                return
            except Exception as exc:
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

    async def query(self, text: str) -> None:
        await self._client.query(text)

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def abort(self) -> None:
        """Force-kill a wedged Claude CLI transport without awaiting cleanup.

        Normal teardown still uses ``disconnect()`` so the CLI can flush its
        session. This is only the hard-timeout escape hatch: kill the child
        synchronously, then the already-running async disconnect can reap it.
        """
        query = getattr(self._client, "_query", None)
        if query is not None:
            with suppress(Exception):
                query.close_receive_stream()

        transport = getattr(self._client, "_transport", None)
        transport_abort = getattr(transport, "abort", None)
        if callable(transport_abort):
            with suppress(Exception):
                transport_abort()

        process = getattr(transport, "_process", None)
        if process is not None and getattr(process, "returncode", None) is None:
            with suppress(ProcessLookupError):
                process.kill()

    async def get_mcp_status(self) -> dict:
        """Passthrough to the SDK's MCP status probe (preflight only).

        Not part of the BrainSession protocol — an optional capability the MCP
        preflight uses; brains without an equivalent simply won't offer it.
        """
        return await self._client.get_mcp_status()

    async def receive_response(self) -> AsyncIterator[BrainMessage]:
        """Translate one turn's SDK messages into normalized brain messages."""
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            ServerToolResultBlock,
            ServerToolUseBlock,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )
        tool_use_types = tuple(
            item for item in (ToolUseBlock, ServerToolUseBlock)
            if isinstance(item, type)
        )
        tool_result_types = tuple(
            item for item in (ToolResultBlock, ServerToolResultBlock)
            if isinstance(item, type)
        )
        user_message_type = UserMessage if isinstance(UserMessage, type) else ()

        assistant_error_kind = ""
        assistant_error_message = ""
        turn_started_us = time.time_ns() // 1000
        last_event_us = turn_started_us
        invocations: list[BrainInvocation] = []
        tool_states: dict[str, dict[str, Any]] = {}
        completed_tool_ids: list[str] = []

        def finish_tool(block: Any, observed_at_us: int) -> None:
            tool_id = str(getattr(block, "tool_use_id", "") or "")
            if not tool_id:
                return
            state = tool_states.setdefault(
                tool_id,
                {
                    "provider_tool_call_id": tool_id,
                    "tool_name": "unknown",
                    "tool_kind": "provider",
                    "triggering_provider_event_id": "",
                    "started_at_us": observed_at_us,
                },
            )
            state["ended_at_us"] = observed_at_us
            state["status"] = "failed" if getattr(block, "is_error", False) else "completed"
            state["is_error"] = bool(getattr(block, "is_error", False))
            if tool_id not in completed_tool_ids:
                completed_tool_ids.append(tool_id)

        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                observed_at_us = time.time_ns() // 1000
                text_parts = [
                    b.text for b in msg.content if isinstance(b, TextBlock)
                ]
                text = "\n".join(text_parts) if text_parts else ""
                error_kind = str(getattr(msg, "error", "") or "")
                if error_kind:
                    assistant_error_kind = error_kind
                    assistant_error_message = text
                provider_event_id = str(
                    getattr(msg, "message_id", None)
                    or getattr(msg, "uuid", None)
                    or f"claude-invocation-{len(invocations) + 1}"
                )
                invocation_usage = None
                if getattr(msg, "usage", None):
                    invocation_usage = _one_model_usage(
                        str(getattr(msg, "model", "") or ""),
                        msg.usage,
                        provider_event_id=provider_event_id,
                    )
                invocations.append(
                    BrainInvocation(
                        provider_event_id=provider_event_id,
                        model=str(getattr(msg, "model", "") or ""),
                        started_at_us=last_event_us,
                        ended_at_us=observed_at_us,
                        stop_reason=str(getattr(msg, "stop_reason", "") or ""),
                        status="failed" if error_kind else "completed",
                        usage=invocation_usage,
                    )
                )
                for tool_id in completed_tool_ids:
                    if not tool_states[tool_id].get("consuming_provider_event_id"):
                        tool_states[tool_id]["consuming_provider_event_id"] = provider_event_id
                completed_tool_ids.clear()
                for block in msg.content:
                    if isinstance(block, tool_use_types):
                        tool_id = str(getattr(block, "id", "") or "")
                        if tool_id:
                            tool_name = str(getattr(block, "name", "") or "unknown")
                            is_server = (
                                isinstance(ServerToolUseBlock, type)
                                and isinstance(block, ServerToolUseBlock)
                            )
                            tool_states[tool_id] = {
                                "provider_tool_call_id": tool_id,
                                "tool_name": tool_name,
                                "tool_kind": _tool_kind(tool_name, server=is_server),
                                "triggering_provider_event_id": provider_event_id,
                                "started_at_us": observed_at_us,
                                "status": "running",
                                "is_error": False,
                            }
                    elif isinstance(block, tool_result_types):
                        finish_tool(block, observed_at_us)
                last_event_us = observed_at_us
                yield AssistantText(
                    text=text,
                    usage=getattr(msg, "usage", None),
                )
            elif isinstance(msg, user_message_type):
                observed_at_us = time.time_ns() // 1000
                content = getattr(msg, "content", None)
                blocks = content if isinstance(content, list) else []
                for block in blocks:
                    if isinstance(block, tool_result_types):
                        finish_tool(block, observed_at_us)
                last_event_us = observed_at_us
            elif isinstance(msg, ResultMessage):
                observed_at_us = time.time_ns() // 1000
                cumulative_usage = getattr(msg, "model_usage", None)
                cumulative_cost = getattr(msg, "total_cost_usd", 0.0) or 0.0
                usage_snapshot = _model_usage_snapshot(cumulative_usage)
                if usage_snapshot:
                    if getattr(self, "_cumulative_usage_ready", True):
                        usage_baseline = getattr(self, "_model_usage_baseline", {})
                        turn_usage = _model_usage_delta(cumulative_usage, usage_baseline)
                        cost_baseline = getattr(self, "_total_cost_baseline", 0.0)
                        turn_cost = _counter_delta(cumulative_cost, cost_baseline)
                    else:
                        turn_usage = {}
                        turn_cost = 0.0
                else:
                    turn_usage = cumulative_usage
                    turn_cost = cumulative_cost
                result = _result_to_turn(
                    msg,
                    assistant_error_kind=assistant_error_kind,
                    assistant_error_message=assistant_error_message,
                    model_usage_override=turn_usage,
                    total_cost_usd_override=turn_cost,
                )
                if usage_snapshot:
                    self._model_usage_baseline = usage_snapshot
                    self._total_cost_baseline = float(cumulative_cost)
                    self._cumulative_usage_ready = True
                result.invocations = invocations
                result.tool_executions = [
                    BrainToolExecution(**state)
                    for state in tool_states.values()
                ]
                yield result
            # Other SDK message types carry no signal the call sites consume.


def _result_to_turn(
    msg: Any,
    *,
    assistant_error_kind: str = "",
    assistant_error_message: str = "",
    model_usage_override: Any = _UNSET,
    total_cost_usd_override: Any = _UNSET,
) -> TurnResult:
    """Normalize an SDK ``ResultMessage`` into a :class:`TurnResult`."""
    model_usage = (
        getattr(msg, "model_usage", None)
        if model_usage_override is _UNSET
        else model_usage_override
    )
    usage = _model_usage_to_usage(
        model_usage,
        result_usage=getattr(msg, "usage", None),
    )
    costs = [item.legacy_cost() for item in usage]

    deferred = None
    dtu = getattr(msg, "deferred_tool_use", None)
    if dtu is not None:
        deferred = DeferredTool(
            name=getattr(dtu, "name", ""), input=getattr(dtu, "input", None)
        )

    error_kind, error_message, max_turns, turn_count = _terminal_error(msg)
    if not error_kind and assistant_error_kind:
        error_kind = assistant_error_kind
        error_message = assistant_error_message

    result_text = getattr(msg, "result", "") or ""
    unavailable_kind = classify_brain_unavailability(
        error_kind,
        error_message or result_text,
    )
    if unavailable_kind:
        error_kind = unavailable_kind
        error_message = error_message or result_text

    is_error = bool(getattr(msg, "is_error", False) or error_kind)

    return TurnResult(
        session_id=getattr(msg, "session_id", "") or "",
        is_error=is_error,
        error_kind=error_kind,
        error_message=error_message,
        max_turns=max_turns,
        turn_count=turn_count,
        api_error_status=getattr(msg, "api_error_status", None),
        total_cost_usd=(
            getattr(msg, "total_cost_usd", 0.0) or 0.0
            if total_cost_usd_override is _UNSET
            else total_cost_usd_override
        ),
        duration_ms=getattr(msg, "duration_ms", 0) or 0,
        api_duration_ms=getattr(msg, "duration_api_ms", 0) or 0,
        num_turns=getattr(msg, "num_turns", 0) or 0,
        provider_turn_id=str(getattr(msg, "uuid", "") or ""),
        result_text=result_text,
        deferred_tool=deferred,
        costs=costs,
        usage=usage,
    )


def _counter_delta(current: Any, previous: Any) -> int | float:
    current_value = current if isinstance(current, (int, float)) else 0
    previous_value = previous if isinstance(previous, (int, float)) else 0
    return current_value - previous_value if current_value >= previous_value else current_value


def _model_usage_snapshot(model_usage: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(model_usage, dict):
        return {}
    return {
        str(model): _json_safe_usage(usage)
        for model, usage in model_usage.items()
    }


def _model_usage_delta(
    model_usage: Any,
    previous: dict[str, dict[str, Any]],
) -> Any:
    """Convert Claude's session-cumulative modelUsage into per-turn usage."""
    current = _model_usage_snapshot(model_usage)
    if not current:
        return model_usage
    result: dict[str, dict[str, Any]] = {}
    for model, usage in current.items():
        prior = previous.get(model, {})
        delta = dict(usage)
        changed = not prior
        for keys in _CUMULATIVE_USAGE_FIELDS:
            value = _usage_value(usage, *keys)
            if value is None:
                continue
            prior_value = _usage_value(prior, *keys)
            difference = _counter_delta(value, prior_value)
            selected_key = next((key for key in keys if key in usage), keys[0])
            for key in keys:
                delta.pop(key, None)
            delta[selected_key] = difference
            changed = changed or difference != 0
        if changed:
            result[model] = delta
    return result


def _model_usage_to_costs(model_usage: Any) -> list[BrainCost]:
    """Normalize Claude SDK per-model usage into stored token facts.

    The SDK's real shape is ``dict[model, usage]``. Older tests and call sites
    also exercise a list-of-objects shape, so keep both. Anthropic reports
    prompt-cache reads/writes as separate fields; for display parity the
    recorded input volume is the full context input, while cache reads stay
    split for downstream renderers.
    """
    return [usage.legacy_cost() for usage in _model_usage_to_usage(model_usage)]


def _one_model_usage_to_cost(model: str, usage: Any) -> BrainCost:
    return _one_model_usage(model, usage).legacy_cost()


def _model_usage_to_usage(
    model_usage: Any, *, result_usage: Any = None
) -> list[BrainUsage]:
    """Preserve every supported Claude usage dimension without guessing."""
    if not model_usage:
        return []
    if isinstance(model_usage, dict):
        items = [
            (str(model), _json_safe_usage(item))
            for model, item in model_usage.items()
        ]
        detail = _json_safe_usage(result_usage) if result_usage else {}
        detail_match = _matching_model_usage(items, detail) if detail else None
        return [
            _one_model_usage(
                model,
                _merge_result_usage(raw, detail) if model == detail_match else raw,
            )
            for model, raw in items
        ]
    items = model_usage if isinstance(model_usage, list) else [model_usage]
    return [_one_model_usage("", usage) for usage in items]


def _matching_model_usage(
    items: list[tuple[str, dict[str, Any]]], detail: dict[str, Any]
) -> str | None:
    detailed = claude_usage(detail, model="")
    fields = (
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
    )
    candidates = []
    for model, raw in items:
        summary = claude_usage(raw, model=model)
        if all(
            getattr(detailed, field) is None
            or getattr(detailed, field) == getattr(summary, field)
            for field in fields
        ):
            candidates.append(model)
    return candidates[0] if len(candidates) == 1 else None


def _merge_result_usage(
    summary: dict[str, Any], detail: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(summary)
    merged.update(detail)
    merged["model_usage"] = summary
    merged["turn_usage"] = detail
    return merged


def _one_model_usage(
    model: str, usage: Any, *, provider_event_id: str = ""
) -> BrainUsage:
    raw_usage = _json_safe_usage(usage)
    normalized = claude_usage(
        raw_usage,
        model=model or _usage_str(usage, "canonicalModel", "model"),
        provider_event_id=provider_event_id,
        scope="turn",
    )
    return BrainUsage(
        model=normalized.model,
        provider_event_id=normalized.provider_event_id,
        input_tokens=normalized.input_tokens,
        uncached_input_tokens=normalized.uncached_input_tokens,
        cache_read_input_tokens=normalized.cache_read_input_tokens,
        cache_write_input_tokens=normalized.cache_write_input_tokens,
        cache_write_5m_input_tokens=normalized.cache_write_5m_input_tokens,
        cache_write_1h_input_tokens=normalized.cache_write_1h_input_tokens,
        cache_write_unknown_ttl_input_tokens=(
            normalized.cache_write_unknown_ttl_input_tokens
        ),
        cache_write_breakdown_complete=normalized.cache_write_breakdown_complete,
        output_tokens=normalized.output_tokens,
        reasoning_output_tokens=normalized.reasoning_output_tokens,
        raw_usage=normalized.raw_usage,
        token_semantics_version=normalized.token_semantics_version,
    )


def _usage_value(usage: Any, *keys: str) -> Any:
    """First present value across a field's accepted spellings.

    The live SDK (``claude_agent_sdk.types.ModelUsage``) passes the CLI's
    ``modelUsage`` through verbatim, so its keys are camelCase; older SDK
    versions and Bobi's own fixtures use snake_case. Reading only one spelling
    silently recorded zero for every token counter (#935). First match wins, so
    a payload carrying both spellings of one field is read once, never summed.
    """
    for key in keys:
        value = usage.get(key) if isinstance(usage, dict) else getattr(
            usage, key, None
        )
        if value is not None:
            return value
    return None


def _usage_str(usage: Any, *keys: str) -> str:
    value = _usage_value(usage, *keys)
    return value if isinstance(value, str) else ""


def _json_safe_usage(usage: Any) -> dict[str, Any]:
    if isinstance(usage, dict):
        source = usage
    else:
        source = getattr(usage, "__dict__", {})
    try:
        return json.loads(json.dumps(source, default=str))
    except (TypeError, ValueError):
        return {}


def _terminal_error(msg: Any) -> tuple[str, str, int | None, int | None]:
    """Return provider-neutral terminal error details for known SDK failures."""
    stop_reason = str(getattr(msg, "stop_reason", "") or "")
    found, max_turns, turn_count = _max_turns_from_errors(
        getattr(msg, "errors", None)
    )
    if (
        stop_reason != ERROR_KIND_MAX_TURNS
        and not found
        and (getattr(msg, "is_error", False) or not getattr(msg, "result", None))
    ):
        found, max_turns, turn_count = _max_turns_from_transcript(
            getattr(msg, "session_id", "") or ""
        )

    if stop_reason == ERROR_KIND_MAX_TURNS or found:
        message = _render_max_turns_error(max_turns, turn_count)
        return ERROR_KIND_MAX_TURNS, message, max_turns, turn_count

    return "", "", None, None


def _max_turns_from_errors(errors: Any) -> tuple[bool, int | None, int | None]:
    if not errors:
        return False, None, None
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
            and attachment.get("type") == ERROR_KIND_MAX_TURNS
        ):
            return (
                True,
                _int_or_none(
                    attachment.get("maxTurns", attachment.get("max_turns"))
                ),
                _int_or_none(
                    attachment.get("turnCount", attachment.get("turn_count"))
                ),
            )
    return False, None, None


def _max_turns_from_transcript(
    session_id: str,
) -> tuple[bool, int | None, int | None]:
    """Fallback for Claude SDK runs whose JSONL has terminal metadata only."""
    from bobi.chat_history import find_claude_transcript

    transcript = find_claude_transcript(session_id)
    if transcript is None:
        return False, None, None
    try:
        with transcript.open() as fh:
            lines = deque(fh, maxlen=200)
    except OSError:
        return False, None, None
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except (TypeError, ValueError):
            continue
        found, max_turns, turn_count = _max_turns_from_errors(parsed)
        if found:
            return found, max_turns, turn_count
    return False, None, None


def _render_max_turns_error(max_turns: int | None,
                            turn_count: int | None) -> str:
    details = []
    if max_turns is not None:
        details.append(f"max={max_turns}")
    if turn_count is not None:
        details.append(f"turns={turn_count}")
    if details:
        return f"{ERROR_KIND_MAX_TURNS} ({', '.join(details)})"
    return ERROR_KIND_MAX_TURNS


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


class ClaudeBrain(GatewayAwareEngine):
    """Factory for Claude Code sessions (the default brain).

    Gateway-aware (#789): when a gateway base URL is pinned (``brain.base_url``
    on a claude-engine team), every session gets the ``ANTHROPIC_*`` rerouting
    env merged in, and the mixin flips ``provider`` / ``capabilities``. Same
    machinery either way - gateway mode is endpoint config, not a different
    brain.
    """

    name = "claude"
    native_provider = "anthropic"
    # Cross-model resume (native): the Claude CLI accepts --resume together
    # with a different --model, so a session's transcript continues under the
    # new model (#642; verified live by
    # tests/integration/test_cross_model_resume.py).
    # Efforts per the claude CLI's --effort choices (verified 2026-07-14; an
    # unknown value is warned about and IGNORED, so validation is the only
    # place a typo surfaces). The vocabulary is the CLI's in gateway mode too -
    # the CLI is what parses the flag.
    _EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

    def make_session(
        self,
        *,
        cwd: str | None,
        system_prompt: Any,
        resume: str | None = None,
        options: dict | None = None,
    ) -> BrainSession:
        from claude_agent_sdk import ClaudeAgentOptions

        from bobi.brain import with_default_effort_option, with_default_model_option

        extra = with_default_effort_option(with_default_model_option(options))
        if gateway_base_url():
            extra = with_gateway_env(extra)
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

        _configure_initialize_timeout()
        attempts = _env_int("BOBI_CLAUDE_CONNECT_ATTEMPTS", DEFAULT_CONNECT_ATTEMPTS)
        backoff = _env_float(
            "BOBI_CLAUDE_CONNECT_BACKOFF_SECONDS",
            DEFAULT_CONNECT_BACKOFF_SECONDS,
        )
        from bobi.brain import resolve_model_option, with_default_effort_option

        extra = with_default_effort_option(options)
        if gateway_base_url():
            extra = with_gateway_env(extra)
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
            assistant_error_kind = ""
            assistant_error_message = ""
            try:
                async for msg in query(prompt=user_prompt, options=opts):
                    yielded_message = True
                    if isinstance(msg, StreamEvent):
                        yield StreamDelta(text=_delta_text(msg.event))
                    elif isinstance(msg, AssistantMessage):
                        text = "\n".join(
                            b.text for b in msg.content if isinstance(b, TextBlock)
                        )
                        error_kind = str(getattr(msg, "error", "") or "")
                        if error_kind:
                            assistant_error_kind = error_kind
                            assistant_error_message = text
                        yield AssistantText(
                            text=text,
                            usage=getattr(msg, "usage", None),
                        )
                    elif isinstance(msg, ResultMessage):
                        yield _result_to_turn(
                            msg,
                            assistant_error_kind=assistant_error_kind,
                            assistant_error_message=assistant_error_message,
                        )
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
