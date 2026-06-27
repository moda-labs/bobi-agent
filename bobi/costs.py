"""Cost attribution and rollup for multi-model sessions.

Provides a price table for providers that don't return cost directly,
and rollup functions for the `bobi costs` CLI command.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Fallback price per million tokens (input, output) by provider:model.
# Used only when the provider's API response doesn't include cost directly
# (e.g. non-Anthropic providers via connections). These are approximate
# list prices as of 2025-06 and should be updated periodically — the
# authoritative source of cost is always the provider's own billing.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    # Anthropic
    "anthropic:claude-sonnet-4-20250514": (3.0, 15.0),
    "anthropic:claude-opus-4-20250514": (15.0, 75.0),
    "anthropic:claude-haiku-3-5-20241022": (0.80, 4.0),
    # OpenAI
    "openai:gpt-4o": (2.50, 10.0),
    "openai:gpt-4o-mini": (0.15, 0.60),
    "openai:gpt-4.1": (2.0, 8.0),
    "openai:gpt-4.1-mini": (0.40, 1.60),
    "openai:o3": (2.0, 8.0),
    "openai:o4-mini": (1.10, 4.40),
    # Google
    "google:gemini-2.5-pro": (1.25, 10.0),
    "google:gemini-2.5-flash": (0.15, 0.60),
}

# Per-image pricing for image generation models.
IMAGE_PRICE_TABLE: dict[str, float] = {
    "openai:gpt-image-1": 0.04,  # 1024x1024 standard
    "openai:dall-e-3": 0.04,
    "google:imagen-3.0-generate-002": 0.03,
}


def estimate_cost(provider: str, model: str,
                  input_tokens: int = 0, output_tokens: int = 0) -> float:
    """Estimate cost from the price table. Returns 0.0 if not in the table."""
    key = f"{provider}:{model}"
    prices = PRICE_TABLE.get(key)
    if not prices:
        return 0.0
    input_price, output_price = prices
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


@dataclass
class CostSummary:
    """Aggregated cost data for display."""
    total_cost_usd: float = 0.0
    by_provider: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, float] = field(default_factory=dict)
    by_session: dict[str, float] = field(default_factory=dict)
    by_role: dict[str, float] = field(default_factory=dict)
    sessions_counted: int = 0


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

        cost = data.get("total_cost_usd", 0.0)
        if not cost and not data.get("model_usage"):
            continue

        summary.sessions_counted += 1
        summary.total_cost_usd += cost

        name = data.get("name", d.name)
        role = data.get("role", "unknown")
        summary.by_session[name] = summary.by_session.get(name, 0.0) + cost
        summary.by_role[role] = summary.by_role.get(role, 0.0) + cost

        model_usage = data.get("model_usage", {})
        for key, usage in model_usage.items():
            usage_cost = usage.get("cost_usd", 0.0)
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
