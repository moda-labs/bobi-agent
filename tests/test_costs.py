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

    def test_cached_subset_priced_at_cached_rate(self):
        # gpt-5.6: $5/M input, $0.50/M cached, $30/M output. input_tokens is
        # INCLUSIVE of cached (900K cached + 100K uncached here) - the whole
        # point of #760's split: pricing 1M cache-heavy input at the full
        # rate would fabricate ~$5 where ~$0.95 is defensible.
        cost = estimate_cost("openai", "gpt-5.6",
                             input_tokens=1_000_000,
                             output_tokens=10_000,
                             cached_input_tokens=900_000)
        # 100K * 5.0 + 900K * 0.50 + 10K * 30.0 = 0.5 + 0.45 + 0.3
        assert abs(cost - 1.25) < 0.001

    def test_cached_clamped_to_input(self):
        # A malformed entry claiming more cached than input must not go
        # negative on the uncached remainder.
        cost = estimate_cost("openai", "gpt-5.6",
                             input_tokens=100,
                             cached_input_tokens=500)
        assert abs(cost - 100 * 0.50 / 1_000_000) < 1e-9


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

    def test_unattributed_provider_cost_with_token_usage(self, tmp_path):
        """A multi-model turn can report aggregate provider dollars only.
        Tokens stay per-model; the aggregate dollars count at provider level
        without pretending either model's cost_usd was reported."""
        sessions_dir = self._make_sessions(tmp_path, {
            "multi-model": {
                "name": "multi-model",
                "role": "engineer",
                "total_cost_usd": 0.30,
                "provider": "anthropic",
                "model_usage": {
                    "anthropic:claude-opus-4-8": {
                        "cost_usd": 0.0,
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cached_input_tokens": 0,
                    },
                    "anthropic:claude-haiku-3-5-20241022": {
                        "cost_usd": 0.0,
                        "input_tokens": 5,
                        "output_tokens": 1,
                        "cached_input_tokens": 0,
                    },
                },
            }
        })
        summary = rollup_costs(sessions_dir)
        assert summary.total_cost_usd == 0.30
        assert summary.by_provider["anthropic"] == 0.30
        assert summary.estimated_cost_usd == 0.0
        assert summary.by_model["anthropic:claude-opus-4-8"] == 0.0
        assert summary.tokens_by_model[
            "anthropic:claude-opus-4-8"]["input_tokens"] == 10

    def test_null_cost_coerces_to_zero(self, tmp_path):
        # A hand-edited or partially-written state.json can carry an explicit
        # null; the fold must not crash (it now backs a web endpoint).
        sessions_dir = self._make_sessions(tmp_path, {
            "broken": {"name": "broken", "role": "eng",
                       "total_cost_usd": None,
                       "model_usage": {"anthropic:opus": {"cost_usd": None}}},
            "good": {"name": "good", "role": "eng", "total_cost_usd": 0.5,
                     "model_usage": {"anthropic:opus": {"cost_usd": 0.5}}},
        })
        summary = rollup_costs(sessions_dir)
        assert summary.total_cost_usd == 0.5
        assert summary.by_model["anthropic:opus"] == 0.5

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


