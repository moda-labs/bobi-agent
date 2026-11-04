#!/usr/bin/env python3
"""Refresh bobi/model_prices.json from the official Anthropic and OpenAI pricing pages.

Both vendors publish their pricing page as Markdown; this reads the standard
(non-batch) per-model tables from each and writes USD per million tokens:

    input, cache_read, cache_write_5m, cache_write_1h, output

Usage:
    python3 scripts/update_prices.py            # fetch and rewrite the file
    python3 scripts/update_prices.py --check    # exit 1 when the file is stale
    python3 scripts/update_prices.py --anthropic page.md --openai page.md

The script refuses to write when a page no longer parses into the expected
table (a layout change), so a broken page never empties the price list. A
model that drops off a page keeps its last recorded price: historical usage
of a retired model must not lose its rate or be repriced.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

ANTHROPIC_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
OPENAI_URL = "https://developers.openai.com/api/docs/pricing.md"
OUTPUT = Path(__file__).resolve().parent.parent / "bobi" / "model_prices.json"
# A parse that misses any of these means the page layout changed. Each of the
# two OpenAI standard tables holds one: gpt-5.4 the tiered one, gpt-5 and o3 the plain one.
REQUIRED = ("anthropic:claude-sonnet-4-5", "anthropic:claude-haiku-4-5", "anthropic:claude-opus-4-5",
            "openai:gpt-5.4", "openai:gpt-5", "openai:o3")


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "bobi-price-updater"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def _tables(markdown: str) -> list[list[list[str]]]:
    """Every Markdown table as rows of stripped cells (separator rows dropped)."""
    tables, current = [], []
    for line in markdown.splitlines():
        if line.lstrip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _usd(cell: str) -> float | None:
    """'$0.20 / MTok<sup>2</sup>' -> 0.2; '-' -> None."""
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", re.sub(r"<[^>]+>", "", cell))
    return float(match.group(1)) if match else None


def _rows(table: list[list[str]]) -> list[list[str]]:
    """A table's data rows; a row whose cells do not line up with the header cannot be read."""
    for row in table[1:]:
        if len(row) != len(table[0]):
            raise ValueError(f"pricing table row does not match its header; layout may have changed: {row}")
    return table[1:]


def _column(header: list[str], *words: str) -> int:
    for index, name in enumerate(header):
        if all(word in name.lower() for word in words):
            return index
    raise ValueError(f"pricing table has no column matching {words}; layout may have changed: {header}")


def parse_anthropic(markdown: str) -> dict[str, dict[str, float]]:
    """The model table: base input, 5m and 1h cache writes, cache hits, output."""
    table = next((t for t in _tables(markdown) if any("base input" in cell.lower() for cell in t[0])), None)
    if table is None:
        raise ValueError("Anthropic page has no model pricing table")
    header, rows = table[0], _rows(table)
    columns = {"input": _column(header, "base input"), "cache_write_5m": _column(header, "5m"),
               "cache_write_1h": _column(header, "1h"), "cache_read": _column(header, "hit"),
               "output": _column(header, "output")}
    prices = {}
    for row in rows:
        # Drop markup and links ("Claude Opus 4 ([retired](https://...))") first.
        label = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", re.sub(r"<[^>]+>", "", row[0]))
        # Tiered rows ("for prompts over 100,000 tokens") keep the base tier only.
        if re.search(r"\bover\b", label, re.I):
            continue
        name = re.sub(r"\s*\(.*?\)\s*", " ", label).strip().lower()
        match = re.fullmatch(r"claude ([a-z]+) (\d+(?:\.\d+)?)", name)
        if not match:
            continue
        model = f"claude-{match.group(1)}-{match.group(2).replace('.', '-')}"
        values = {key: _usd(row[index]) for key, index in columns.items()}
        if None in values.values():
            continue
        prices[f"anthropic:{model}"] = values
    return prices


# OpenAI column labels, without the "Short context " prefix of tiered tables.
# OpenAI sells cache writes at two retentions and the price file has a field
# for each, named for Anthropic's: the 30-minute write is cache_write_5m and the
# 12-hour write cache_write_1h. bobi/costs.py bills a write at cache_write_1h
# only when usage reports it as a 1-hour write, which Codex usage never does,
# so Codex cache writes cost the 30-minute rate.
OPENAI_RATES = {"input": "input", "cache_read": "cached input", "output": "output"}
OPENAI_WRITES = {"cache_write_5m": "30m write", "cache_write_1h": "12h write"}


