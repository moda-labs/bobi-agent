"""Tests for CLI commands."""

from click.testing import CliRunner

from dispatch.cli import main


def test_version_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "dispatch" in result.output
    assert "0.1.0" in result.output
