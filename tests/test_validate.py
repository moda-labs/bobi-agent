"""Tests for startup config validation."""

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch, MagicMock

from modastack.validate import (
    validate_config,
    _check_entry_point,
    _check_service_credentials,
    _check_venn_services,
    _check_mcp_servers,
    status_glyph,
    supports_unicode,
    CheckResult,
)
from modastack.config import Config, ServiceConfig


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
        from modastack.validate import ValidationResult
        checks = [
            CheckResult("github", ok=True, detail="native"),
            CheckResult("email", ok=False, detail="venn — not connected", required=False),
            CheckResult("slack", ok=False, detail="native — missing bot_token", required=True),
        ]
        vr = ValidationResult(ok=False, checks=checks)
        with patch("modastack.validate.supports_unicode", return_value=False):
            output = vr.format()
        assert "[OK]" in output
        assert "[WARN]" in output   # optional failure
        assert "[ERROR]" in output  # required failure
        assert "✓" not in output and "⚠" not in output and "✗" not in output


class TestCheckEntryPoint:

    def test_valid_role(self, tmp_path):
        (tmp_path / ".modastack" / "roles" / "director").mkdir(parents=True)
        cfg = Config(entry_point="director")
        result = _check_entry_point(cfg, tmp_path)
        assert result.ok

    def test_missing_role(self, tmp_path):
        (tmp_path / ".modastack" / "roles" / "engineer").mkdir(parents=True)
        cfg = Config(entry_point="director")
        result = _check_entry_point(cfg, tmp_path)
        assert not result.ok
        assert "not found" in result.detail

    def test_empty_entry_point_defaults(self, tmp_path):
        cfg = Config(entry_point="")
        result = _check_entry_point(cfg, tmp_path)
        assert result.ok
        assert "defaulting" in result.detail


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

    @patch("modastack.venn.check_services")
    def test_all_connected(self, mock_check):
        from modastack.venn import ServiceCheck
        mock_check.return_value = ServiceCheck(connected=["email", "calendar"], missing=[])

        cfg = Config(
            services=[ServiceConfig(name="email"), ServiceConfig(name="calendar")],
            venn_api_key="venn_test",
        )
        checks = _check_venn_services(cfg)
        assert all(c.ok for c in checks)
        assert len(checks) == 2

    @patch("modastack.venn.check_services")
    def test_missing_service(self, mock_check):
        from modastack.venn import ServiceCheck
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

    @patch("modastack.venn.check_services")
    def test_missing_service_carries_required(self, mock_check):
        # The live-`missing` branch: per-service required is threaded through.
        from modastack.venn import ServiceCheck
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

    @patch("modastack.venn.check_services")
    def test_missing_service_duplicate_name_fails_safe(self, mock_check):
        # A name declared twice (required + optional) must block: a required
        # declaration anywhere wins, regardless of declaration order.
        from modastack.venn import ServiceCheck
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
        config_dir = tmp_path / ".modastack"
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
        from modastack.validate import ValidationResult
        vr = ValidationResult(ok=False, checks=result.checks)
        output = vr.format()
        assert "✓" in output
        assert "✗" in output
        assert "venn.ai" in output

    def test_ok_passes_when_only_optional_failures(self):
        from modastack.validate import ValidationResult
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
        from modastack.validate import ValidationResult
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
        from modastack.validate import ValidationResult
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
        # `modastack start` proceeds and the optional service degrades.
        config_dir = tmp_path / ".modastack"
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
        config_dir = tmp_path / ".modastack"
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
