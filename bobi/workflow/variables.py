"""Variable resolution and safe condition evaluation.

Handles ${{scope.key}} substitution and when: condition parsing.
No eval() — uses a simple recursive-descent parser for safety.
"""

from __future__ import annotations

import logging
import operator
import re
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

VAR_PATTERN = re.compile(r'\$\{\{(.+?)\}\}')


class VariableContext:

    def __init__(self):
        self.scopes: dict[str, dict[str, Any]] = {}

    def set_scope(self, name: str, data: dict[str, Any]):
        self.scopes[name] = data

    def get(self, scope: str, key: str, default: str = "") -> str:
        data = self.scopes.get(scope, {})
        val = data.get(key, default)
        return str(val) if val is not None else default

    def resolve(self, template: str) -> str:
        """Replace ${{scope.key}} with values. Supports ${{scope.key | lower}}."""
        def _replacer(match: re.Match) -> str:
            expr = match.group(1).strip()

            pipe_filter = None
            if "|" in expr:
                expr, pipe_filter = expr.rsplit("|", 1)
                expr = expr.strip()
                pipe_filter = pipe_filter.strip()

            parts = expr.split(".", 1)
            if len(parts) != 2:
                return match.group(0)

            scope, key = parts

            # Distinguish "missing" (scope/key absent) from "present but
            # empty". A missing reference silently became "" before, which
            # produced malformed prompts like "complexity=, needs_spec=."
            # downstream. Still resolve to "" so optional fields keep working,
            # but log loudly so the gap is visible.
            if scope not in self.scopes:
                log.warning(
                    f"Variable ${{{{{scope}.{key}}}}} references unknown scope "
                    f"'{scope}' — resolving to empty string"
                )
                val = ""
            elif key not in self.scopes[scope]:
                log.warning(
                    f"Variable ${{{{{scope}.{key}}}}} not found in scope "
                    f"'{scope}' — resolving to empty string"
                )
                val = ""
            else:
                val = self.get(scope, key)

            if pipe_filter == "lower":
                val = val.lower()
            elif pipe_filter == "upper":
                val = val.upper()

            return val

        return VAR_PATTERN.sub(_replacer, template)

    def set_flat(self, key: str, value: Any):
        """Set a variable accessible without a scope prefix in conditions."""
        if "_flat" not in self.scopes:
            self.scopes["_flat"] = {}
        self.scopes["_flat"][key] = value

    def evaluate_condition(self, expr: str) -> bool:
        """Evaluate a when: expression. Safe — no eval().

        Supports: ==, !=, >, >=, <, <=, in, not in, and, or, true, false,
        'string literals'. ``${{scope.key}}`` references are substituted
        first; bare names are resolved by the parser itself from the _flat
        scope, so a value carrying spaces, quotes or backslashes can never
        corrupt the token stream (D026).
        """
        return _eval_expr(self.resolve(expr).strip(), self.scopes.get("_flat", {}))


_Flat = dict[str, Any] | None


def _resolve_name(name: str, flat: _Flat) -> str:
    """Resolve a bare name from the flat scope, else return it verbatim.

    Values are stringified here — the one place a flat value enters the
    grammar — so every operand the parser compares is a plain string,
    exactly like a ``${{scope.key}}`` reference.
    """
    if not flat or name not in flat:
        return name
    val = flat[name]
    text = str(val) if val is not None else ""
    # A bool-ish value IS the boolean literal. The textual substitution this
    # replaced fed "True" through _parse_value's case-insensitive literal
    # branch, so `flag == true` matched a Python True (and a brain handoff
    # spelling it "True"); keep that.
    return text.lower() if text.lower() in ("true", "false") else text


def _eval_expr(expr: str, flat: _Flat = None) -> bool:
    """Recursive-descent parser for simple boolean expressions."""
    return _parse_or(expr.strip(), flat)[0]


def _parse_or(expr: str, flat: _Flat = None) -> tuple[bool, str]:
    left, rest = _parse_and(expr, flat)
    while rest.lstrip().startswith("or "):
        rest = rest.lstrip()[3:]
        right, rest = _parse_and(rest, flat)
        left = left or right
    return left, rest


