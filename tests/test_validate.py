"""Tests for startup config validation."""

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch, MagicMock

from bobi.validate import (
    validate_config,
    _check_entry_point,
    _check_service_credentials,
    _check_venn_services,
    _check_mcp_servers,
    status_glyph,
    supports_unicode,
    CheckResult,
)
from bobi.config import Config, ServiceConfig


class _FakeStream:
    def __init__(self, encoding):
        self.encoding = encoding


class TestStatusGlyph:

    def test_unicode_glyphs(self):
        assert status_glyph(True, True, unicode=True) == "✓"
        assert status_glyph(False, True, unicode=True) == "✗"   # blocking
        assert status_glyph(False, False, unicode=True) == "⚠"  # warning

    def test_ascii_fallback(self):
        assert status_glyph(True, True, unicode=False) == "[OK]"
        assert status_glyph(False, True, unicode=False) == "[ERROR]"
        assert status_glyph(False, False, unicode=False) == "[WARN]"

    def test_required_ignored_when_ok(self):
        # When a check passes, `required` doesn't change the marker.
        assert status_glyph(True, False, unicode=True) == "✓"
        assert status_glyph(True, False, unicode=False) == "[OK]"

    def test_supports_unicode_utf8(self):
        assert supports_unicode(_FakeStream("utf-8")) is True

    def test_supports_unicode_ascii(self):
        assert supports_unicode(_FakeStream("ascii")) is False

    def test_supports_unicode_no_encoding(self):
        assert supports_unicode(_FakeStream(None)) is False

    def test_supports_unicode_unknown_encoding(self):
        # An unknown codec name raises LookupError → treat as no support.
        assert supports_unicode(_FakeStream("definitely-not-a-codec")) is False

    def test_format_falls_back_to_text_markers(self):
        from bobi.validate import ValidationResult
        checks = [
            CheckResult("github", ok=True, detail="native"),
            CheckResult("email", ok=False, detail="venn — not connected", required=False),
            CheckResult("slack", ok=False, detail="native — missing bot_token", required=True),
        ]
        vr = ValidationResult(ok=False, checks=checks)
        with patch("bobi.validate.supports_unicode", return_value=False):
            output = vr.format()
        assert "[OK]" in output
        assert "[WARN]" in output   # optional failure
        assert "[ERROR]" in output  # required failure
        assert "✓" not in output and "⚠" not in output and "✗" not in output


class TestCheckEntryPoint:

    def test_valid_role(self, tmp_path):
        (tmp_path / "package" / "roles" / "director").mkdir(parents=True)
        cfg = Config(entry_point="director")
        result = _check_entry_point(cfg, tmp_path)
        assert result.ok

    def test_missing_role(self, tmp_path):
        (tmp_path / "package" / "roles" / "engineer").mkdir(parents=True)
        cfg = Config(entry_point="director")
        result = _check_entry_point(cfg, tmp_path)
        assert not result.ok
        assert "not found" in result.detail

    def test_empty_entry_point_defaults(self, tmp_path):
        cfg = Config(entry_point="")
        result = _check_entry_point(cfg, tmp_path)
        assert result.ok
        assert "defaulting" in result.detail