def parse_openai(markdown: str) -> dict[str, dict[str, float]]:
    """Every table of the standard (not batch) section, at short-context rates.

    Models with a long-context tier are listed with "Short context ..." and
    "Long context ..." columns, including both cache writes; the rest sit in a
    plain Input / Output / Cached input table with no cache-write surcharge.
    A table with any other columns is a layout change."""
    section = markdown.split("### Standard pricing data", 1)
    if len(section) < 2:
        raise ValueError("OpenAI page has no standard pricing table")
    plain, tiered = {*OPENAI_RATES.values()}, {*OPENAI_RATES.values(), *OPENAI_WRITES.values()}
    prices = {}
    # The section ends at the next heading of its level or above; the batch, flex and fast tables follow it.
    for table in _tables(re.split(r"^#{1,3} ", section[1], maxsplit=1, flags=re.M)[0]):
        header, rows = table[0], _rows(table)
        # Exact labels: "input" is also a word of "cached input". Long-context columns are not read.
        labels = [re.sub(r"^short context ", "", name.lower()) for name in header]
        if {label for label in labels[1:] if not label.startswith("long context ")} not in (plain, tiered):
            raise ValueError(f"OpenAI pricing table has unexpected columns; layout may have changed: {header}")
        columns = {key: labels.index(label) for key, label in {**OPENAI_RATES, **OPENAI_WRITES}.items()
                   if label in labels}
        for row in rows:
            model = re.sub(r"\s*\(.*?\)\s*", "", row[0]).strip().lower()
            values = {key: _usd(row[index]) for key, index in columns.items()}
            if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", model) or values["input"] is None or values["output"] is None:
                continue
            # A "-" or a missing column means no discount (cached reads) or no surcharge (cache writes).
            entry = {key: values["input"] if values.get(key) is None else values[key]
                     for key in (*OPENAI_RATES, *OPENAI_WRITES)}
            if prices.setdefault(f"openai:{model}", entry) != entry:
                raise ValueError(f"OpenAI lists {model} at two different prices; layout may have changed")
    return prices


ORDER = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")


def _retained(previous: object) -> dict[str, dict[str, float]]:
    """Well-formed entries of the existing file; anything else is not carried over."""
    if not isinstance(previous, dict):
        return {}
    return {key: entry for key, entry in previous.items()
            if isinstance(key, str) and isinstance(entry, dict)
            and all(isinstance(entry.get(name), (int, float)) and not isinstance(entry.get(name), bool)
                    for name in ORDER)}


def build(anthropic_md: str, openai_md: str, previous: object = None) -> dict:
    """Prices from both pages, laid over the ``previous`` models mapping so a
    model no longer listed keeps its last price instead of disappearing."""
    parsed = {**parse_anthropic(anthropic_md), **parse_openai(openai_md)}
    missing = [key for key in REQUIRED if key not in parsed]
    if missing:
        raise ValueError(f"pricing pages parsed without {', '.join(missing)}; layout may have changed")
    models = {**_retained(previous), **parsed}
    return {"sources": {"anthropic": ANTHROPIC_URL, "openai": OPENAI_URL},
            "unit": "USD per million tokens",
            "models": {key: {name: models[key][name] for name in ORDER} for key in sorted(models)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if bobi/model_prices.json is stale")
    parser.add_argument("--anthropic", type=Path, help="read the Anthropic page from a file")
    parser.add_argument("--openai", type=Path, help="read the OpenAI page from a file")
    args = parser.parse_args(argv)
    try:
        current = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {}
        if not isinstance(current, dict):
            raise ValueError(f"{OUTPUT.name} is not a JSON object")
        data = build(args.anthropic.read_text() if args.anthropic else _fetch(ANTHROPIC_URL),
                     args.openai.read_text() if args.openai else _fetch(OPENAI_URL),
                     current.get("models"))
    except (OSError, ValueError) as error:
        print(f"update_prices: {error}", file=sys.stderr)
        return 2
    if current.get("models") == data["models"]:
        print(f"update_prices: {len(data['models'])} prices unchanged")
        return 0
    if args.check:
        print("update_prices: bobi/model_prices.json is stale; run scripts/update_prices.py", file=sys.stderr)
        return 1
    data["updated"] = datetime.date.today().isoformat()
    OUTPUT.write_text(json.dumps(data, indent=2) + "\n")
    print(f"update_prices: wrote {len(data['models'])} prices to {OUTPUT.relative_to(OUTPUT.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
