"""Cost attribution and rollup for multi-model sessions.

Provides a price table for providers that don't return cost directly,
and rollup functions for the named cost-attribution CLI command.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Fallback price per million tokens (input, cached_input, output) by
# provider:model. Used only when the provider doesn't report cost directly
# (e.g. the codex brain reports token counts only - #760); an estimate is
# never mixed into recorded dollars, it rides the separate estimated_* fields.
# The cached-input rate is carried per model because the discount is NOT a
# constant: gpt-5.x cache reads are 10% of input, o-series and codex-mini are
# 25%, gpt-4o was 50%. Models a fleet may have recorded historically stay in
# the table - the fold prices the model each session actually recorded, never
# "the current default". Approximate list prices as of 2026-07
# (developers.openai.com/api/docs/pricing; cross-checked against LiteLLM's
# model_prices json and models.dev) and should be updated periodically - the
# authoritative source of cost is always the provider's own billing.
PRICE_TABLE: dict[str, tuple[float, float, float]] = {
    # Anthropic (cache read = 10% of input)
    "anthropic:claude-sonnet-4-20250514": (3.0, 0.30, 15.0),
    "anthropic:claude-opus-4-20250514": (15.0, 1.50, 75.0),
    "anthropic:claude-haiku-3-5-20241022": (0.80, 0.08, 4.0),
    # OpenAI - current codex lineup (gpt-5.6 family) and still-offered tiers
    "openai:gpt-5.6": (5.00, 0.50, 30.00),
    "openai:gpt-5.6-sol": (5.00, 0.50, 30.00),
    "openai:gpt-5.6-terra": (2.50, 0.25, 15.00),
    "openai:gpt-5.6-luna": (1.00, 0.10, 6.00),
    "openai:gpt-5.5": (5.00, 0.50, 30.00),
    "openai:gpt-5.4": (2.50, 0.25, 15.00),
    "openai:gpt-5.4-mini": (0.75, 0.075, 4.50),
    "openai:gpt-5.3-codex": (1.75, 0.175, 14.00),
    "openai:gpt-5.3-codex-spark": (1.75, 0.175, 14.00),
    # OpenAI - legacy codex-era models a fleet may have recorded
    "openai:gpt-5.2": (1.75, 0.175, 14.00),
    "openai:gpt-5.2-codex": (1.75, 0.175, 14.00),
    "openai:gpt-5.1": (1.25, 0.125, 10.00),
    "openai:gpt-5.1-codex": (1.25, 0.125, 10.00),
    "openai:gpt-5.1-codex-max": (1.25, 0.125, 10.00),
    "openai:gpt-5.1-codex-mini": (0.25, 0.025, 2.00),
    "openai:gpt-5": (1.25, 0.125, 10.00),
    "openai:gpt-5-codex": (1.25, 0.125, 10.00),
    "openai:codex-mini-latest": (1.50, 0.375, 6.00),
    # OpenAI - older API models
    "openai:gpt-4o": (2.50, 1.25, 10.0),
    "openai:gpt-4o-mini": (0.15, 0.075, 0.60),
    "openai:gpt-4.1": (2.0, 0.50, 8.0),
    "openai:gpt-4.1-mini": (0.40, 0.10, 1.60),
    "openai:o3": (2.0, 0.50, 8.0),
    "openai:o4-mini": (1.10, 0.275, 4.40),
    # Google (implicit cache read = 25% of input)
    "google:gemini-2.5-pro": (1.25, 0.3125, 10.0),
    "google:gemini-2.5-flash": (0.15, 0.0375, 0.60),
}

# Per-image pricing for image generation models.
IMAGE_PRICE_TABLE: dict[str, float] = {
    "openai:gpt-image-1": 0.04,  # 1024x1024 standard
    "openai:dall-e-3": 0.04,
    "google:imagen-3.0-generate-002": 0.03,
}


def estimate_cost(provider: str, model: str,
                  input_tokens: int = 0, output_tokens: int = 0,
                  cached_input_tokens: int = 0) -> float:
    """Estimate cost from the price table. Returns 0.0 if not in the table.

    ``cached_input_tokens`` is the cached SUBSET of ``input_tokens`` (the
    provider convention), priced at the per-model cached rate; the remainder
    bills at the full input rate. ``output_tokens`` already includes any
    reasoning tokens - callers must not add those separately.
    """
    key = f"{provider}:{model}"
    prices = PRICE_TABLE.get(key)
    if not prices:
        return 0.0
    input_price, cached_price, output_price = prices
    cached = min(max(cached_input_tokens, 0), max(input_tokens, 0))
    uncached = max(input_tokens, 0) - cached
    return (uncached * input_price + cached * cached_price
            + max(output_tokens, 0) * output_price) / 1_000_000


@dataclass
class CostSummary:
    """Aggregated cost data for display.

    ``total_cost_usd`` and the ``by_*`` maps hold PROVIDER-REPORTED dollars
    only. Usage with no reported dollars (the codex brain records token counts
    only - #760) is estimated at fold time from :data:`PRICE_TABLE` into the
    separate ``estimated_*`` fields, never mixed into the recorded figures:
    an estimate is honest only while it is distinguishable from a bill.
    ``tokens_by_model`` carries the raw token volumes for every model with
    usage - the display fallback when no defensible estimate exists (model not
    in the table, or history recorded before the cached/uncached split).
    """
    total_cost_usd: float = 0.0
    by_provider: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, float] = field(default_factory=dict)
    by_session: dict[str, float] = field(default_factory=dict)
    by_role: dict[str, float] = field(default_factory=dict)
    sessions_counted: int = 0
    estimated_cost_usd: float = 0.0
    estimated_by_model: dict[str, float] = field(default_factory=dict)
    tokens_by_model: dict[str, dict] = field(default_factory=dict)

    def to_dict(self, *, ndigits: int = 4) -> dict:
        """A JSON-ready view: costs rounded, dict order highest-spend first.

        Shared by both webapp runtimes (the observability spend panel) so the
        wire shape is defined once here rather than in each ``TeamRuntime``
        implementation. The estimated/token fields are additive (#760); the
        pre-existing keys are byte-stable for older consumers."""
        def ranked(d: dict[str, float]) -> dict[str, float]:
            return {k: round(v, ndigits)
                    for k, v in sorted(d.items(), key=lambda kv: kv[1],
                                       reverse=True)}

        tokens = {k: dict(v)
                  for k, v in sorted(
                      self.tokens_by_model.items(),
                      key=lambda kv: (kv[1].get("input_tokens", 0)
                                      + kv[1].get("output_tokens", 0)),
                      reverse=True)}
        return {
            "total_cost_usd": round(self.total_cost_usd, ndigits),
            "sessions_counted": self.sessions_counted,
            "by_provider": ranked(self.by_provider),
            "by_model": ranked(self.by_model),
            "by_session": ranked(self.by_session),
            "by_role": ranked(self.by_role),
            "estimated_cost_usd": round(self.estimated_cost_usd, ndigits),
            "estimated_by_model": ranked(self.estimated_by_model),
            "tokens_by_model": tokens,
        }


def _tok(v) -> int:
    """A token count usable in arithmetic. isinstance (not just ``or 0``) for
    the same reason the cost fold coerces null: a hand-edited state.json can
    carry a string count, and the fold backs a web endpoint that must not 500
    on one malformed session."""
    return v if isinstance(v, int) else 0


def rollup_costs(sessions_dir: Path, group_by: str = "provider") -> CostSummary:
    """Aggregate costs across all session state files.

    Args:
        sessions_dir: Path to <run>/state/sessions/
        group_by: One of "provider", "model", "session", "role"
    """
    summary = CostSummary()

    if not sessions_dir.exists():
        return summary

    for d in sessions_dir.iterdir():
        if not d.is_dir():
            continue
        state_file = d / "state.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # `or 0.0` (not a get default) so a present-but-null cost - a
        # hand-edited or partially-written state.json - coerces to 0.0
        # instead of crashing the arithmetic below (these fold now backs a
        # web endpoint that must not 500 on one malformed session).
        cost = data.get("total_cost_usd") or 0.0
        model_usage = data.get("model_usage") or {}
        if not cost and not model_usage:
            continue

        summary.sessions_counted += 1
        summary.total_cost_usd += cost

        name = data.get("name", d.name)
        role = data.get("role", "unknown")
        summary.by_session[name] = summary.by_session.get(name, 0.0) + cost
        summary.by_role[role] = summary.by_role.get(role, 0.0) + cost

        for key, usage in model_usage.items():
            usage = usage or {}
            usage_cost = usage.get("cost_usd") or 0.0
            # key format is "provider:model"
            parts = key.split(":", 1)
            provider = parts[0] if len(parts) > 1 else "unknown"
            model = parts[1] if len(parts) > 1 else key

            summary.by_provider[provider] = (
                summary.by_provider.get(provider, 0.0) + usage_cost
            )
            summary.by_model[key] = (
                summary.by_model.get(key, 0.0) + usage_cost
            )

            input_tokens = _tok(usage.get("input_tokens"))
            output_tokens = _tok(usage.get("output_tokens"))
            cached_tokens = _tok(usage.get("cached_input_tokens"))
            if input_tokens or output_tokens:
                t = summary.tokens_by_model.setdefault(
                    key, {"input_tokens": 0, "cached_input_tokens": 0,
                          "output_tokens": 0})
                t["input_tokens"] += input_tokens
                t["cached_input_tokens"] += cached_tokens
                t["output_tokens"] += output_tokens
            # Estimate dollars only where honesty allows (#760): the model
            # reported no cost, the entry carries the cached/uncached split
            # (its absence marks pre-split history, where cached tokens were
            # folded into input_tokens at full weight - pricing those would
            # overestimate ~10x on cache-heavy turns), and the model is in
            # the table (unknown models render as token volume, not $0).
            if (not usage_cost and "cached_input_tokens" in usage
                    and (input_tokens or output_tokens)):
                est = estimate_cost(provider, model,
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    cached_input_tokens=cached_tokens)
                if est:
                    summary.estimated_cost_usd += est
                    summary.estimated_by_model[key] = (
                        summary.estimated_by_model.get(key, 0.0) + est
                    )

        # If no model_usage but has cost, attribute to provider from entry
        if not model_usage and cost > 0:
            provider = data.get("provider", "anthropic") or "anthropic"
            model_key = f"{provider}:{data.get('model', 'unknown')}"
            summary.by_provider[provider] = (
                summary.by_provider.get(provider, 0.0) + cost
            )
            summary.by_model[model_key] = (
                summary.by_model.get(model_key, 0.0) + cost
            )

    return summary


def format_costs(summary: CostSummary, group_by: str = "provider") -> str:
    """Format a CostSummary for CLI display."""
    lines = []
    lines.append(f"Total cost: ${summary.total_cost_usd:.4f}")
    if summary.estimated_cost_usd:
        lines.append(f"Estimated:  ~${summary.estimated_cost_usd:.4f}"
                     "  (token usage priced at list rates, not billed)")
    lines.append(f"Sessions:   {summary.sessions_counted}")
    lines.append("")

    if group_by == "provider":
        data = summary.by_provider
        label = "Provider"
    elif group_by == "model":
        data = summary.by_model
        label = "Model"
    elif group_by == "session":
        data = summary.by_session
        label = "Session"
    elif group_by == "role":
        data = summary.by_role
        label = "Role"
    else:
        data = summary.by_provider
        label = "Provider"

    if data:
        lines.append(f"By {label.lower()}:")
        for key in sorted(data, key=data.get, reverse=True):
            cost = data[key]
            pct = (cost / summary.total_cost_usd * 100) if summary.total_cost_usd else 0
            lines.append(f"  {key:40s} ${cost:>10.4f}  ({pct:5.1f}%)")
    else:
        lines.append(f"No cost breakdown available by {label.lower()}.")

    return "\n".join(lines)
