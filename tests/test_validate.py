"""Tests for startup config validation."""

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch, MagicMock

from modastack.validate import (
    validate_config,
    _check_entry_point,
    _check_native_credentials,
    _check_venn_services,
    _check_mcp_servers,
    CheckResult,
)
from modastack.config import Config, ServiceConfig


class TestCheckEntryPoint:

    def test_valid_role(self, tmp_path):
        (tmp_path / ".modastack" / "roles" / "director").mkdir(parents=True)
        cfg = Config(entry_point="director")
        result = _check_entry_point(cfg, tmp_path, "test")
        assert result.ok

    def test_missing_role(self, tmp_path):
        (tmp_path / ".modastack" / "roles" / "engineer").mkdir(parents=True)
        cfg = Config(entry_point="director")
        result = _check_entry_point(cfg, tmp_path, "test")
        assert not result.ok
        assert "not found" in result.detail

    def test_empty_entry_point_defaults(self, tmp_path):
        cfg = Config(entry_point="")
        result = _check_entry_point(cfg, tmp_path, None)
        assert result.ok
        assert "defaulting" in result.detail


class TestCheckNativeCredentials:

    def test_slack_with_token(self):
        cfg = Config(
            services=[ServiceConfig(name="slack")],
            slack_bot_token="xoxb-test",
        )
        checks = _check_native_credentials(cfg)
        assert len(checks) == 1
        assert checks[0].ok
        assert checks[0].detail == "native"

    def test_slack_missing_token(self):
        cfg = Config(services=[ServiceConfig(name="slack")])
        checks = _check_native_credentials(cfg)
        assert len(checks) == 1
        assert not checks[0].ok
        assert "native" in checks[0].detail
        assert "missing" in checks[0].detail

    def test_linear_with_key(self):
        cfg = Config(
            services=[ServiceConfig(name="linear")],
            linear_api_key="lin_test",
        )
        checks = _check_native_credentials(cfg)
        assert len(checks) == 1
        assert checks[0].ok
        assert checks[0].detail == "native"

    def test_linear_missing_key(self):
        cfg = Config(services=[ServiceConfig(name="linear")])
        checks = _check_native_credentials(cfg)
        assert not checks[0].ok

    def test_github_always_ok(self):
        cfg = Config(services=[ServiceConfig(name="github")])
        checks = _check_native_credentials(cfg)
        assert checks[0].ok
        assert checks[0].detail == "native"

    def test_no_native_services(self):
        cfg = Config(services=[ServiceConfig(name="email")])
        checks = _check_native_credentials(cfg)
        assert checks == []


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
