"""Unit tests for workflow variable resolution and safe condition evaluation."""

import pytest

from modastack.workflow.variables import (
    VariableContext,
    _eval_expr,
    _parse_value,
    _parse_value_greedy,
)


# ---------------------------------------------------------------------------
# VariableContext — scope management
# ---------------------------------------------------------------------------

class TestVariableContextScopes:
    def test_set_and_get_scope(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {"name": "modastack", "branch": "main"})
        assert ctx.get("repo", "name") == "modastack"
        assert ctx.get("repo", "branch") == "main"

    def test_get_missing_scope_returns_default(self):
        ctx = VariableContext()
        assert ctx.get("missing", "key") == ""

    def test_get_missing_key_returns_default(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {"name": "modastack"})
        assert ctx.get("repo", "missing") == ""

    def test_get_custom_default(self):
        ctx = VariableContext()
        assert ctx.get("x", "y", default="fallback") == "fallback"

    def test_get_casts_non_string_to_string(self):
        ctx = VariableContext()
        ctx.set_scope("data", {"count": 42, "flag": True})
        assert ctx.get("data", "count") == "42"
        assert ctx.get("data", "flag") == "True"

    def test_get_none_value_returns_default(self):
        ctx = VariableContext()
        ctx.set_scope("data", {"key": None})
        assert ctx.get("data", "key") == ""

    def test_set_scope_overwrites(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {"name": "old"})
        ctx.set_scope("repo", {"name": "new"})
        assert ctx.get("repo", "name") == "new"


# ---------------------------------------------------------------------------
# VariableContext.resolve — template substitution
# ---------------------------------------------------------------------------

class TestVariableResolve:
    def test_simple_substitution(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {"name": "modastack"})
        assert ctx.resolve("Project: ${{repo.name}}") == "Project: modastack"

    def test_multiple_substitutions(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {"name": "modastack", "branch": "main"})
        result = ctx.resolve("${{repo.name}} on ${{repo.branch}}")
        assert result == "modastack on main"

    def test_missing_scope_resolves_to_empty(self):
        ctx = VariableContext()
        assert ctx.resolve("val=${{missing.key}}") == "val="

    def test_missing_key_resolves_to_empty(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {})
        assert ctx.resolve("val=${{repo.missing}}") == "val="

    def test_no_dot_in_expr_preserved(self):
        ctx = VariableContext()
        assert ctx.resolve("${{nodot}}") == "${{nodot}}"

    def test_pipe_filter_lower(self):
        ctx = VariableContext()
        ctx.set_scope("data", {"name": "HELLO"})
        assert ctx.resolve("${{data.name | lower}}") == "hello"

    def test_pipe_filter_upper(self):
        ctx = VariableContext()
        ctx.set_scope("data", {"name": "hello"})
        assert ctx.resolve("${{data.name | upper}}") == "HELLO"

    def test_unknown_filter_ignored(self):
        ctx = VariableContext()
        ctx.set_scope("data", {"name": "hello"})
        assert ctx.resolve("${{data.name | unknown}}") == "hello"

    def test_no_variables_passthrough(self):
        ctx = VariableContext()
        assert ctx.resolve("plain text") == "plain text"

    def test_empty_template(self):
        ctx = VariableContext()
        assert ctx.resolve("") == ""

    def test_whitespace_in_expr(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {"key": "val"})
        assert ctx.resolve("${{ repo.key }}") == "val"


# ---------------------------------------------------------------------------
# VariableContext.set_flat / evaluate_condition
# ---------------------------------------------------------------------------

class TestFlatScope:
    def test_set_flat_creates_scope(self):
        ctx = VariableContext()
        ctx.set_flat("needs_spec", True)
        assert "_flat" in ctx.scopes
        assert ctx.scopes["_flat"]["needs_spec"] is True

    def test_set_flat_multiple(self):
        ctx = VariableContext()
        ctx.set_flat("a", 1)
        ctx.set_flat("b", 2)
        assert ctx.scopes["_flat"]["a"] == 1
        assert ctx.scopes["_flat"]["b"] == 2


# ---------------------------------------------------------------------------
# Condition evaluation — equality
# ---------------------------------------------------------------------------

class TestConditionEquality:
    def test_string_equal(self):
        ctx = VariableContext()
        ctx.set_flat("complexity", "medium")
        assert ctx.evaluate_condition("complexity == medium") is True

    def test_string_not_equal(self):
        ctx = VariableContext()
        ctx.set_flat("complexity", "medium")
        assert ctx.evaluate_condition("complexity != large") is True

    def test_string_equal_false(self):
        ctx = VariableContext()
        ctx.set_flat("complexity", "medium")
        assert ctx.evaluate_condition("complexity == large") is False

    def test_quoted_string_equal(self):
        ctx = VariableContext()
        ctx.set_flat("name", "hello world")
        assert ctx.evaluate_condition("'hello world' == 'hello world'") is True

    def test_bool_true_literal(self):
        ctx = VariableContext()
        ctx.set_flat("needs_spec", "true")
        assert ctx.evaluate_condition("needs_spec == true") is True

    def test_bool_false_literal(self):
        ctx = VariableContext()
        ctx.set_flat("needs_spec", "false")
        assert ctx.evaluate_condition("needs_spec == false") is True


# ---------------------------------------------------------------------------
# Condition evaluation — boolean operators
# ---------------------------------------------------------------------------

