"""Tests for workflow variable resolution and condition evaluation.

Covers the spec-approval-gate root cause: a route condition like
`needs_spec == true` must compare the *handoff value*, not the literal
string "needs_spec". Before the fix, bare identifiers were never resolved,
so the condition was always false and routing always fell through to the
`else` branch — bypassing the spec/approval gate.
"""

from modastack.workflow.variables import VariableContext, _normalize_value


class TestNormalizeValue:
    def test_bool_true_lowercase(self):
        assert _normalize_value(True) == "true"

    def test_bool_false_lowercase(self):
        assert _normalize_value(False) == "false"

    def test_none_is_empty(self):
        assert _normalize_value(None) == ""

    def test_passthrough_str(self):
        assert _normalize_value("medium") == "medium"

    def test_int(self):
        assert _normalize_value(3) == "3"


class TestResolveTemplates:
    def test_scoped_lookup(self):
        ctx = VariableContext()
        ctx.set_scope("pickup", {"complexity": "medium"})
        assert ctx.resolve("c=${{pickup.complexity}}") == "c=medium"

    def test_bool_resolves_lowercase(self):
        """A YAML bool must render as `true`, not Python's `True`."""
        ctx = VariableContext()
        ctx.set_scope("pickup", {"needs_spec": True})
        assert ctx.resolve("${{pickup.needs_spec}}") == "true"


class TestEvaluateBareConditions:
    def test_bare_bool_true_routes(self):
        ctx = VariableContext()
        ctx.update_flat({"needs_spec": True})
        assert ctx.evaluate_condition("needs_spec == true") is True

    def test_bare_bool_false_does_not_route(self):
        ctx = VariableContext()
        ctx.update_flat({"needs_spec": False})
        assert ctx.evaluate_condition("needs_spec == true") is False

    def test_bare_string_true_routes(self):
        """Handoffs sometimes carry the string "true" rather than a YAML bool."""
        ctx = VariableContext()
        ctx.update_flat({"needs_spec": "true"})
        assert ctx.evaluate_condition("needs_spec == true") is True

    def test_bare_string_value_comparison(self):
        ctx = VariableContext()
        ctx.update_flat({"complexity": "medium"})
        assert ctx.evaluate_condition("complexity == 'medium'") is True
        assert ctx.evaluate_condition("complexity == 'small'") is False

    def test_multiword_value_does_not_break_parser(self):
        ctx = VariableContext()
        ctx.update_flat({"status": "in progress"})
        assert ctx.evaluate_condition("status == 'in progress'") is True

    def test_unknown_bare_identifier_is_literal(self):
        """Backward compat: an identifier not in the handoff stays a literal,
        so a genuinely-missing field never silently routes true."""
        ctx = VariableContext()
        assert ctx.evaluate_condition("needs_spec == true") is False

    def test_keywords_not_substituted(self):
        """Even if a handoff field is named like an operator, the operator
        keyword itself is never replaced."""
        ctx = VariableContext()
        ctx.update_flat({"and": "x", "needs_spec": True})
        assert ctx.evaluate_condition("needs_spec == true and needs_spec == true") is True

    def test_and_or_compose(self):
        ctx = VariableContext()
        ctx.update_flat({"a": True, "b": False})
        assert ctx.evaluate_condition("a == true and b == false") is True
        assert ctx.evaluate_condition("a == false or b == true") is False