class TestCheckRoles:
    """roles: misconfiguration fails silently at runtime, so validate must
    surface it (#617 review finding)."""

    def _check(self, cfg, tmp_path):
        from bobi.validate import _check_roles
        return _check_roles(cfg, tmp_path)

    def test_valid_roles_pass(self, tmp_path):
        (tmp_path / "package" / "roles" / "reviewer").mkdir(parents=True)
        cfg = Config(roles={"reviewer": {"model": "opus"},
                            "monitor": {"model": "haiku"}})
        assert self._check(cfg, tmp_path) == []

    def test_non_dict_entry_warns(self, tmp_path):
        cfg = Config(roles={"reviewer": "opus"})
        results = self._check(cfg, tmp_path)
        assert len(results) == 1
        assert not results[0].ok
        assert not results[0].required  # warning, not blocking
        assert "must be a mapping" in results[0].detail

    def test_unknown_role_name_warns(self, tmp_path):
        (tmp_path / "package" / "roles" / "reviewer").mkdir(parents=True)
        cfg = Config(roles={"moniter": {"model": "haiku"}})  # typo
        results = self._check(cfg, tmp_path)
        assert len(results) == 1
        assert not results[0].ok
        assert not results[0].required
        assert "unknown role" in results[0].detail
        assert "monitor" in results[0].hint  # built-in listed as known

    def test_monitor_is_builtin_even_without_role_dir(self, tmp_path):
        (tmp_path / "package" / "roles" / "reviewer").mkdir(parents=True)
        cfg = Config(roles={"monitor": {"model": "haiku"}})
        assert self._check(cfg, tmp_path) == []

    def test_no_role_dirs_skips_name_check(self, tmp_path):
        cfg = Config(roles={"anything": {"model": "haiku"}})
        assert self._check(cfg, tmp_path) == []

    def test_empty_roles_pass(self, tmp_path):
        assert self._check(Config(), tmp_path) == []


class TestCheckEffort:
    """effort: is pass-through like model, so validate only warns on values
    the configured brain does not declare (#778) - never blocks."""

    def _check(self, cfg):
        from bobi.validate import _check_effort
        return _check_effort(cfg)

    def test_brain_accepted_values_pass(self):
        codex = Config(
            brain={"kind": "codex", "effort": "high"},
            roles={"monitor": {"effort": "none"},
                   "planner": {"effort": "xhigh"}},
        )
        assert self._check(codex) == []
        claude = Config(
            brain={"kind": "claude", "effort": "max"},
            roles={"monitor": {"effort": "low"}},
        )
        assert self._check(claude) == []

    def test_cross_vendor_value_warns_per_brain(self):
        """The check consults the configured brain's declared set, so a value
        valid only on the OTHER vendor warns instead of hiding in the union."""
        codex_with_max = Config(brain={"kind": "codex", "effort": "max"})
        results = self._check(codex_with_max)
        assert len(results) == 1
        assert "codex brain" in results[0].detail
        # Default (claude) brain rejects the codex-only tiers.
        claude_with_none = Config(roles={"monitor": {"effort": "none"}})
        results = self._check(claude_with_none)
        assert len(results) == 1
        assert results[0].name == "roles.monitor.effort"

    def test_unknown_brain_effort_warns(self):
        cfg = Config(brain={"effort": "turbo"})
        results = self._check(cfg)
        assert len(results) == 1
        assert results[0].name == "brain.effort"
        assert not results[0].ok
        assert not results[0].required  # warning, not blocking

    def test_unknown_role_effort_warns(self):
        cfg = Config(roles={"monitor": {"effort": "extreme"}})
        results = self._check(cfg)
        assert len(results) == 1
        assert results[0].name == "roles.monitor.effort"
        assert not results[0].ok
        assert not results[0].required

    def test_non_string_effort_warns(self):
        cfg = Config(roles={"monitor": {"effort": 3}})
        results = self._check(cfg)
        assert len(results) == 1
        assert not results[0].ok

    def test_falsy_non_string_effort_warns(self):
        """`effort: no` parses as YAML False and the runtime silently drops
        it, so validate must not skip falsy values."""
        cfg = Config(roles={"monitor": {"effort": False}})
        results = self._check(cfg)
        assert len(results) == 1
        assert not results[0].ok
        cfg = Config(brain={"effort": 0})
        assert len(self._check(cfg)) == 1

    def test_no_effort_configured_passes(self):
        assert self._check(Config()) == []
        assert self._check(Config(
            brain={"kind": "codex", "model": "gpt-5.6"},
            roles={"monitor": {"model": "haiku"}},
        )) == []

    def test_gateway_brain_falls_back_to_union(self):
        """A gateway backend's accepted set is unknown, so any union value
        passes rather than warning on everything."""
        cfg = Config(brain={"kind": "gateway", "base_url": "http://x",
                            "effort": "max"})
        assert self._check(cfg) == []


