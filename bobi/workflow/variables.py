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
        """Evaluate a when: expression. Safe - no eval().

        Supports: ==, !=, >, >=, <, <=, in, not in, and, or, true, false,
        'string literals'. Both ``${{scope.key}}`` references and bare names
        are resolved by the parser itself, once the operand around them has
        been delimited, and the result is handed back as a finished value
        that is never re-read as expression text. So a value carrying
        spaces, quotes or backslashes can never corrupt the token stream
        (D026) - the expression's own operators are the only operators.
        """
        return _eval_expr(expr.strip(), self)


# The parser consults the context for both bare names and ${{scope.key}}
# references. None means "no bindings": every operand stays literal.
_Ctx = VariableContext | None


def _resolve_refs(text: str, ctx: _Ctx) -> str:
    """Substitute ``${{scope.key}}`` references in already-delimited text.

    Every caller has finished reading the operand out of the expression
    (consumed its quotes, its list commas, its word boundary) before
    calling this, so what comes back is a complete value. It is returned to
    the caller, never fed back to the tokenizer.
    """
    return ctx.resolve(text) if ctx else text


def _resolve_name(name: str, ctx: _Ctx) -> str:
    """Resolve a bare name from the flat scope, else return it verbatim."""
    flat: dict[str, Any] = ctx.scopes.get("_flat", {}) if ctx else {}
    if name not in flat:
        return name
    val = flat[name]
    return str(val) if val is not None else ""


def _resolve_operand(text: str, ctx: _Ctx) -> str:
    """Resolve a parsed operand to the value the grammar compares.

    A ``${{scope.key}}`` reference resolves through the context; anything
    else is a bare name looked up in the flat scope. A resolved reference
    is not then looked up as a name, so untrusted text cannot alias a flat
    binding.
    """
    if VAR_PATTERN.search(text):
        value = _resolve_refs(text, ctx)
    else:
        value = _resolve_name(text, ctx)
    # A bool-ish value IS the boolean literal. The textual substitution this
    # replaced fed "True" through _parse_value's case-insensitive literal
    # branch, so `flag == true` matched a Python True (and a brain handoff
    # spelling it "True"); keep that.
    lowered = value.strip().lower()
    return lowered if lowered in ("true", "false") else value


def _eval_expr(expr: str, ctx: _Ctx = None) -> bool:
    """Recursive-descent parser for simple boolean expressions."""
    return _parse_or(expr.strip(), ctx)[0]


def _parse_or(expr: str, ctx: _Ctx = None) -> tuple[bool, str]:
    left, rest = _parse_and(expr, ctx)
    while rest.lstrip().startswith("or "):
        rest = rest.lstrip()[3:]
        right, rest = _parse_and(rest, ctx)
        left = left or right
    return left, rest


def _parse_and(expr: str, ctx: _Ctx = None) -> tuple[bool, str]:
    left, rest = _parse_comparison(expr, ctx)
    while rest.lstrip().startswith("and "):
        rest = rest.lstrip()[4:]
        right, rest = _parse_comparison(rest, ctx)
        left = left and right
    return left, rest


# Two-character operators come first so `>=` is not read as `>` then `=`.
_NUMERIC_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


def _membership(haystack: Any, needle: Any, *, negate: bool) -> bool:
    """Evaluate `needle in haystack`, or its `not in` inverse. Fails CLOSED.

    Two rules, both learned from the same routing gate failing open.

    ONE: `in` against a LIST literal is membership; against text it stays a
    substring. A list literal is the allow-list idiom
    (`verdict in ['approved', 'lgtm']`), so it has to compare whole items.
    Stringifying the list first turned the gate into a substring test over
    `"['approved', 'lgtm']"`, which admits any fragment of it - `app` passed.

    TWO: an unknown value decides nothing, so a blank `needle` is False for
    BOTH operators. Whole-item comparison alone did not close this: written
    against a string - a literal, or a policy resolved from a scope - the gate
    is still a substring test, and every string contains "". `not in` leaked
    the same way inverted, admitting blank because it is not a member.

    Blank is the ordinary case, not an exotic one: a step whose brain returned
    nothing, or a handoff missing the field, since an unknown scope key
    resolves to "" with a warning rather than an error. The value the gate
    exists to judge is therefore unknown on exactly the runs where judging it
    matters, and both directions used to route as if the check had passed.

    `not in` is a separate gate - "proceed unless denied" - not the boolean
    negation of `in`, which is why the blank check precedes `negate` instead
    of being inverted along with the result. The deliberate cost: an allow-list
    can no longer admit "" on purpose. Letting it would reopen the hole for
    every gate that never thought to list "" in the first place.
    """
    value = str(needle).strip()
    if not value:
        return False
    if isinstance(haystack, list):
        member = value in [str(item).strip() for item in haystack]
    else:
        member = value in str(haystack)
    return not member if negate else member