class TestConditionBoolean:
    def test_and_both_true(self):
        ctx = VariableContext()
        ctx.set_flat("a", "true")
        ctx.set_flat("b", "true")
        assert ctx.evaluate_condition("a and b") is True

    def test_and_one_false(self):
        ctx = VariableContext()
        ctx.set_flat("a", "true")
        ctx.set_flat("b", "false")
        assert ctx.evaluate_condition("a and b") is False

    def test_or_one_true(self):
        ctx = VariableContext()
        ctx.set_flat("a", "false")
        ctx.set_flat("b", "true")
        assert ctx.evaluate_condition("a or b") is True

    def test_or_both_false(self):
        ctx = VariableContext()
        ctx.set_flat("a", "false")
        ctx.set_flat("b", "false")
        assert ctx.evaluate_condition("a or b") is False

    def test_not_operator(self):
        ctx = VariableContext()
        ctx.set_flat("flag", "false")
        assert ctx.evaluate_condition("not flag") is True

    def test_not_true_is_false(self):
        ctx = VariableContext()
        ctx.set_flat("flag", "true")
        assert ctx.evaluate_condition("not flag") is False

    def test_complex_and_or(self):
        ctx = VariableContext()
        ctx.set_flat("a", "true")
        ctx.set_flat("b", "false")
        ctx.set_flat("c", "true")
        assert ctx.evaluate_condition("a and b or c") is True

    def test_and_has_higher_precedence_than_or(self):
        ctx = VariableContext()
        ctx.set_flat("a", "false")
        ctx.set_flat("b", "true")
        ctx.set_flat("c", "true")
        # false and true or true → (false and true) or true → true
        assert ctx.evaluate_condition("a and b or c") is True


# ---------------------------------------------------------------------------
# Condition evaluation — in / not in
# ---------------------------------------------------------------------------

class TestConditionContainment:
    def test_in_list(self):
        assert _eval_expr("'medium' in ['small', 'medium', 'large']") is True

    def test_not_in_list(self):
        assert _eval_expr("'huge' not in ['small', 'medium', 'large']") is True

    def test_in_list_negative(self):
        assert _eval_expr("'huge' in ['small', 'medium']") is False

    def test_in_string(self):
        ctx = VariableContext()
        ctx.set_flat("text", "hello world")
        assert ctx.evaluate_condition("'hello' in text") is True

    def test_not_in_string(self):
        ctx = VariableContext()
        ctx.set_flat("text", "hello world")
        assert ctx.evaluate_condition("'bye' not in text") is True


# ---------------------------------------------------------------------------
# Condition evaluation — bare truthy
# ---------------------------------------------------------------------------

class TestConditionTruthy:
    def test_bare_true(self):
        assert _eval_expr("true") is True

    def test_bare_false(self):
        assert _eval_expr("false") is False

    def test_bare_1_is_truthy(self):
        assert _eval_expr("1") is True

    def test_bare_yes_is_truthy(self):
        assert _eval_expr("yes") is True

    def test_bare_no_is_falsy(self):
        assert _eval_expr("no") is False

    def test_bare_random_word_is_falsy(self):
        assert _eval_expr("random") is False


# ---------------------------------------------------------------------------
# Condition evaluation — with ${{scope.key}} variables
# ---------------------------------------------------------------------------

class TestConditionWithVariables:
    def test_scoped_variable_in_condition(self):
        ctx = VariableContext()
        ctx.set_scope("handoff", {"complexity": "medium"})
        assert ctx.evaluate_condition("${{handoff.complexity}} == medium") is True

    def test_scoped_variable_not_equal(self):
        ctx = VariableContext()
        ctx.set_scope("handoff", {"complexity": "trivial"})
        assert ctx.evaluate_condition("${{handoff.complexity}} != medium") is True


# ---------------------------------------------------------------------------
# _parse_value — low-level value parsing
# ---------------------------------------------------------------------------

class TestParseValue:
    def test_single_quoted_string(self):
        val, rest = _parse_value("'hello' rest")
        assert val == "hello"
        assert rest == " rest"

    def test_double_quoted_string(self):
        val, rest = _parse_value('"world" rest')
        assert val == "world"
        assert rest == " rest"

    def test_list_literal(self):
        val, rest = _parse_value("['a', 'b', 'c'] more")
        assert val == ["a", "b", "c"]
        assert rest == " more"

    def test_boolean_true(self):
        val, rest = _parse_value("true rest")
        assert val == "true"
        assert rest == " rest"

    def test_boolean_false(self):
        val, rest = _parse_value("false rest")
        assert val == "false"
        assert rest == " rest"

    def test_bare_word(self):
        val, rest = _parse_value("medium == something")
        assert val == "medium"
        assert rest == " == something"

    def test_empty_input(self):
        val, rest = _parse_value("")
        assert val == ""
        assert rest == ""


# ---------------------------------------------------------------------------
# _parse_value_greedy — for `in` operator RHS
# ---------------------------------------------------------------------------

class TestParseValueGreedy:
    def test_quoted_delegates_to_parse_value(self):
        val, rest = _parse_value_greedy("'hello world' and more")
        assert val == "hello world"

    def test_list_delegates_to_parse_value(self):
        val, rest = _parse_value_greedy("['a', 'b'] or c")
        assert val == ["a", "b"]

    def test_bare_word_consumes_to_boundary(self):
        val, rest = _parse_value_greedy("some long text and more")
        assert val == "some long text"
        assert rest == " and more"

    def test_bare_word_consumes_to_end(self):
        val, rest = _parse_value_greedy("everything here")
        assert val == "everything here"
        assert rest == ""

    def test_or_boundary(self):
        val, rest = _parse_value_greedy("left side or right")
        assert val == "left side"
        assert rest == " or right"
