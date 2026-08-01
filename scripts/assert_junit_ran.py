#!/usr/bin/env python3
"""Assert that a gated pytest lane actually RAN, not merely that it exited 0.

Every test this repo gates on a credential carries a ``skipif``, so a misnamed
secret, an unavailable harness, or a platform guard turns the whole lane into
"0 selected, exit 0" — green, and proving nothing. That is the exact failure
mode this repo's CI shipped for months (see plans/2026-08-01-ci-coverage.md).

Reading pytest's ``-rA`` prose to detect it is brittle. The junit XML is
machine-readable and states the counts outright, so the gate parses that:

    pytest ... --junitxml=live.xml
    python scripts/assert_junit_ran.py live.xml \
        --expect-passed 2 \
        --require test_image_ask_roundtrip \
        --require test_image_codex_ask_roundtrip

Exits non-zero — loudly, with the reason — on a missing report, on any skip,
on any failure or error, on the wrong number of passes, or when a named test
is absent from the report entirely.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


class ReportError(Exception):
    """A junit report that does not prove the lane ran."""


def _suites(root: ET.Element) -> list[ET.Element]:
    """The <testsuite> elements, whether or not wrapped in <testsuites>."""
    if root.tag == "testsuite":
        return [root]
    return list(root.iter("testsuite"))


def _int_attr(element: ET.Element, name: str) -> int:
    raw = element.get(name)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - malformed junit
        raise ReportError(f"non-integer {name!r} attribute: {raw!r}") from exc


def _passed_names(suites: list[ET.Element]) -> set[str]:
    """Names of testcases that neither skipped, failed, nor errored."""
    names: set[str] = set()
    for suite in suites:
        for case in suite.iter("testcase"):
            outcome = {child.tag for child in case}
            if outcome & {"skipped", "failure", "error"}:
                continue
            name = case.get("name")
            if name:
                names.add(name)
    return names


def check(path: Path, expect_passed: int, required: list[str]) -> None:
    """Raise ReportError unless *path* proves the lane ran as specified."""
    if not path.exists():
        raise ReportError(
            f"no junit report at {path} — the lane did not run at all "
            "(pytest never started, or --junitxml pointed elsewhere)"
        )

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ReportError(f"unparseable junit report at {path}: {exc}") from exc

    suites = _suites(root)
    if not suites:
        raise ReportError(f"junit report at {path} contains no <testsuite>")

    total = sum(_int_attr(s, "tests") for s in suites)
    skipped = sum(_int_attr(s, "skipped") for s in suites)
    failures = sum(_int_attr(s, "failures") for s in suites)
    errors = sum(_int_attr(s, "errors") for s in suites)
    passed = total - skipped - failures - errors

    problems: list[str] = []
    if total == 0:
        problems.append("0 tests were selected — the marker expression matched nothing")
    if skipped:
        problems.append(
            f"{skipped} test(s) SKIPPED — a gated lane that skips is a silent no-op "
            "(missing credential, unavailable harness, or a platform guard)"
        )
    if failures or errors:
        problems.append(f"{failures} failure(s) and {errors} error(s)")
    if passed != expect_passed:
        problems.append(f"expected exactly {expect_passed} passed, got {passed}")

    missing = [name for name in required if name not in _passed_names(suites)]
    if missing:
        problems.append("required test(s) did not pass: " + ", ".join(sorted(missing)))

    if problems:
        summary = (
            f"tests={total} passed={passed} skipped={skipped} "
            f"failures={failures} errors={errors}"
        )
        raise ReportError(f"{path}: {summary}\n  - " + "\n  - ".join(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_xml", type=Path, help="path to the --junitxml report")
    parser.add_argument(
        "--expect-passed",
        type=int,
        required=True,
        help="exact number of tests that must have passed",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="TEST_NAME",
        help="a testcase name that must appear as passed (repeatable)",
    )
    args = parser.parse_args(argv)

    try:
        check(args.junit_xml, args.expect_passed, args.require)
    except ReportError as exc:
        print(f"::error::live lane did not run as required: {exc}", file=sys.stderr)
        return 1

    print(
        f"live lane ran: {args.expect_passed} passed, 0 skipped "
        f"({', '.join(args.require) or 'no named requirements'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
