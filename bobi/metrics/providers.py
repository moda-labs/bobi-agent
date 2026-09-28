"""Provider usage contracts shared by adapters, reconciliation, and smokes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


class ProviderContractError(ValueError):
    """The provider event does not match a supported exact-usage contract."""


def _token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _event_id(provider: str, value: str, raw: dict[str, Any]) -> str:
    if value:
        return value
    material = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    return f"{provider}_{hashlib.sha256(material).hexdigest()}"


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    model: str
    provider_event_id: str
    measurement_source: str
    scope: str
    input_tokens: int | None
    uncached_input_tokens: int | None
    cache_read_input_tokens: int | None
    cache_write_input_tokens: int | None
    cache_write_5m_input_tokens: int | None
    cache_write_1h_input_tokens: int | None
    cache_write_unknown_ttl_input_tokens: int | None
    cache_write_breakdown_complete: bool
    output_tokens: int | None
    reasoning_output_tokens: int | None
    raw_usage: dict[str, Any]
    token_semantics_version: int = 1

    def to_measurement_payload(
        self,
        *,
        measurement_id: str,
        turn_id: str,
        observed_at_us: int,
        invocation_id: str | None = None,
    ) -> dict[str, Any]:
        if self.scope == "invocation" and not invocation_id:
            raise ValueError("invocation usage requires invocation_id")
        return {
            "measurement_id": measurement_id,
            "scope": self.scope,
            "turn_id": turn_id,
            "invocation_id": invocation_id,
            "provider": self.provider,
            "model": self.model,
            "provider_event_id": self.provider_event_id,
            "measurement_source": self.measurement_source,
            "is_estimated": 0,
            "estimator_name": None,
            "estimator_version": None,
            "token_semantics_version": self.token_semantics_version,
            "input_tokens": self.input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "cache_write_5m_input_tokens": self.cache_write_5m_input_tokens,
            "cache_write_1h_input_tokens": self.cache_write_1h_input_tokens,
            "cache_write_unknown_ttl_input_tokens": (
                self.cache_write_unknown_ttl_input_tokens
            ),
            "cache_write_breakdown_complete": int(
                self.cache_write_breakdown_complete
            ),
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "observed_at_us": observed_at_us,
            "raw_usage_json": json.dumps(
                self.raw_usage, sort_keys=True, separators=(",", ":")
            ),
            "supersedes_measurement_id": None,
        }

    def comparable_tokens(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            key: data[key]
            for key in (
                "input_tokens",
                "uncached_input_tokens",
                "cache_read_input_tokens",
                "cache_write_input_tokens",
                "cache_write_5m_input_tokens",
                "cache_write_1h_input_tokens",
                "cache_write_unknown_ttl_input_tokens",
                "cache_write_breakdown_complete",
                "output_tokens",
                "reasoning_output_tokens",
            )
        }


def claude_usage(
    raw_usage: dict[str, Any],
    *,
    model: str,
    provider_event_id: str = "",
    source: str = "provider_stream",
    scope: str = "invocation",
) -> ProviderUsage:
    raw_input = _token(_first(raw_usage, "input_tokens", "inputTokens"))
    cache_read = _token(
        _first(raw_usage, "cache_read_input_tokens", "cacheReadInputTokens")
    )
    cache_write = _token(
        _first(raw_usage, "cache_creation_input_tokens", "cacheCreationInputTokens")
    )
    detail = _first(raw_usage, "cache_creation", "cacheCreation")
    detail = detail if isinstance(detail, dict) else {}
    cache_5m = _token(
        _first(detail, "ephemeral_5m_input_tokens", "ephemeral5mInputTokens")
    )
    cache_1h = _token(
        _first(detail, "ephemeral_1h_input_tokens", "ephemeral1hInputTokens")
    )
    detail_present = cache_5m is not None or cache_1h is not None
    if cache_write is None and detail_present:
        cache_write = (cache_5m or 0) + (cache_1h or 0)
    breakdown_complete = (
        detail_present
        and cache_write is not None
        and cache_write == (cache_5m or 0) + (cache_1h or 0)
    )
    parts = (raw_input, cache_read, cache_write)
    normalized_input = (
        sum(value or 0 for value in parts)
        if any(value is not None for value in parts)
        else None
    )
    output_detail = _first(raw_usage, "output_tokens_details", "outputTokensDetails")
    output_detail = output_detail if isinstance(output_detail, dict) else {}
    reasoning_output = _first(
        output_detail,
        "reasoning_tokens",
        "reasoningTokens",
        "thinking_tokens",
        "thinkingTokens",
    )
    if reasoning_output is None:
        reasoning_output = _first(
            raw_usage,
            "reasoning_output_tokens",
            "reasoningOutputTokens",
            "thinking_tokens",
            "thinkingTokens",
        )
    return ProviderUsage(
        provider="anthropic",
        model=str(model or _first(raw_usage, "canonicalModel", "model") or "claude"),
        provider_event_id=_event_id("claude", provider_event_id, raw_usage),
        measurement_source=source,
        scope=scope,
        input_tokens=normalized_input,
        uncached_input_tokens=raw_input,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        cache_write_5m_input_tokens=cache_5m,
        cache_write_1h_input_tokens=cache_1h,
        cache_write_unknown_ttl_input_tokens=(
            None if breakdown_complete else cache_write
        ),
        cache_write_breakdown_complete=breakdown_complete,
        output_tokens=_token(_first(raw_usage, "output_tokens", "outputTokens")),
        reasoning_output_tokens=_token(reasoning_output),
        raw_usage=json.loads(json.dumps(raw_usage)),
    )


def codex_usage(
    raw_usage: dict[str, Any],
    *,
    model: str,
    provider_event_id: str = "",
    source: str = "provider_stream",
    scope: str = "turn",
) -> ProviderUsage:
    input_tokens = _token(raw_usage.get("input_tokens"))
    cached = _token(raw_usage.get("cached_input_tokens"))
    cache_write = _token(raw_usage.get("cache_write_input_tokens"))
    uncached = None
    if input_tokens is not None and cached is not None:
        uncached = max(input_tokens - cached, 0)
    return ProviderUsage(
        provider="openai",
        model=model or "codex",
        provider_event_id=_event_id("codex", provider_event_id, raw_usage),
        measurement_source=source,
        scope=scope,
        input_tokens=input_tokens,
        uncached_input_tokens=uncached,
        cache_read_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        cache_write_5m_input_tokens=None,
        cache_write_1h_input_tokens=None,
        cache_write_unknown_ttl_input_tokens=cache_write,
        cache_write_breakdown_complete=False,
        output_tokens=_token(raw_usage.get("output_tokens")),
        reasoning_output_tokens=_token(raw_usage.get("reasoning_output_tokens")),
        raw_usage=json.loads(json.dumps(raw_usage)),
    )


def parse_claude_transcript(path: Path | str) -> list[ProviderUsage]:
    """Parse exact invocation usage, deduplicated by provider request/message ID."""
    found: dict[str, ProviderUsage] = {}
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                break
            if event.get("type") != "assistant":
                continue
            message = event.get("message") or {}
            if message.get("model") == "<synthetic>":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            event_id = str(
                message.get("id") or event.get("requestId") or event.get("uuid") or ""
            )
            parsed = claude_usage(
                usage,
                model=str(message.get("model") or "claude"),
                provider_event_id=event_id,
                source="claude_transcript",
            )
            found[parsed.provider_event_id] = parsed
    return list(found.values())


def parse_codex_stdout(events: Iterable[dict[str, Any]], model: str) -> ProviderUsage:
    """Select the terminal turn aggregate; repeated progress counters are ignored."""
    terminal = [event for event in events if event.get("type") == "turn.completed"]
    if not terminal:
        raise ProviderContractError("codex stream has no turn.completed usage")
    event = terminal[-1]
    usage = event.get("usage")
    if not isinstance(usage, dict):
        raise ProviderContractError("codex turn.completed has no usage object")
    event_id = str(event.get("turn_id") or event.get("id") or "")
    return codex_usage(usage, model=model, provider_event_id=event_id)


def parse_codex_rollout(path: Path | str, model: str = "codex") -> list[ProviderUsage]:
    """Select the last exact token_usage_record for each persisted Codex turn."""
    latest: dict[str, tuple[int, ProviderUsage]] = {}
    turn_models: dict[str, str] = {}
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                break
            if event.get("type") == "turn_context":
                payload = event.get("payload") or {}
                turn_id = str(payload.get("turn_id") or "")
                selected_model = str(payload.get("model") or "")
                if turn_id and selected_model:
                    turn_models[turn_id] = selected_model
                continue
            if event.get("type") != "token_usage_record":
                continue
            payload = event.get("payload") or {}
            usage = payload.get("turn_token_usage") or payload.get("usage")
            if not isinstance(usage, dict):
                continue
            turn_id = str(payload.get("turn_id") or payload.get("root_turn_id") or "")
            provider_event_id = str(payload.get("response_id") or turn_id)
            parsed = codex_usage(
                usage,
                model=turn_models.get(turn_id) or model,
                provider_event_id=provider_event_id,
                source="codex_rollout",
            )
            ordinal = event.get("ordinal")
            ordinal = ordinal if isinstance(ordinal, int) else -1
            previous = latest.get(turn_id)
            if previous is None or ordinal >= previous[0]:
                latest[turn_id] = (ordinal, parsed)
    return [value[1] for _, value in sorted(latest.items())]