class TestCheckWorkflowEffort:
    """Step-level effort: gets the same typo warning as config-level (#778)."""

    def _check(self, cfg, project_path):
        from bobi.validate import _check_workflow_effort
        return _check_workflow_effort(cfg, project_path)

    def _write_workflow(self, tmp_path, effort_line):
        wf_dir = tmp_path / "package" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "adhoc.yaml").write_text(
            "name: adhoc\nsteps:\n  - name: task\n"
            f"    prompt: p\n{effort_line}"
        )

    def test_valid_step_effort_passes(self, tmp_path):
        self._write_workflow(tmp_path, "    effort: high\n")
        assert self._check(Config(), tmp_path) == []

    def test_bogus_step_effort_warns(self, tmp_path):
        self._write_workflow(tmp_path, "    effort: hihg\n")
        results = self._check(Config(), tmp_path)
        assert len(results) == 1
        assert results[0].name == "adhoc.yaml:task.effort"
        assert not results[0].ok
        assert not results[0].required

    def test_malformed_workflow_skipped(self, tmp_path):
        wf_dir = tmp_path / "package" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "bad.yaml").write_text("steps: [unclosed\n")
        assert self._check(Config(), tmp_path) == []

    def test_no_workflows_dir_passes(self, tmp_path):
        assert self._check(Config(), tmp_path) == []


class TestCheckBrain:
    """A gateway team without a resolvable base_url fails every session at its
    first turn, so validate must block it (#655, #789)."""

    def _check(self, cfg):
        from bobi.validate import _check_brain
        return _check_brain(cfg)

    def _gateway_results(self, cfg):
        """The non-deprecation results (alias kinds add an advisory check)."""
        return [r for r in self._check(cfg) if r.name != "brain.kind"]

    def test_engine_kind_with_base_url_passes(self):
        for kind, name in (("claude", "brain.gateway"),
                           ("codex", "brain.gateway_openai")):
            cfg = Config(brain={"kind": kind,
                                "base_url": "http://localhost:4000"})
            results = self._check(cfg)
            assert len(results) == 1
            assert results[0].ok
            assert results[0].name == name

    def test_alias_kind_passes_with_deprecation_notice(self):
        cfg = Config(brain={"kind": "gateway",
                            "base_url": "http://localhost:4000"})
        results = self._check(cfg)
        assert [r.name for r in results] == ["brain.kind", "brain.gateway"]
        assert all(r.ok for r in results)
        assert "deprecated" in results[0].detail
        assert "kind: claude" in results[0].hint

    def test_alias_kind_without_base_url_blocks(self):
        cfg = Config(brain={"kind": "gateway", "model": "qwen3:14b"})
        results = self._gateway_results(cfg)
        assert len(results) == 1
        assert not results[0].ok
        assert results[0].required  # blocking, not a warning
        assert "base_url" in results[0].detail

    def test_uninterpolated_base_url_blocks(self):
        # Config interpolation turns an unset ${VAR} into "" - same failure,
        # in both spellings (presence of the key declares the gateway).
        for brain in ({"kind": "gateway", "base_url": ""},
                      {"kind": "claude", "base_url": ""},
                      {"base_url": ""}):
            assert not self._gateway_results(Config(brain=brain))[0].ok

    def test_gateway_openai_alias_with_base_url_passes(self):
        cfg = Config(brain={"kind": "gateway-openai",
                            "base_url": "http://localhost:9000/v1"})
        results = self._gateway_results(cfg)
        assert len(results) == 1
        assert results[0].ok
        assert results[0].name == "brain.gateway_openai"

    def test_gateway_openai_alias_without_base_url_blocks(self):
        cfg = Config(brain={"kind": "gateway-openai", "model": "gpt-5.5"})
        results = self._gateway_results(cfg)
        assert len(results) == 1
        assert not results[0].ok
        assert results[0].required
        assert "base_url" in results[0].detail

    def test_codex_gateway_invalid_wire_api_blocks(self):
        for kind in ("codex", "gateway-openai"):
            cfg = Config(brain={"kind": kind,
                                "base_url": "http://localhost:9000/v1",
                                "wire_api": "legacy"})
            results = self._gateway_results(cfg)
            assert len(results) == 1
            assert not results[0].ok
            assert "wire_api" in results[0].detail

    def test_base_url_on_non_engine_kind_warns_ignored(self):
        cfg = Config(brain={"kind": "stub",
                            "base_url": "http://localhost:4000"})
        results = self._check(cfg)
        assert len(results) == 1
        assert results[0].ok  # advisory, not blocking
        assert "ignored" in results[0].detail

    def test_native_brains_are_not_checked(self):
        assert self._check(Config()) == []
        assert self._check(Config(brain={"kind": "codex"})) == []
        # unknown kinds fail loud at get_brain(), not here
        assert self._check(Config(brain={"kind": "gemini"})) == []


