"""Tests for cost attribution and rollup."""

import json
from pathlib import Path

import pytest

from bobi.costs import (
    CostSummary,
    estimate_cost,
    format_costs,
    rollup_costs,
)


class TestPriceTable:
    def test_known_model(self):
        # claude-sonnet-4: $3/M input, $15/M output
        cost = estimate_cost("anthropic", "claude-sonnet-4-20250514",
                             input_tokens=1_000_000, output_tokens=100_000)
        assert abs(cost - 4.5) < 0.01  # 3.0 + 1.5

    def test_unknown_model(self):
        cost = estimate_cost("unknown", "mystery-model",
                             input_tokens=1000, output_tokens=500)
        assert cost == 0.0

    def test_zero_tokens(self):
        cost = estimate_cost("anthropic", "claude-sonnet-4-20250514")
        assert cost == 0.0


class TestCostRollup:
    def _make_sessions(self, tmp_path, sessions):
        """Create session state files for testing."""
        sessions_dir = tmp_path / "sessions"
        for name, data in sessions.items():
            d = sessions_dir / name
            d.mkdir(parents=True)
            (d / "state.json").write_text(json.dumps(data))
        return sessions_dir

    def test_empty_sessions(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        summary = rollup_costs(sessions_dir)
        assert summary.total_cost_usd == 0.0
        assert summary.sessions_counted == 0

    def test_nonexistent_dir(self, tmp_path):
        summary = rollup_costs(tmp_path / "nonexistent")
        assert summary.total_cost_usd == 0.0

    def test_single_session(self, tmp_path):
        sessions_dir = self._make_sessions(tmp_path, {
            "eng-42-implement": {
                "name": "eng-42-implement",
                "role": "engineer",
                "total_cost_usd": 0.25,
                "model": "claude-sonnet-4-20250514",
                "provider": "anthropic",
                "model_usage": {
                    "anthropic:claude-sonnet-4-20250514": {
                        "cost_usd": 0.25,
                        "input_tokens": 50000,
                        "output_tokens": 10000,
                    }
                },
            }
        })
        summary = rollup_costs(sessions_dir)
        assert summary.total_cost_usd == 0.25
        assert summary.sessions_counted == 1
        assert summary.by_provider["anthropic"] == 0.25
        assert "anthropic:claude-sonnet-4-20250514" in summary.by_model
        assert summary.by_role["engineer"] == 0.25

    def test_multi_provider(self, tmp_path):
        sessions_dir = self._make_sessions(tmp_path, {
            "eng-1": {
                "name": "eng-1",
                "role": "engineer",
                "total_cost_usd": 0.50,
                "model_usage": {
                    "anthropic:claude-sonnet-4-20250514": {
                        "cost_usd": 0.30,
                        "input_tokens": 60000,
                        "output_tokens": 12000,
                    },
                    "openai:gpt-image-1": {
                        "cost_usd": 0.20,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    },
                },
            }
        })
        summary = rollup_costs(sessions_dir)
        assert summary.total_cost_usd == 0.50
        assert summary.by_provider["anthropic"] == 0.30
        assert summary.by_provider["openai"] == 0.20

    def test_no_model_usage_fallback(self, tmp_path):
        """Sessions with cost but no model_usage get attributed to their provider."""
        sessions_dir = self._make_sessions(tmp_path, {
            "legacy": {
                "name": "legacy",
                "role": "manager",
                "total_cost_usd": 1.00,
                "provider": "anthropic",
                "model": "claude-opus-4-20250514",
            }
        })
        summary = rollup_costs(sessions_dir)
        assert summary.total_cost_usd == 1.00
        assert summary.by_provider["anthropic"] == 1.00

    def test_skips_zero_cost_sessions(self, tmp_path):
        sessions_dir = self._make_sessions(tmp_path, {
            "no-cost": {
                "name": "no-cost",
                "role": "monitor",
                "total_cost_usd": 0.0,
            }
        })
        summary = rollup_costs(sessions_dir)
        assert summary.sessions_counted == 0


class TestFormatCosts:
    def test_by_provider(self):
        summary = CostSummary(
            total_cost_usd=1.50,
            by_provider={"anthropic": 1.00, "openai": 0.50},
            sessions_counted=3,
        )
        output = format_costs(summary, group_by="provider")
        assert "Total cost: $1.5000" in output
        assert "Sessions:   3" in output
        assert "anthropic" in output
        assert "openai" in output

    def test_by_role(self):
        summary = CostSummary(
            total_cost_usd=2.00,
            by_role={"engineer": 1.50, "manager": 0.50},
            sessions_counted=5,
        )
        output = format_costs(summary, group_by="role")
        assert "engineer" in output
        assert "manager" in output

    def test_empty(self):
        summary = CostSummary()
        output = format_costs(summary)
        assert "Total cost: $0.0000" in output


class TestToDict:
    def test_shape_and_rounding(self):
        summary = CostSummary(
            total_cost_usd=1.234567,
            by_provider={"anthropic": 1.234567},
            by_model={"anthropic:claude-opus": 1.234567},
            by_session={"manager": 1.234567},
            by_role={"director": 1.234567},
            sessions_counted=2,
        )
        d = summary.to_dict()
        assert d["total_cost_usd"] == 1.2346          # rounded to 4 places
        assert d["sessions_counted"] == 2
        assert d["by_provider"] == {"anthropic": 1.2346}
        assert d["by_model"] == {"anthropic:claude-opus": 1.2346}
        assert d["by_session"] == {"manager": 1.2346}
        assert d["by_role"] == {"director": 1.2346}

    def test_breakdowns_ranked_highest_first(self):
        summary = CostSummary(
            by_model={"a": 0.10, "b": 0.90, "c": 0.30},
        )
        assert list(summary.to_dict()["by_model"]) == ["b", "c", "a"]

    def test_empty_summary(self):
        d = CostSummary().to_dict()
        assert d["total_cost_usd"] == 0.0
        assert d["sessions_counted"] == 0
        assert d["by_model"] == {}
