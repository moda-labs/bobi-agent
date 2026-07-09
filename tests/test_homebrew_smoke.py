"""Homebrew bottle-URL smoke gate (scripts/smoke-homebrew-bottles.sh).

Recovered at the repo split: these tests lived in test_gitops_c22.py, which
moved to the private deploy repo, but the script and the release.yml step it
guards stayed public. Issue #493 is the regression class: the tap formula had
a root_url whose release tag segment included `.arm64_sequoia.bottle.tar`,
so Homebrew constructed a 404 bottle URL and `brew install bobi` broke after
an otherwise-green release.
"""

import os
import subprocess
from pathlib import Path

from tests.workflow_utils import load_workflow

REPO = Path(__file__).resolve().parent.parent
HOMEBREW_SMOKE_SH = REPO / "scripts" / "smoke-homebrew-bottles.sh"


def test_homebrew_smoke_script_passes_shellcheck():
    sc = subprocess.run(
        ["shellcheck", str(HOMEBREW_SMOKE_SH)], capture_output=True, text=True
    )
    assert sc.returncode == 0, sc.stdout + sc.stderr


def test_release_smokes_homebrew_bottle_urls_after_dispatch():
    job = load_workflow("release.yml")["jobs"]["update-homebrew"]
    uses = "\n".join(str(step.get("uses", "")) for step in job["steps"])
    names = "\n".join(step.get("name", "") for step in job["steps"])
    scripts = "\n".join(step.get("run", "") for step in job["steps"])
    assert "actions/checkout" in uses
    assert "Smoke Homebrew bottle URLs" in names
    assert "scripts/smoke-homebrew-bottles.sh" in scripts


def _run_homebrew_smoke(formula: str, tmp_path: Path) -> subprocess.CompletedProcess:
    formula_path = tmp_path / "bobi.rb"
    formula_path.write_text(formula)
    env = {
        **os.environ,
        "BOBI_HOMEBREW_FORMULA_FILE": str(formula_path),
        "BOBI_HOMEBREW_SKIP_HEAD": "1",
        "BOBI_HOMEBREW_SMOKE_ATTEMPTS": "1",
        "BOBI_HOMEBREW_SMOKE_SLEEP": "0",
    }
    return subprocess.run(
        [str(HOMEBREW_SMOKE_SH), "0.33.0"],
        capture_output=True,
        text=True,
        env=env,
    )


def test_homebrew_smoke_accepts_valid_bottle_formula(tmp_path):
    formula = """
class Bobi < Formula
  url "https://files.pythonhosted.org/packages/bobi-0.33.0.tar.gz"
  bottle do
    root_url "https://github.com/moda-labs/homebrew-bobi-agent/releases/download/bobi-0.33.0"
    sha256 cellar: :any_skip_relocation, arm64_sequoia: "aaaaaaaa"
    sha256 cellar: :any_skip_relocation, arm64_sonoma: "bbbbbbbb"
  end
end
"""
    result = _run_homebrew_smoke(formula, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "bobi-0.33.0.arm64_sequoia.bottle.tar.gz" in result.stdout
    assert "bobi-0.33.0.arm64_sonoma.bottle.tar.gz" in result.stdout


def test_homebrew_smoke_rejects_current_version_malformed_root_url(tmp_path):
    formula = """
class Bobi < Formula
  url "https://files.pythonhosted.org/packages/bobi-0.33.0.tar.gz"
  bottle do
    root_url "https://github.com/moda-labs/homebrew-bobi-agent/releases/download/bobi-0.33.0.arm64_sequoia.bottle.tar"
    sha256 cellar: :any_skip_relocation, arm64_sequoia: "aaaaaaaa"
  end
end
"""
    result = _run_homebrew_smoke(formula, tmp_path)
    assert result.returncode == 1
    assert "root_url is malformed" in result.stdout


def test_homebrew_smoke_waits_for_incomplete_bottle_formula(tmp_path):
    formula = """
class Bobi < Formula
  url "https://files.pythonhosted.org/packages/bobi-0.33.0.tar.gz"
end
"""
    result = _run_homebrew_smoke(formula, tmp_path)
    assert result.returncode == 1
    assert "Timed out waiting" in result.stdout