class TestCheckMonitorRelevance:
    """relevance: on an ungateable monitor flavor is silently ignored at
    runtime, so validate must surface it (#630)."""

    def _check(self, tmp_path, monitors):
        import yaml
        from bobi.validate import _check_monitor_relevance
        pkg = tmp_path / "package"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "agent.yaml").write_text("entry_point: manager\n")
        (pkg / "monitors.yaml").write_text(yaml.dump({"monitors": monitors}))
        return _check_monitor_relevance(tmp_path)

    def test_relevance_on_check_monitor_passes(self, tmp_path):
        results = self._check(tmp_path, [
            {"name": "billing", "check": "venn_poll", "interval": "5m",
             "relevance": "about billing"}])
        assert results == []

    def test_relevance_on_command_monitor_passes(self, tmp_path):
        results = self._check(tmp_path, [
            {"name": "billing", "command": "echo '[]'",
             "relevance": "about billing"}])
        assert results == []

    def test_relevance_on_description_only_warns(self, tmp_path):
        results = self._check(tmp_path, [
            {"name": "watch", "description": "watch the inbox",
             "relevance": "about billing"}])
        assert len(results) == 1
        assert not results[0].ok
        assert not results[0].required  # warning, not blocking
        assert "ignored" in results[0].detail

    def test_relevance_on_notify_warns(self, tmp_path):
        results = self._check(tmp_path, [
            {"name": "roundup", "notify": True, "command": "echo '[]'",
             "relevance": "about billing"}])
        assert len(results) == 1
        assert not results[0].ok

    def test_relevance_on_command_plus_curator_passes(self, tmp_path):
        """run_monitor's elif chain routes command before curator, so this
        combo IS gated at runtime - validate must not claim otherwise."""
        results = self._check(tmp_path, [
            {"name": "combo", "command": "echo '[]'", "curator": True,
             "relevance": "about billing"}])
        assert results == []

    def test_no_relevance_no_warnings(self, tmp_path):
        results = self._check(tmp_path, [
            {"name": "watch", "description": "watch the inbox"}])
        assert results == []


