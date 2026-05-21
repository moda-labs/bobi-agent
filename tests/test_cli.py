"""Tests for CLI commands."""

from click.testing import CliRunner

from modastack.cli import main


def test_version_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "modastack" in result.output
    assert "0.2.0" in result.output