class TestEstimatedRollup:
    """Fold-time dollar estimation for models that report tokens only (#760)."""

    _make_sessions = TestCostRollup._make_sessions

    def _codex_session(self, *, cached_key=True, model="gpt-5.6",
                       cost=0.0):
        usage = {"cost_usd": cost, "input_tokens": 1_000_000,
                 "output_tokens": 10_000}
        if cached_key:
            usage["cached_input_tokens"] = 900_000
        return {
            "name": "dev-1-task", "role": "dev", "total_cost_usd": cost,
            "model": model, "provider": "openai",
            "model_usage": {f"openai:{model}": usage},
        }

    def test_codex_entry_estimated(self, tmp_path):
        sessions_dir = self._make_sessions(
            tmp_path, {"dev-1-task": self._codex_session()})
        summary = rollup_costs(sessions_dir)
        # Recorded dollars stay zero - the estimate rides separate fields.
        assert summary.total_cost_usd == 0.0
        assert summary.by_model["openai:gpt-5.6"] == 0.0
        # 100K uncached * $5 + 900K cached * $0.50 + 10K out * $30 per Mtok
        assert abs(summary.estimated_cost_usd - 1.25) < 0.001
        assert abs(summary.estimated_by_model["openai:gpt-5.6"] - 1.25) < 0.001
        assert summary.tokens_by_model["openai:gpt-5.6"] == {
            "input_tokens": 1_000_000, "cached_input_tokens": 900_000,
            "output_tokens": 10_000}

    def test_legacy_entry_without_split_not_estimated(self, tmp_path):
        # Pre-split history folded cached tokens into input_tokens at full
        # weight; estimating it would overestimate ~10x on cache-heavy
        # turns. Token volume still surfaces.
        sessions_dir = self._make_sessions(
            tmp_path, {"dev-1-task": self._codex_session(cached_key=False)})
        summary = rollup_costs(sessions_dir)
        assert summary.estimated_cost_usd == 0.0
        assert summary.estimated_by_model == {}
        assert summary.tokens_by_model["openai:gpt-5.6"]["input_tokens"] == 1_000_000

    def test_unknown_model_not_estimated(self, tmp_path):
        sessions_dir = self._make_sessions(
            tmp_path, {"dev-1-task": self._codex_session(model="codex")})
        summary = rollup_costs(sessions_dir)
        assert summary.estimated_cost_usd == 0.0
        assert "openai:codex" in summary.tokens_by_model

    def test_reported_cost_never_reestimated(self, tmp_path):
        # An entry with provider-reported dollars must not ALSO contribute
        # an estimate (double counting).
        sessions_dir = self._make_sessions(
            tmp_path, {"dev-1-task": self._codex_session(cost=0.42)})
        summary = rollup_costs(sessions_dir)
        assert summary.total_cost_usd == 0.42
        assert summary.estimated_cost_usd == 0.0
        assert "openai:gpt-5.6" in summary.tokens_by_model

    def test_string_token_counts_do_not_crash_the_fold(self, tmp_path):
        # A hand-edited state.json can carry a string count; the fold backs a
        # web endpoint and must skip it (treat as 0), not 500 the whole fleet.
        s = self._codex_session()
        s["model_usage"]["openai:gpt-5.6"]["input_tokens"] = "1000000"
        sessions_dir = self._make_sessions(tmp_path, {"dev-1-task": s})
        summary = rollup_costs(sessions_dir)
        assert summary.tokens_by_model["openai:gpt-5.6"]["input_tokens"] == 0
        assert summary.tokens_by_model["openai:gpt-5.6"]["output_tokens"] == 10_000

    def test_estimates_accumulate_across_sessions(self, tmp_path):
        sessions_dir = self._make_sessions(tmp_path, {
            "dev-1-a": self._codex_session(),
            "dev-1-b": self._codex_session(),
        })
        summary = rollup_costs(sessions_dir)
        assert abs(summary.estimated_cost_usd - 2.50) < 0.001
        assert summary.tokens_by_model["openai:gpt-5.6"]["output_tokens"] == 20_000


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
        assert "Estimated" not in output  # no line when nothing is estimated

    def test_estimated_line(self):
        summary = CostSummary(total_cost_usd=1.0, estimated_cost_usd=0.25,
                              sessions_counted=1)
        output = format_costs(summary)
        assert "Estimated:  ~$0.2500" in output


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
        # Additive #760 fields are always present (value-branching wire
        # convention: null/empty, never omitted).
        assert d["estimated_cost_usd"] == 0.0
        assert d["estimated_by_model"] == {}
        assert d["tokens_by_model"] == {}

    def test_estimated_fields_rounded_and_ranked(self):
        summary = CostSummary(
            estimated_cost_usd=0.123456,
            estimated_by_model={"openai:a": 0.02, "openai:b": 0.103456},
            tokens_by_model={
                "openai:a": {"input_tokens": 10, "cached_input_tokens": 5,
                             "output_tokens": 1},
                "openai:b": {"input_tokens": 999, "cached_input_tokens": 0,
                             "output_tokens": 100},
            },
        )
        d = summary.to_dict()
        assert d["estimated_cost_usd"] == 0.1235
        assert list(d["estimated_by_model"]) == ["openai:b", "openai:a"]
        # tokens ranked by volume, values untouched (raw facts, not dollars)
        assert list(d["tokens_by_model"]) == ["openai:b", "openai:a"]
        assert d["tokens_by_model"]["openai:a"]["cached_input_tokens"] == 5