class TestCheckServiceCredentials:

    def test_slack_with_token(self):
        cfg = Config(
            services=[ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"})],
        )
        checks = _check_service_credentials(cfg)
        assert len(checks) == 1
        assert checks[0].ok
        assert checks[0].detail == "native"

    def test_slack_missing_token(self):
        cfg = Config(
            services=[ServiceConfig(name="slack", credentials={"bot_token": ""})],
        )
        checks = _check_service_credentials(cfg)
        assert len(checks) == 1
        assert not checks[0].ok
        assert "native" in checks[0].detail
        assert "missing" in checks[0].detail

    def test_linear_with_key(self):
        cfg = Config(
            services=[ServiceConfig(name="linear", credentials={"api_key": "lin_test"})],
        )
        checks = _check_service_credentials(cfg)
        assert len(checks) == 1
        assert checks[0].ok
        assert checks[0].detail == "native"

    def test_linear_missing_key(self):
        cfg = Config(
            services=[ServiceConfig(name="linear", credentials={"api_key": ""})],
        )
        checks = _check_service_credentials(cfg)
        assert not checks[0].ok

    def test_github_always_ok(self):
        cfg = Config(services=[ServiceConfig(name="github")])
        checks = _check_service_credentials(cfg)
        assert checks[0].ok
        assert checks[0].detail == "native"

    def test_no_registered_services(self):
        cfg = Config(services=[ServiceConfig(name="email")])
        checks = _check_service_credentials(cfg)
        assert checks == []

    def test_missing_creds_optional_service_is_warning(self):
        # A non-required service with missing creds fails but does not block.
        cfg = Config(
            services=[ServiceConfig(name="slack", credentials={"bot_token": ""})],
        )
        checks = _check_service_credentials(cfg)
        assert not checks[0].ok
        assert checks[0].required is False

    def test_missing_creds_required_service_blocks(self):
        cfg = Config(
            services=[ServiceConfig(
                name="slack", required=True, credentials={"bot_token": ""},
            )],
        )
        checks = _check_service_credentials(cfg)
        assert not checks[0].ok
        assert checks[0].required is True


class TestCheckVennServices:

    @patch("bobi.venn.check_services")
    def test_all_connected(self, mock_check):
        from bobi.venn import ServiceCheck
        mock_check.return_value = ServiceCheck(connected=["email", "calendar"], missing=[])

        cfg = Config(
            services=[ServiceConfig(name="email"), ServiceConfig(name="calendar")],
            venn_api_key="venn_test",
        )
        checks = _check_venn_services(cfg)
        assert all(c.ok for c in checks)
        assert len(checks) == 2

    @patch("bobi.venn.check_services")
    def test_missing_service(self, mock_check):
        from bobi.venn import ServiceCheck
        mock_check.return_value = ServiceCheck(connected=["email"], missing=["salesforce"])

        cfg = Config(
            services=[ServiceConfig(name="email"), ServiceConfig(name="salesforce")],
            venn_api_key="venn_test",
        )
        checks = _check_venn_services(cfg)
        assert len(checks) == 2
        ok_names = {c.name for c in checks if c.ok}
        fail_names = {c.name for c in checks if not c.ok}
        assert "email" in ok_names
        assert "salesforce" in fail_names

    def test_no_venn_key(self):
        cfg = Config(
            services=[ServiceConfig(name="email")],
            venn_api_key="",
        )
        checks = _check_venn_services(cfg)
        assert len(checks) == 1
        assert not checks[0].ok
        assert "no API key" in checks[0].detail

    def test_no_venn_key_optional_is_warning(self):
        # The no-API-key branch: optional venn service warns, required blocks.
        cfg = Config(
            services=[
                ServiceConfig(name="email"),
                ServiceConfig(name="salesforce", required=True),
            ],
            venn_api_key="",
        )
        checks = {c.name: c for c in _check_venn_services(cfg)}
        assert checks["email"].required is False
        assert checks["salesforce"].required is True

    @patch("bobi.venn.check_services")
    def test_missing_service_carries_required(self, mock_check):
        # The live-`missing` branch: per-service required is threaded through.
        from bobi.venn import ServiceCheck
        mock_check.return_value = ServiceCheck(connected=[], missing=["email", "salesforce"])

        cfg = Config(
            services=[
                ServiceConfig(name="email"),
                ServiceConfig(name="salesforce", required=True),
            ],
            venn_api_key="venn_test",
        )
        checks = {c.name: c for c in _check_venn_services(cfg)}
        assert checks["email"].required is False
        assert checks["salesforce"].required is True

    @patch("bobi.venn.check_services")
    def test_missing_service_duplicate_name_fails_safe(self, mock_check):
        # A name declared twice (required + optional) must block: a required
        # declaration anywhere wins, regardless of declaration order.
        from bobi.venn import ServiceCheck
        mock_check.return_value = ServiceCheck(connected=[], missing=["email"])

        cfg = Config(
            services=[
                ServiceConfig(name="email", required=True),
                ServiceConfig(name="email"),  # optional, declared last
            ],
            venn_api_key="venn_test",
        )
        checks = _check_venn_services(cfg)
        assert all(c.required for c in checks if not c.ok)

    def test_no_venn_services(self):
        cfg = Config(services=[ServiceConfig(name="github")])
        checks = _check_venn_services(cfg)
        assert checks == []


class TestCheckMcpServers:

    def test_http_missing_url(self, tmp_path):
        cfg = Config(mcp_servers={"bad": {"type": "http"}})
        checks = _check_mcp_servers(cfg, tmp_path)
        assert len(checks) == 1
        assert not checks[0].ok
        assert checks[0].name == "bad"
        assert "missing url" in checks[0].detail

    def test_stdio_missing_command(self, tmp_path):
        cfg = Config(mcp_servers={"bad": {"type": "stdio"}})
        checks = _check_mcp_servers(cfg, tmp_path)
        assert not checks[0].ok
        assert checks[0].name == "bad"
        assert "missing command" in checks[0].detail

    def test_no_mcp_servers(self, tmp_path):
        cfg = Config()
        checks = _check_mcp_servers(cfg, tmp_path)
        assert checks == []


class TestValidateConfig:

    def test_minimal_valid_config(self, tmp_path):
        config_dir = tmp_path / "package"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(dedent("""\
            entry_point: manager
            services:
              - name: github
        """))

        result = validate_config(tmp_path)
        assert result.ok

    def test_format_output(self):
        result = MagicMock()
        result.checks = [
            CheckResult("github", ok=True, detail="native"),
            CheckResult("venn (email)", ok=False, detail="not connected", hint="Connect at venn.ai"),
        ]
        from bobi.validate import ValidationResult
        vr = ValidationResult(ok=False, checks=result.checks)
        output = vr.format()
        assert "✓" in output
        assert "✗" in output
        assert "venn.ai" in output

    def test_ok_passes_when_only_optional_failures(self):
        from bobi.validate import ValidationResult
        checks = [
            CheckResult("github", ok=True, detail="native"),
            CheckResult("email", ok=False, detail="venn — not connected", required=False),
        ]
        vr = ValidationResult(
            ok=not any((not c.ok) and c.required for c in checks),
            checks=checks,
        )
        assert vr.ok is True

    def test_ok_blocks_on_required_failure(self):
        from bobi.validate import ValidationResult
        checks = [
            CheckResult("email", ok=False, detail="venn — not connected", required=False),
            CheckResult("slack", ok=False, detail="native — missing bot_token", required=True),
        ]
        vr = ValidationResult(
            ok=not any((not c.ok) and c.required for c in checks),
            checks=checks,
        )
        assert vr.ok is False

    def test_format_warns_on_optional_blocks_on_required(self):
        from bobi.validate import ValidationResult
        checks = [
            CheckResult("github", ok=True, detail="native"),
            CheckResult("email", ok=False, detail="venn — not connected", required=False),
            CheckResult("slack", ok=False, detail="native — missing bot_token", required=True),
        ]
        output = ValidationResult(ok=False, checks=checks).format()
        lines = {line.split()[1]: line for line in output.splitlines() if line.strip() and not line.strip().startswith("→")}
        assert lines["github"].lstrip().startswith("✓")
        assert lines["email"].lstrip().startswith("⚠")   # optional → warning
        assert lines["slack"].lstrip().startswith("✗")   # required → error

    def test_pack_with_optional_venn_service_starts_degraded(self, tmp_path):
        # github (native, zero-config) + an unconfigured venn service explicitly
        # marked required: false: validate_config must return ok=True so
        # Named start proceeds and the optional service degrades.
        config_dir = tmp_path / "package"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(dedent("""\
            entry_point: manager
            services:
              - name: github
                events: true
              - name: email
                events: true
                required: false
        """))
        result = validate_config(tmp_path)
        assert result.ok is True
        email = next(c for c in result.checks if c.name == "email")
        assert not email.ok
        assert email.required is False

    def test_pack_with_required_venn_service_blocks(self, tmp_path):
        # Mirrors the dogfood-content-review decision (#329 / PR #405): a venn
        # service marked required: true (dogfood's email) hard-blocks startup
        # when its credential is missing — it does NOT degrade.
        config_dir = tmp_path / "package"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(dedent("""\
            entry_point: manager
            services:
              - name: github
                events: true
                required: true
              - name: email
                events: true
                required: true
        """))
        result = validate_config(tmp_path)
        assert result.ok is False
        email = next(c for c in result.checks if c.name == "email")
        assert not email.ok
        assert email.required is True
