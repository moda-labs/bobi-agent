"""Regression tests for the legacy ``modastack`` CLI entrypoint."""

import tomllib
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner


ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_keeps_legacy_modastack_console_script():
    """Fresh installs must still create a working ``modastack`` command."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert pyproject["project"]["scripts"]["bobi"] == "bobi.cli:main"
    assert pyproject["project"]["scripts"]["modastack"] == "bobi.cli:main"


def test_legacy_modastack_cli_module_delegates_to_bobi():
    """Stale wrappers importing ``modastack.cli`` should keep working."""
    from bobi.cli import main as bobi_main
    from modastack.cli import main

    assert main is bobi_main

    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "Usage: main" in result.output
    assert "Bobi" in result.output


def test_legacy_modastack_cli_module_executes():
    """``python -m modastack.cli`` should keep invoking the CLI."""
    result = subprocess.run(
        [sys.executable, "-m", "modastack.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: python -m modastack.cli" in result.stdout
    assert "Bobi" in result.stdout
