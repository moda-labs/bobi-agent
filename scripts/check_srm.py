#!/usr/bin/env python3
"""Calculate the chi-square sample-ratio-mismatch gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bobi.metrics.experiment import sample_ratio_mismatch


def _numbers(value: str, cast):
    try:
        return [cast(item.strip()) for item in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", required=True)
    parser.add_argument("--observed", required=True)
    parser.add_argument("--max-p-value", type=float, default=0.001)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = sample_ratio_mismatch(
        _numbers(args.observed, int),
        _numbers(args.expected, float),
        alpha=args.max_p_value,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n")
    if not report["detected"]:
        raise SystemExit("sample ratio mismatch was not detected")


if __name__ == "__main__":
    main()