def _parse_comparison(expr: str, ctx: _Ctx = None) -> tuple[bool, str]:
    expr = expr.strip()

    if expr.startswith("not "):
        val, rest = _parse_comparison(expr[4:], ctx)
        return not val, rest

    left, rest = _parse_value(expr, ctx)
    rest = rest.strip()

    if rest.startswith("=="):
        right, rest = _parse_value(rest[2:], ctx)
        return str(left).strip() == str(right).strip(), rest
    elif rest.startswith("!="):
        right, rest = _parse_value(rest[2:], ctx)
        return str(left).strip() != str(right).strip(), rest
    elif rest.startswith("not in "):
        right, rest = _parse_value_greedy(rest[7:], ctx)
        return _membership(right, left, negate=True), rest
    elif rest.startswith("in "):
        right, rest = _parse_value_greedy(rest[3:], ctx)
        return _membership(right, left, negate=False), rest

    for symbol, apply in _NUMERIC_OPS.items():
        if rest.startswith(symbol):
            right, rest = _parse_value(rest[len(symbol):], ctx)
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
            f"value - evaluating to False"
        )
        return False


def _parse_value_greedy(expr: str, ctx: _Ctx = None) -> tuple[Any, str]:
    """Parse a value that may be multi-word (for `in` operator RHS).
    Consumes up to `and`/`or` boundaries or end of string.
    Delegates to _parse_value for quoted strings and list literals."""
    expr = expr.strip()
    if expr and expr[0] in ('"', "'", "["):
        return _parse_value(expr, ctx)
    # Consume everything up to ` and ` or ` or ` or end. The scan reads the
    # expression as written, so an `and` inside a resolved value is text.
    for boundary in (" and ", " or "):
        idx = expr.find(boundary)
        if idx != -1:
            return _resolve_operand(expr[:idx].strip(), ctx), expr[idx:]
    return _resolve_operand(expr.strip(), ctx), ""


# One operand's worth of bare text. A whole ``${{scope.key}}`` reference is
# atomic, so the spaces and operators a reference may carry inside its braces
# (`${{event.action | lower}}`, `${{ input.merged }}`) stay part of it.
_BARE_WORD = re.compile(r'(?:\$\{\{.+?\}\}|[^\s=!<>])+')


def _parse_value(expr: str, ctx: _Ctx = None) -> tuple[Any, str]:
    expr = expr.strip()

    # String literal (single or double quotes) - never name resolution.
    # The quotes belong to the expression and are consumed before anything
    # between them resolves, so a value carrying a quote cannot end the
    # literal early.
    if expr and expr[0] in ('"', "'"):
        quote = expr[0]
        end = expr.index(quote, 1)
        return _resolve_refs(expr[1:end], ctx), expr[end + 1:]

    # List literal ['a', 'b']. Items are split before they are resolved, so
    # a comma inside a value cannot add an item.
    if expr.startswith("["):
        end = expr.index("]")
        inner = expr[1:end]
        items = [_resolve_refs(s.strip().strip("'\""), ctx)
                 for s in inner.split(",")]
        return items, expr[end + 1:]

    # Boolean literals
    bool_match = re.match(r'(true|false)(?=$|[\s=!<>])', expr, re.IGNORECASE)
    if bool_match:
        value = bool_match.group(1).lower()
        return value, expr[bool_match.end():]

    # Bare word (until whitespace or operator), resolved to its value
    match = _BARE_WORD.match(expr)
    if match:
        return _resolve_operand(match.group(0), ctx), expr[match.end():]

    return "", expr
