"""Unit tests for workflow variable resolution and safe condition evaluation."""

import logging

import pytest

from bobi.workflow.variables import (
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
        ctx.set_scope("repo", {"name": "bobi", "branch": "main"})
        assert ctx.get("repo", "name") == "bobi"
        assert ctx.get("repo", "branch") == "main"

    def test_get_missing_scope_returns_default(self):
        ctx = VariableContext()
        assert ctx.get("missing", "key") == ""

    def test_get_missing_key_returns_default(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {"name": "bobi"})
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
        ctx.set_scope("repo", {"name": "bobi"})
        assert ctx.resolve("Project: ${{repo.name}}") == "Project: bobi"

    def test_multiple_substitutions(self):
        ctx = VariableContext()
        ctx.set_scope("repo", {"name": "bobi", "branch": "main"})
        result = ctx.resolve("${{repo.name}} on ${{repo.branch}}")
        assert result == "bobi on main"

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

    def test_python_bool_true_matches_lowercase_literal(self):
        ctx = VariableContext()
        ctx.set_flat("needs_spec", True)
        assert ctx.evaluate_condition("needs_spec == true") is True

    def test_python_bool_true_matches_capitalized_literal(self):
        ctx = VariableContext()
        ctx.set_flat("needs_spec", True)
        assert ctx.evaluate_condition("needs_spec == True") is True

    def test_bool_false_literal(self):
        ctx = VariableContext()
        ctx.set_flat("needs_spec", "false")
        assert ctx.evaluate_condition("needs_spec == false") is True

    def test_python_bool_false_matches_lowercase_literal(self):
        ctx = VariableContext()
        ctx.set_flat("needs_spec", False)
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


class TestListContainmentComparesWholeItems:
    """A list literal is the allow-list idiom, so `in` must compare items.

    Stringifying the list made the gate a substring test over
    "['approved', 'lgtm']", which admits any fragment of that text - including
    the empty string, which every string contains. A step that returned a
    blank or missing field was therefore ADMITTED by the allow-list written to
    exclude it, and `not in` rejected it for the same reason inverted. These
    are the two directions a routing gate can fail, so both are pinned.
    """

    @pytest.mark.parametrize("value, allowed", [
        ("", False),             # the one that failed open
        ("app", False),          # a fragment of 'approved'
        ("approved", True),
        ("lgtm", True),
        ("rejected", False),
        ("['approved'", False),  # a fragment of the rendered list itself
    ])
    def test_allow_list_admits_only_whole_items(self, value, allowed):
        ctx = VariableContext()
        ctx.scopes["review"] = {"verdict": value}
        expr = "${{review.verdict}} in ['approved', 'lgtm']"
        assert ctx.evaluate_condition(expr) is allowed

    @pytest.mark.parametrize("value, denied", [
        ("rejected", True),
        ("", True),              # unknown value: undecidable, so denied
        ("reject", False),
    ])
    def test_deny_list_catches_only_whole_items(self, value, denied):
        ctx = VariableContext()
        ctx.scopes["review"] = {"verdict": value}
        expr = "${{review.verdict}} not in ['rejected']"
        assert ctx.evaluate_condition(expr) is not denied

    def test_substring_still_works_against_plain_text(self):
        """Only the LIST form changed - `in` over a string stays a substring."""
        ctx = VariableContext()
        ctx.set_flat("body", "please deploy to staging")
        assert ctx.evaluate_condition("'staging' in body") is True
        assert ctx.evaluate_condition("'production' in body") is False


# The right-hand spellings of one allow-list. The list literal is the idiom the
# fix above already covers; the other two are the same gate written against a
# string, which is what a policy value resolved from a scope arrives as.
_ALLOW_LIST_SPELLINGS = [
    pytest.param("['approved', 'lgtm']", id="list-literal"),
    pytest.param("'approved'", id="string-literal"),
    pytest.param("${{policy.allowed}}", id="scope-resolved-string"),
]


class TestMembershipDeniesAnUnknownValue:
    """A membership gate whose tested value is blank must route NOWHERE.

    Comparing whole items stopped `in ['approved']` admitting the empty string,
    but only for that one spelling. Written against a string - a literal, or a
    policy value resolved from a scope - the gate is still a substring test,
    and every string contains "". Inverted, the list form failed the same way:

      ${{review.verdict}} in 'approved'           verdict=""  was True   ADMITTED
      ${{review.verdict}} in ${{policy.allowed}}  verdict=""  was True   ADMITTED
      ${{review.verdict}} not in ['rejected']     verdict=""  was True   PROCEEDED

    Blank is not an exotic input: it is what a routing gate sees on the most
    ordinary failure there is - a review step whose brain returned nothing, or
    a handoff missing the field, since an unknown scope key resolves to "" (a
    warning, not an error). So the value the gate exists to judge is unknown
    on exactly the runs where judging it matters.

    An unknown value is therefore undecidable and BOTH operators evaluate to
    False. `not in` is a separate gate - "proceed unless denied" - not the
    boolean negation of `in`, so failing closed means False for each of them
    rather than one being the complement of the other. The deliberate cost is
    that an allow-list can no longer admit the empty string on purpose; that
    is pinned below so it reads as a choice, not an oversight.
    """

    def _verdict_ctx(self, value: str) -> VariableContext:
        ctx = VariableContext()
        ctx.scopes["review"] = {"verdict": value}
        ctx.scopes["policy"] = {"allowed": "approved"}
        return ctx

    @pytest.mark.parametrize("allowed", _ALLOW_LIST_SPELLINGS)
    def test_blank_value_is_never_admitted_by_an_allow_list(self, allowed):
        ctx = self._verdict_ctx("")
        assert ctx.evaluate_condition(f"${{{{review.verdict}}}} in {allowed}") is False

    @pytest.mark.parametrize("allowed", _ALLOW_LIST_SPELLINGS)
    def test_blank_value_is_never_passed_by_a_deny_list(self, allowed):
        ctx = self._verdict_ctx("")
        assert ctx.evaluate_condition(
            f"${{{{review.verdict}}}} not in {allowed}") is False

    @pytest.mark.parametrize("allowed", _ALLOW_LIST_SPELLINGS)
    def test_a_missing_field_denies_exactly_like_a_blank_one(self, allowed):
        """The scope key is absent entirely - the omitted-field shape."""
        ctx = VariableContext()
        ctx.scopes["review"] = {}
        ctx.scopes["policy"] = {"allowed": "approved"}
        assert ctx.evaluate_condition(f"${{{{review.verdict}}}} in {allowed}") is False
        assert ctx.evaluate_condition(
            f"${{{{review.verdict}}}} not in {allowed}") is False

    @pytest.mark.parametrize("allowed", _ALLOW_LIST_SPELLINGS)
    def test_a_whitespace_only_value_is_blank_too(self, allowed):
        ctx = self._verdict_ctx("   ")
        assert ctx.evaluate_condition(f"${{{{review.verdict}}}} in {allowed}") is False
        assert ctx.evaluate_condition(
            f"${{{{review.verdict}}}} not in {allowed}") is False

    @pytest.mark.parametrize("allowed", _ALLOW_LIST_SPELLINGS)
    def test_a_known_value_still_decides_in_every_spelling(self, allowed):
        """The gate must still WORK - fail-closed is not deny-everything."""
        assert self._verdict_ctx("approved").evaluate_condition(
            f"${{{{review.verdict}}}} in {allowed}") is True
        assert self._verdict_ctx("rejected").evaluate_condition(
            f"${{{{review.verdict}}}} in {allowed}") is False
        assert self._verdict_ctx("rejected").evaluate_condition(
            f"${{{{review.verdict}}}} not in {allowed}") is True

    @pytest.mark.parametrize("allowed", _ALLOW_LIST_SPELLINGS)
    def test_the_not_prefix_spelling_denies_a_blank_value_too(self, allowed):
        """`not X in [...]` is the same gate as `X not in [...]`.

        Fail-closed has to survive negation. Returning False for an unknown
        value closes `in`, but a plain `not` in front of it turns that False
        straight back into True - so the two spellings of one gate decided
        OPPOSITELY on the blank verdict, and the `not` form went on admitting
        it. `not ` is a first-class part of the grammar, so this is a spelling
        a workflow author reaches for, not a curiosity.
        """
        ctx = self._verdict_ctx("")
        assert ctx.evaluate_condition(f"not ${{{{review.verdict}}}} in {allowed}") is False

    @pytest.mark.parametrize("allowed", _ALLOW_LIST_SPELLINGS)
    def test_both_spellings_of_the_gate_always_agree(self, allowed):
        """Whatever the value, the two spellings must decide the same way."""
        for value in ("", "   ", "approved", "rejected", "app"):
            ctx_a, ctx_b = self._verdict_ctx(value), self._verdict_ctx(value)
            prefix = ctx_a.evaluate_condition(
                f"not ${{{{review.verdict}}}} in {allowed}")
            infix = ctx_b.evaluate_condition(
                f"${{{{review.verdict}}}} not in {allowed}")
            assert prefix is infix, (
                f"verdict={value!r}: `not X in {allowed}` said {prefix} but "
                f"`X not in {allowed}` said {infix}"
            )

    def test_not_still_negates_an_ordinary_comparison(self):
        """Only an UNDECIDABLE result is immune to `not`; the rest negate."""
        ctx = self._verdict_ctx("approved")
        assert ctx.evaluate_condition("not ${{review.verdict}} == 'rejected'") is True
        assert ctx.evaluate_condition("not ${{review.verdict}} == 'approved'") is False
        ctx.set_flat("flag", "false")
        assert ctx.evaluate_condition("not flag") is True

    def test_stacked_nots_cannot_launder_an_unknown_value(self):
        """Undecidable stays undecidable however many `not`s wrap it."""
        ctx = self._verdict_ctx("")
        assert ctx.evaluate_condition(
            "not not ${{review.verdict}} in ['approved']") is False

    def test_an_allow_list_cannot_opt_the_empty_string_back_in(self):
        """The recorded cost: "" is undecidable even when listed explicitly.

        Admitting it would reopen the hole for every gate that did not think
        to exclude it, so the blank check precedes the membership test rather
        than deferring to what the author happened to list.
        """
        ctx = self._verdict_ctx("")
        assert ctx.evaluate_condition("${{review.verdict}} in ['', 'approved']") is False

    def test_a_blank_verdict_routes_to_the_else_branch(self):
        """The same decision through the orchestrator's real routing line.

        `orchestrator.py` gates a route step with exactly this expression and
        picks `goto` or `else_goto` off the result, so a gate that failed open
        did not merely return True - it sent a blank-verdict run down the
        approved branch.
        """
        from bobi.workflow.schema import StepDef

        step = StepDef(name="route_verdict",
                       condition="${{review.verdict}} in ['approved', 'lgtm']",
                       goto="merge", else_goto="escalate")

        def target(verdict: str) -> str:
            ctx = self._verdict_ctx(verdict)
            return step.goto if ctx.evaluate_condition(step.condition) else step.else_goto

        assert target("approved") == "merge"
        assert target("") == "escalate", "a blank verdict reached the merge branch"
        assert target("rejected") == "escalate"


# ---------------------------------------------------------------------------
# Condition evaluation - numeric comparison
# ---------------------------------------------------------------------------

class TestConditionNumeric:
    def test_greater_than_true(self):
        ctx = VariableContext()
        ctx.set_flat("issues_count", 3)
        assert ctx.evaluate_condition("issues_count > 0") is True

    def test_greater_than_false_when_equal(self):
        ctx = VariableContext()
        ctx.set_flat("issues_count", 0)
        assert ctx.evaluate_condition("issues_count > 0") is False

    def test_greater_than_one_is_true(self):
        ctx = VariableContext()
        ctx.set_flat("issues_count", 1)
        assert ctx.evaluate_condition("issues_count > 0") is True

    def test_greater_or_equal(self):
        ctx = VariableContext()
        ctx.set_flat("n", 2)
        assert ctx.evaluate_condition("n >= 2") is True
        assert ctx.evaluate_condition("n >= 3") is False

    def test_less_than(self):
        ctx = VariableContext()
        ctx.set_flat("n", 2)
        assert ctx.evaluate_condition("n < 3") is True
        assert ctx.evaluate_condition("n < 2") is False

    def test_less_or_equal(self):
        ctx = VariableContext()
        ctx.set_flat("n", 2)
        assert ctx.evaluate_condition("n <= 2") is True
        assert ctx.evaluate_condition("n <= 1") is False

    def test_float_values_compare(self):
        ctx = VariableContext()
        ctx.set_flat("ratio", "0.75")
        assert ctx.evaluate_condition("ratio > 0.5") is True

    def test_scoped_variable_numeric_comparison(self):
        ctx = VariableContext()
        ctx.set_scope("audit", {"issues_count": 4})
        assert ctx.evaluate_condition("${{audit.issues_count}} > 0") is True

    def test_non_numeric_operand_is_false(self):
        """An unresolvable / non-numeric operand must not route by accident."""
        ctx = VariableContext()
        assert ctx.evaluate_condition("issues_count > 0") is False

    def test_numeric_comparison_combines_with_and(self):
        ctx = VariableContext()
        ctx.set_flat("n", 5)
        ctx.set_flat("ok", "true")
        assert ctx.evaluate_condition("n > 0 and ok") is True
        assert ctx.evaluate_condition("n > 9 and ok") is False


# ---------------------------------------------------------------------------
# Condition evaluation - values are resolved by the parser, not by textual
# substitution into the expression (D026/Q017). A multi-word or backslash
# bearing value used to corrupt the token stream before parsing.
# ---------------------------------------------------------------------------

class TestFlatResolutionIsNotTextualSubstitution:
    def test_multi_word_value_equality(self):
        ctx = VariableContext()
        ctx.set_flat("complexity", "medium or large")
        assert ctx.evaluate_condition("complexity == 'medium or large'") is True

    def test_multi_word_value_inequality(self):
        ctx = VariableContext()
        ctx.set_flat("verdict", "needs work")
        # Textual substitution produced `needs work != 'approved'`, whose first
        # bare word `needs` swallowed the comparison: the parser never saw the
        # `!=` and fell through to a bare-truthy check on "needs" -> False, so
        # the route silently never fired.
        assert ctx.evaluate_condition("verdict != 'approved'") is True

    def test_multi_word_value_in_list(self):
        ctx = VariableContext()
        ctx.set_flat("verdict", "needs work")
        assert ctx.evaluate_condition(
            "verdict in ['needs work', 'approved']"
        ) is True

    def test_value_containing_and_does_not_split_the_expression(self):
        ctx = VariableContext()
        ctx.set_flat("summary", "docs and tests")
        assert ctx.evaluate_condition("summary == 'docs and tests'") is True

    def test_quoted_literal_is_not_rewritten_by_a_flat_key(self):
        ctx = VariableContext()
        ctx.set_flat("status", "failed")
        assert ctx.evaluate_condition("'status' != 'failed'") is True

    def test_backslash_in_value_is_not_an_escape(self):
        ctx = VariableContext()
        ctx.set_flat("path", r"C:\temp\build")
        assert ctx.evaluate_condition(r"path == 'C:\temp\build'") is True

    def test_invalid_regex_escape_in_value_does_not_raise(self):
        ctx = VariableContext()
        ctx.set_flat("pattern", r"a\qb")
        assert ctx.evaluate_condition(r"pattern == 'a\qb'") is True

    def test_flat_key_with_regex_metacharacters(self):
        ctx = VariableContext()
        ctx.set_flat("count.total", "5")
        ctx.set_flat("cost$", "12")
        # The key IS the bare name: it is looked up verbatim, never compiled
        # into a pattern, so `.` and `$` match nothing but themselves. The
        # substitution this replaced anchored each key with `\b`, which a `$`
        # suffix defeats outright: `cost$` never resolved at all.
        assert ctx.evaluate_condition("count.total == '5'") is True
        assert ctx.evaluate_condition("cost$ == '12'") is True
        # `.` is not a wildcard: a same-shaped name that is not the key stays
        # unresolved rather than matching it.
        assert ctx.evaluate_condition("countXtotal == '5'") is False

    def test_greedy_rhs_resolves_a_bare_name(self):
        ctx = VariableContext()
        ctx.set_flat("text", "hello world")
        assert ctx.evaluate_condition("'hello' in text") is True

    def test_unresolved_bare_name_stays_literal(self):
        ctx = VariableContext()
        assert ctx.evaluate_condition("missing == missing") is True

    def test_none_value_resolves_to_empty(self):
        ctx = VariableContext()
        ctx.set_flat("maybe", None)
        assert ctx.evaluate_condition("maybe == ''") is True


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

    def test_scoped_python_bool_matches_lowercase_literal(self):
        ctx = VariableContext()
        ctx.set_scope("handoff", {"scope_clear": True})
        assert ctx.evaluate_condition("${{handoff.scope_clear}} == true") is True

    def test_scoped_python_false_matches_lowercase_literal(self):
        ctx = VariableContext()
        ctx.set_scope("handoff", {"scope_clear": False})
        assert ctx.evaluate_condition("${{handoff.scope_clear}} == false") is True

    def test_scoped_variable_not_equal(self):
        ctx = VariableContext()
        ctx.set_scope("handoff", {"complexity": "trivial"})
        assert ctx.evaluate_condition("${{handoff.complexity}} != medium") is True


# ---------------------------------------------------------------------------
# Condition evaluation - a ${{scope.key}} reference resolves to a VALUE inside
# the parser, exactly like a bare name. Its content is never spliced into the
# expression text, so untrusted data (a GitHub issue title in the `event`
# scope) cannot inject operators and steer the branch.
# ---------------------------------------------------------------------------

class TestScopedReferenceResolvesToAValue:
    def test_reference_as_operand(self):
        ctx = VariableContext()
        ctx.set_scope("event", {"action": "opened"})
        assert ctx.evaluate_condition("${{event.action}} == 'opened'") is True
        assert ctx.evaluate_condition("${{event.action}} == 'closed'") is False

    def test_reference_inside_quoted_literal(self):
        """A ref inside quotes still resolves: the quotes delimit the source
        text, and the resolved value is returned whole, never re-tokenized."""
        ctx = VariableContext()
        ctx.set_scope("event", {"title": "Deploy"})
        assert ctx.evaluate_condition("'${{event.title}}' == 'Deploy'") is True
        assert ctx.evaluate_condition("'${{event.title}}' == 'Ship'") is False

    def test_reference_with_pipe_filter(self):
        """Spaces inside the braces must not truncate the reference."""
        ctx = VariableContext()
        ctx.set_scope("event", {"action": "OPENED"})
        assert ctx.evaluate_condition(
            "${{event.action | lower}} == 'opened'"
        ) is True

    def test_reference_with_whitespace_inside_braces(self):
        ctx = VariableContext()
        ctx.set_scope("input", {"merged": True})
        assert ctx.evaluate_condition("${{ input.merged }} == true") is True

    def test_reference_as_greedy_rhs_of_in(self):
        ctx = VariableContext()
        ctx.set_scope("event", {"title": "fix the flaky test"})
        assert ctx.evaluate_condition("'flaky' in ${{event.title}}") is True
        assert ctx.evaluate_condition("'green' not in ${{event.title}}") is True

    def test_reference_in_list_literal(self):
        ctx = VariableContext()
        ctx.set_scope("event", {"action": "opened"})
        assert ctx.evaluate_condition(
            "'opened' in ['${{event.action}}', 'closed']"
        ) is True

    def test_missing_scope_resolves_to_empty_with_warning(self, caplog):
        ctx = VariableContext()
        with caplog.at_level(logging.WARNING, logger="bobi.workflow.variables"):
            assert ctx.evaluate_condition("${{missing.key}} == ''") is True
        assert "unknown scope" in caplog.text

    def test_missing_key_resolves_to_empty_with_warning(self, caplog):
        ctx = VariableContext()
        ctx.set_scope("event", {})
        with caplog.at_level(logging.WARNING, logger="bobi.workflow.variables"):
            assert ctx.evaluate_condition("${{event.title}} == ''") is True
        assert "not found in scope" in caplog.text

    @pytest.mark.parametrize("payload,matches", [
        ("x' or 'a' == 'a", False),
        ("x' or 'a' == 'a' or 'x", False),
        ("x or true or x", False),
        ("expected", True),  # only the literal value may take the branch
    ])
    def test_quote_injection_cannot_take_the_branch(self, payload, matches):
        """The security case: an untrusted value that adds its own operators.

        Textual substitution spliced the value into the expression text, so
        `x' or 'a' == 'a' or 'x` became a real `or` clause and the route
        fired unconditionally.
        """
        ctx = VariableContext()
        ctx.set_scope("event", {"title": payload})
        assert ctx.evaluate_condition(
            "${{event.title}} == 'expected'"
        ) is matches

    def test_quote_injection_inside_a_quoted_literal_cannot_take_the_branch(self):
        """The classic quote-breaking shape: `'${{ref}}' == 'expected'`."""
        ctx = VariableContext()
        ctx.set_scope("event", {"title": "x' or 'a' == 'a"})
        assert ctx.evaluate_condition("'${{event.title}}' == 'expected'") is False

    def test_operator_injection_cannot_take_the_branch(self):
        """A value that is itself a whole expression stays a plain value."""
        ctx = VariableContext()
        ctx.set_scope("event", {"title": "'a' == 'a' or 'b' == 'b'"})
        assert ctx.evaluate_condition("${{event.title}}") is False

    def test_and_injection_cannot_split_the_expression(self):
        ctx = VariableContext()
        ctx.set_scope("event", {"title": "nope' and 'x"})
        ctx.set_flat("ok", "true")
        assert ctx.evaluate_condition(
            "${{event.title}} == 'expected' or ok"
        ) is True
        assert ctx.evaluate_condition(
            "${{event.title}} == 'expected'"
        ) is False

    def test_bracketed_issue_title_does_not_take_the_branch(self):
        """No payload needed: a normal issue title was enough.

        `[bug] crash on save` spliced in as `[bug] crash on save ==
        'expected'` parsed as a list literal, and a non-empty list is
        truthy, so the route fired on every bracketed title.
        """
        ctx = VariableContext()
        ctx.set_scope("event", {"title": "[bug] crash on save"})
        assert ctx.evaluate_condition("${{event.title}} == 'expected'") is False

    @pytest.mark.parametrize("payload", [
        "[unclosed bracket",
        "it's broken",
        'unbalanced " quote',
        "] or true or [",
    ])
    def test_unbalanced_delimiter_in_a_value_does_not_raise(self, payload):
        """An unbalanced quote or bracket used to reach index() and raise
        ValueError out of the parser, failing the run."""
        ctx = VariableContext()
        ctx.set_scope("event", {"title": payload})
        assert ctx.evaluate_condition("${{event.title}} == 'expected'") is False
        assert ctx.evaluate_condition("'${{event.title}}' == 'expected'") is False

    def test_resolved_reference_is_not_re_read_as_a_bare_name(self):
        """Untrusted text must not alias a flat binding."""
        ctx = VariableContext()
        ctx.set_flat("secret", "1234")
        ctx.set_scope("event", {"title": "secret"})
        assert ctx.evaluate_condition("${{event.title}} == 'secret'") is True
        assert ctx.evaluate_condition("${{event.title}} == '1234'") is False

    def test_value_carrying_spaces_compares_whole(self):
        ctx = VariableContext()
        ctx.set_scope("event", {"title": "needs work"})
        assert ctx.evaluate_condition("${{event.title}} != 'approved'") is True
        assert ctx.evaluate_condition("${{event.title}} == 'needs work'") is True


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

    def test_capitalized_boolean_literal(self):
        val, rest = _parse_value("True == true")
        assert val == "true"
        assert rest == " == true"

    def test_boolean_prefix_without_boundary_is_bare_word(self):
        val, rest = _parse_value("TrueValue == true")
        assert val == "TrueValue"
        assert rest == " == true"

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