def _parse_and(expr: str, flat: _Flat = None) -> tuple[bool, str]:
    left, rest = _parse_comparison(expr, flat)
    while rest.lstrip().startswith("and "):
        rest = rest.lstrip()[4:]
        right, rest = _parse_comparison(rest, flat)
        left = left and right
    return left, rest


# Two-character operators come first so `>=` is not read as `>` then `=`.
_NUMERIC_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


def _parse_comparison(expr: str, flat: _Flat = None) -> tuple[bool, str]:
    expr = expr.strip()

    if expr.startswith("not "):
        val, rest = _parse_comparison(expr[4:], flat)
        return not val, rest

    left, rest = _parse_value(expr, flat)
    rest = rest.strip()

    if rest.startswith("=="):
        right, rest = _parse_value(rest[2:], flat)
        return str(left).strip() == str(right).strip(), rest
    elif rest.startswith("!="):
        right, rest = _parse_value(rest[2:], flat)
        return str(left).strip() != str(right).strip(), rest
    elif rest.startswith("not in "):
        right, rest = _parse_value_greedy(rest[7:], flat)
        return str(left).strip() not in str(right), rest
    elif rest.startswith("in "):
        right, rest = _parse_value_greedy(rest[3:], flat)
        return str(left).strip() in str(right), rest

    for symbol, apply in _NUMERIC_OPS.items():
        if rest.startswith(symbol):
            right, rest = _parse_value(rest[len(symbol):], flat)
            return _compare_numeric(left, symbol, apply, right), rest

    # Bare truthy check
    if isinstance(left, str):
        return left.lower() in ("true", "1", "yes"), rest
    return bool(left), rest


def _compare_numeric(
    left: Any, symbol: str, apply: Callable[[float, float], bool], right: Any,
) -> bool:
    """Compare two operands numerically.

    A non-numeric operand (an unresolved name, a word, an empty handoff
    value) is False rather than a lexical comparison: `count > 0` must not
    route just because the string "count" sorts after "0".
    """
    try:
        return bool(apply(float(str(left).strip()), float(str(right).strip())))
    except (TypeError, ValueError):
        log.warning(
            f"Condition '{left} {symbol} {right}' compares a non-numeric "
            f"value — evaluating to False"
        )
        return False


def _parse_value_greedy(expr: str, flat: _Flat = None) -> tuple[Any, str]:
    """Parse a value that may be multi-word (for `in` operator RHS).
    Consumes up to `and`/`or` boundaries or end of string.
    Delegates to _parse_value for quoted strings and list literals."""
    expr = expr.strip()
    if expr and expr[0] in ('"', "'", "["):
        return _parse_value(expr, flat)
    # Consume everything up to ` and ` or ` or ` or end
    for boundary in (" and ", " or "):
        idx = expr.find(boundary)
        if idx != -1:
            return _resolve_name(expr[:idx].strip(), flat), expr[idx:]
    return _resolve_name(expr.strip(), flat), ""


def _parse_value(expr: str, flat: _Flat = None) -> tuple[Any, str]:
    expr = expr.strip()

    # String literal (single or double quotes) — never name resolution
    if expr and expr[0] in ('"', "'"):
        quote = expr[0]
        end = expr.index(quote, 1)
        return expr[1:end], expr[end + 1:]

    # List literal ['a', 'b']
    if expr.startswith("["):
        end = expr.index("]")
        inner = expr[1:end]
        items = [s.strip().strip("'\"") for s in inner.split(",")]
        return items, expr[end + 1:]

    # Boolean literals
    bool_match = re.match(r'(true|false)(?=$|[\s=!<>])', expr, re.IGNORECASE)
    if bool_match:
        value = bool_match.group(1).lower()
        return value, expr[bool_match.end():]

    # Bare word (until whitespace or operator), resolved from the flat scope
    match = re.match(r'([^\s=!<>]+)', expr)
    if match:
        return _resolve_name(match.group(1), flat), expr[match.end():]

    return "", expr
