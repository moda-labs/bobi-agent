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

# Captured from https://developers.openai.com/api/docs/pricing.md on 2026-10-26:
# the whole Standard section (both of its tables) and the start of the Batch
# section after it, verbatim except that blank lines are trimmed and collapsed.
OPENAI = """
Standard

### Standard pricing data

| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-astra | $10.00 | $50.00 | $1.00 | $12.50 | $25.00 | $20.00 | $75.00 | $2.00 | $25.00 | $50.00 |
| gpt-6.1-sol | $2.00 | $10.00 | $0.10 | $2.50 | $5.00 | $4.00 | $15.00 | $0.20 | $5.00 | $10.00 |
| gpt-6-luna | $0.10 | $0.50 | $0.01 | $0.125 | $0.25 | $0.20 | $0.75 | $0.02 | $0.25 | $0.50 |
| gpt-6-sol | $2.00 | $10.00 | $0.20 | $2.50 | $5.00 | $4.00 | $15.00 | $0.40 | $5.00 | $10.00 |
| gpt-5.6-sol | $4.00 | $20.00 | $0.40 | $5.00 | $10.00 | $8.00 | $30.00 | $0.80 | $10.00 | $20.00 |
| gpt-5.6-terra | $2.00 | $12.00 | $0.20 | $2.50 | $5.00 | $4.00 | $18.00 | $0.40 | $5.00 | $10.00 |
| gpt-5.6-luna | $0.20 | $1.20 | $0.02 | $0.25 | $0.50 | $0.40 | $1.80 | $0.04 | $0.50 | $1.00 |
| gpt-5.5 (<272K context length) | $5.00 | $30.00 | $0.50 | - | - | $10.00 | $45.00 | $1.00 | - | - |
| gpt-5.5-pro (<272K context length) | $30.00 | $180.00 | - | - | - | $60.00 | $270.00 | - | - | - |
| gpt-5.4 (<272K context length) | $2.50 | $15.00 | $0.25 | - | - | $5.00 | $22.50 | $0.50 | - | - |
| gpt-5.4-pro (<272K context length) | $30.00 | $180.00 | - | - | - | $60.00 | $270.00 | - | - | - |

| Model | Input | Output | Cached input |
| --- | --- | --- | --- |
| gpt-5.4-mini | $0.75 | $4.50 | $0.075 |
| gpt-5.4-nano | $0.20 | $1.25 | $0.02 |
| gpt-5.2 | $1.75 | $14.00 | $0.175 |
| gpt-5.2-pro | $21.00 | $168.00 | - |
| gpt-5.1 | $1.25 | $10.00 | $0.125 |
| gpt-5 | $1.25 | $10.00 | $0.125 |
| gpt-5-mini | $0.25 | $2.00 | $0.025 |
| gpt-5-nano | $0.05 | $0.40 | $0.005 |
| gpt-5-pro | $15.00 | $120.00 | - |
| gpt-4.1 | $2.00 | $8.00 | $0.50 |
| gpt-4.1-mini | $0.40 | $1.60 | $0.10 |
| gpt-4.1-nano | $0.10 | $0.40 | $0.025 |
| gpt-4o | $2.50 | $10.00 | $1.25 |
| gpt-4o-2024-05-13 | $5.00 | $15.00 | - |
| gpt-4o-mini | $0.15 | $0.60 | $0.075 |
| o1 | $15.00 | $60.00 | $7.50 |
| o1-pro | $150.00 | $600.00 | - |
| o3-pro | $20.00 | $80.00 | - |
| o3 | $2.00 | $8.00 | $0.50 |
| o4-mini | $1.10 | $4.40 | $0.275 |
| o3-mini | $1.10 | $4.40 | $0.55 |
| gpt-4-turbo-2024-04-09 | $10.00 | $30.00 | - |
| gpt-4-0613 | $30.00 | $60.00 | - |
| gpt-3.5-turbo | $0.50 | $1.50 | - |
| gpt-3.5-turbo-0125 | $0.50 | $1.50 | - |

Batch

### Batch pricing data

#### Batch

| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-astra | $5.00 | $25.00 | $0.50 | $6.25 | $12.50 | $10.00 | $37.50 | $1.00 | $12.50 | $25.00 |
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


def test_openai_page_reads_every_standard_table_and_both_cache_writes():
    prices = update_prices.parse_openai(OPENAI)
    # Short-context rates; the 30m write is cache_write_5m and the 12h write cache_write_1h.
    # The batch table after the section (input $5.00) is not mixed in.
    assert prices["openai:gpt-6-astra"] == {
        "input": 10.0, "cache_read": 1.0, "cache_write_5m": 12.5, "cache_write_1h": 25.0, "output": 50.0}
    # The context note is dropped, and "-" means no discount or surcharge.
    assert prices["openai:gpt-5.4"] == {
        "input": 2.5, "cache_read": 0.25, "cache_write_5m": 2.5, "cache_write_1h": 2.5, "output": 15.0}
    assert prices["openai:gpt-5.4-pro"]["cache_read"] == 30.0
    # The second table has no cache-write columns: writes bill as plain input.
    assert prices["openai:gpt-5"] == {
        "input": 1.25, "cache_read": 0.125, "cache_write_5m": 1.25, "cache_write_1h": 1.25, "output": 10.0}
    assert prices["openai:o1-pro"]["cache_read"] == 150.0
    # Every row of both tables.
    assert len(prices) == 36
    # A sub-heading inside the section does not end it.
    assert update_prices.parse_openai(OPENAI.replace("pricing data\n", "pricing data\n\n#### Standard\n", 1)) == prices


@pytest.mark.parametrize("old, new", [
    # A cache-write retention the price file has no field for.
    ("Short context 12h write", "Short context 24h write"),
    # Renamed cache-write columns: billing those writes as plain input would hide the surcharge.
    (" write |", " cache |"),
    # A row whose cells no longer line up with the header.
    ("| gpt-5 | $1.25 |", "| gpt-5 | $1.25 | $1.25 |"),
    # One model at two prices.
    ("| gpt-5.4-mini |", "| gpt-5.4 |"),
])
def test_openai_tables_that_cannot_be_read_safely_are_refused(old, new):
    page = OPENAI.replace(old, new)
    assert page != OPENAI
    with pytest.raises(ValueError, match="layout may have changed"):
        update_prices.parse_openai(page)


def test_build_refuses_a_page_that_no_longer_parses():
    with pytest.raises(ValueError, match="layout may have changed"):
        update_prices.build(ANTHROPIC, "### Standard pricing data\n\n| Model | Short context input |\n| --- | --- |\n")
    # Each OpenAI table holds a required model, so losing either table is a layout change too.
    tiered, plain = OPENAI.split("| Model | Input |", 1)
    for page in (tiered, "### Standard pricing data\n\n| Model | Input |" + plain):
        with pytest.raises(ValueError, match="layout may have changed"):
            update_prices.build(ANTHROPIC, page)


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


def test_a_model_dropped_from_the_page_keeps_its_last_price(tmp_path, monkeypatch):
    anthropic, openai = tmp_path / "a.md", tmp_path / "o.md"
    anthropic.write_text(ANTHROPIC)
    openai.write_text(OPENAI)
    retired = {"input": 15.0, "cache_read": 1.5, "cache_write_5m": 18.75,
               "cache_write_1h": 30.0, "output": 75.0}
    stale_sonnet = {**retired, "input": 99.0}
    output = tmp_path / "model_prices.json"
    output.write_text(json.dumps({"models": {
        "anthropic:claude-3-opus": retired,
        "anthropic:claude-sonnet-4-5": stale_sonnet,
        "openai:malformed": {"input": "free"},
    }}))
    monkeypatch.setattr(update_prices, "OUTPUT", output)

    assert update_prices.main(["--anthropic", str(anthropic), "--openai", str(openai)]) == 0

    models = json.loads(output.read_text())["models"]
    # Gone from the page: kept at its last price, not dropped or repriced.
    assert models["anthropic:claude-3-opus"] == retired
    # Still on the page: the page wins.
    assert models["anthropic:claude-sonnet-4-5"]["input"] == 3.0
    # A malformed entry is not carried over.
    assert "openai:malformed" not in models
    assert list(models) == sorted(models)
