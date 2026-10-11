"""Tests for scripts/update_prices.py: parsing the official pricing pages."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "update_prices.py"
spec = importlib.util.spec_from_file_location("update_prices", SCRIPT)
update_prices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update_prices)

ANTHROPIC = """
## Model pricing

| Model | Base Input Tokens | 5m Cache Writes | 1h Cache Writes | Cache Hits & Refreshes | Output Tokens |
| --- | --- | --- | --- | --- | --- |
| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok<sup>2</sup> | $20 / MTok |
| Claude Opus 4.5 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.50 / MTok | $25 / MTok |
| Claude Sonnet 4.5 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.30 / MTok | $15 / MTok |
| Claude Opus 4 ([retired, except on Google Cloud](https://example.com)) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |
| Claude Haiku 5.5 (for prompts up to 100,000 tokens) | $0.10 / MTok | $0.125 / MTok | $0.20 / MTok | $0.01 / MTok | $0.50 / MTok |
| Claude Haiku 5.5 (for prompts over 100,000 tokens) | $0.50 / MTok | $0.625 / MTok | $1 / MTok | $0.05 / MTok | $2.50 / MTok |
| Claude Haiku 4.5 | $1 / MTok | $1.25 / MTok | $2 / MTok | $0.10 / MTok | $5 / MTok |

## Batch processing

| Model | Batch input | Batch output |
| --- | --- | --- |
| Claude Opus 5.5 | $2 / MTok | $10 / MTok |
"""

OPENAI = """
### Standard pricing data

| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-5.6-sol | $4.00 | $0.40 | $5.00 | $20.00 | $8.00 | $0.80 | $10.00 | $30.00 |
| gpt-5.4 (<272K context length) | $2.50 | $0.25 | - | $15.00 | $5.00 | $0.50 | - | $22.50 |
| gpt-5 | $1.25 | $0.125 | - | $10.00 | - | - | - | - |
| o1-pro | $150.00 | - | - | $600.00 | - | - | - | - |
| o3 | $2.00 | $0.50 | - | $8.00 | - | - | - | - |

### Batch pricing data

| Model | Short context input | Short context cached input | Short context cache writes | Short context output |
| --- | --- | --- | --- | --- |
| gpt-5.6-sol | $2.00 | $0.20 | $2.50 | $10.00 |
"""


def test_anthropic_page_parses_every_rate_and_keeps_the_base_tier():
    prices = update_prices.parse_anthropic(ANTHROPIC)
    assert prices["anthropic:claude-opus-5-5"] == {
        "input": 4.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0, "cache_read": 0.2, "output": 20.0}
    # Retired models keep their price for historical usage; tiered rows keep the base tier.
    assert prices["anthropic:claude-opus-4"]["input"] == 15.0
    assert prices["anthropic:claude-haiku-5-5"]["input"] == 0.10
    # The batch table is not mixed in.
    assert len(prices) == 6


def test_openai_page_reads_the_standard_short_context_columns():
    prices = update_prices.parse_openai(OPENAI)
    assert prices["openai:gpt-5.6-sol"] == {
        "input": 4.0, "cache_read": 0.4, "cache_write_5m": 5.0, "cache_write_1h": 5.0, "output": 20.0}
    # The context note is dropped, and "-" means no discount or surcharge.
    assert prices["openai:gpt-5.4"]["cache_write_5m"] == 2.5
    assert prices["openai:o1-pro"]["cache_read"] == 150.0
    assert "openai:gpt-5.6-sol" in prices and len(prices) == 5


def test_build_refuses_a_page_that_no_longer_parses():
    with pytest.raises(ValueError, match="layout may have changed"):
        update_prices.build(ANTHROPIC, "### Standard pricing data\n\n| Model | Short context input |\n| --- | --- |\n")


def test_check_mode_reports_a_stale_file_without_writing(tmp_path, monkeypatch):
    anthropic, openai = tmp_path / "a.md", tmp_path / "o.md"
    anthropic.write_text(ANTHROPIC)
    openai.write_text(OPENAI)
    output = tmp_path / "model_prices.json"
    output.write_text(json.dumps({"models": {}}))
    monkeypatch.setattr(update_prices, "OUTPUT", output)
    argv = ["--anthropic", str(anthropic), "--openai", str(openai)]
    assert update_prices.main([*argv, "--check"]) == 1
    assert json.loads(output.read_text()) == {"models": {}}
    assert update_prices.main(argv) == 0
    written = json.loads(output.read_text())
    assert written["models"]["anthropic:claude-opus-5-5"]["cache_write_1h"] == 8.0
    assert update_prices.main([*argv, "--check"]) == 0
