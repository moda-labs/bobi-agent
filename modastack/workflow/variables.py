"""Variable resolution and safe condition evaluation.

Handles ${{scope.key}} substitution and when: condition parsing.
No eval() — uses a simple recursive-descent parser for safety.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

VAR_PATTERN = re.compile(r'\$\{\{(.+?)\}\}')

# Words that look like bare identifiers but are operators/literals in a
# condition — never substitute these even if a handoff happens to use the name.
_CONDITION_KEYWORDS = frozenset({"and", "or", "not", "in", "true", "false"})
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_NUMERIC = re.compile(r"-?\d+(?:\.\d+)?$")


def _normalize_value(val: Any) -> str:
    """Render a handoff value as a condition/template string.

    YAML parses ``needs_spec: true`` to a Python ``bool``; ``str(True)`` is
    "True", which never equals the lowercase ``true`` literal a condition
    compares against. Normalize bools to lowercase so both ``${{...}}`` and
    bare-name comparisons behave as authors expect.
    """
    if isinstance(val, bool):
        return "true" if val else "false"
    if val is None:
        return ""
    return str(val)


class VariableContext:

    def __init__(self):
        self.scopes: dict[str, dict[str, Any]] = {}
        # Flat accumulation of every step's handoff outputs, so route
        # conditions can reference a field by bare name (`needs_spec == true`)
        # without knowing which step produced it.
        self.flat: dict[str, Any] = {}

    def set_scope(self, name: str, data: dict[str, Any]):
        self.scopes[name] = data

    def update_flat(self, data: dict[str, Any]):
        self.flat.update(data)

    def get(self, scope: str, key: str, default: str = "") -> str:
        data = self.scopes.get(scope, {})
        val = data.get(key, default)
        return _normalize_value(val) if val is not None else default

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

    def evaluate_condition(self, expr: str) -> bool:
        """Evaluate a when: expression. Safe — no eval().

        Supports: ==, !=, in, not in, and, or, true, false, 'string literals'.
        Bare identifiers that match an accumulated handoff field are
        substituted with that field's value, so `needs_spec == true` compares
        the handoff value rather than the literal string "needs_spec".
        """
        resolved = self.resolve(expr)
        resolved = self._substitute_bare(resolved)
        return _eval_expr(resolved.strip())

    def _substitute_bare(self, expr: str) -> str:
        """Replace bare handoff-field references with their values.

        Only identifiers present in ``self.flat`` are touched; operators and
        literals (and/or/not/in/true/false) are left alone. String values are
        single-quoted so multi-word values don't break the parser; booleans
        and numbers are inserted bare so they compare as expected.
        """
        if not self.flat:
            return expr

        def _replace(match: re.Match) -> str:
            token = match.group(0)
            if token in _CONDITION_KEYWORDS or token not in self.flat:
                return token
            val = _normalize_value(self.flat[token])
            if val in ("true", "false") or _NUMERIC.match(val):
                return val
            return f"'{val}'"

        return _IDENTIFIER.sub(_replace, expr)


def _eval_expr(expr: str) -> bool:
    """Recursive-descent parser for simple boolean expressions."""
    return _parse_or(expr.strip())[0]


def _parse_or(expr: str) -> tuple[bool, str]:
    left, rest = _parse_and(expr)
    while rest.lstrip().startswith("or "):
        rest = rest.lstrip()[3:]
        right, rest = _parse_and(rest)
        left = left or right
    return left, rest


def _parse_and(expr: str) -> tuple[bool, str]:
    left, rest = _parse_comparison(expr)
    while rest.lstrip().startswith("and "):
        rest = rest.lstrip()[4:]
        right, rest = _parse_comparison(rest)
        left = left and right
    return left, rest


def _parse_comparison(expr: str) -> tuple[bool, str]:
    expr = expr.strip()

    if expr.startswith("not "):
        val, rest = _parse_comparison(expr[4:])
        return not val, rest

    left, rest = _parse_value(expr)
    rest = rest.strip()

    if rest.startswith("=="):
        right, rest = _parse_value(rest[2:])
        return str(left).strip() == str(right).strip(), rest
    elif rest.startswith("!="):
        right, rest = _parse_value(rest[2:])
        return str(left).strip() != str(right).strip(), rest
    elif rest.startswith("not in "):
        right, rest = _parse_value_greedy(rest[7:])
        return str(left).strip() not in str(right), rest
    elif rest.startswith("in "):
        right, rest = _parse_value_greedy(rest[3:])
        return str(left).strip() in str(right), rest

    # Bare truthy check
    if isinstance(left, str):
        return left.lower() in ("true", "1", "yes"), rest
    return bool(left), rest


def _parse_value_greedy(expr: str) -> tuple[Any, str]:
    """Parse a value that may be multi-word (for `in` operator RHS).
    Consumes up to `and`/`or` boundaries or end of string.
    Delegates to _parse_value for quoted strings and list literals."""
    expr = expr.strip()
    if expr and expr[0] in ('"', "'", "["):
        return _parse_value(expr)
    # Consume everything up to ` and ` or ` or ` or end
    for boundary in (" and ", " or "):
        idx = expr.find(boundary)
        if idx != -1:
            return expr[:idx].strip(), expr[idx:]
    return expr.strip(), ""


def _parse_value(expr: str) -> tuple[Any, str]:
    expr = expr.strip()

    # String literal (single or double quotes)
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
    if expr.startswith("true"):
        return "true", expr[4:]
    if expr.startswith("false"):
        return "false", expr[5:]

    # Bare word (until whitespace or operator)
    match = re.match(r'([^\s=!<>]+)', expr)
    if match:
        return match.group(1), expr[match.end():]

    return "", expr
