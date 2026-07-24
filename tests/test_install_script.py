"""Installer prerequisite coverage for the embedded local event server."""

import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _run_installer(fake_bin: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": str(fake_bin),
        }
    )
    return subprocess.run(
        ["/bin/bash", str(INSTALL_SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_installer_fails_before_install_when_node_is_missing(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode != 0
    assert "Node.js 20+" in result.stderr
    assert "not found on PATH" in result.stderr


def test_installer_rejects_unsupported_node_before_uv(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v18.20.0\\n'\n")
    uv_trace = tmp_path / "uv-trace"
    _write_executable(fake_bin / "uv", f"touch {uv_trace}\n")

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode != 0
    assert "found v18.20.0" in result.stderr
    assert "Node.js 20+" in result.stderr
    assert not uv_trace.exists()


def test_installer_accepts_node_20_and_installs_with_uv(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    uv_trace = tmp_path / "uv-trace"
    _write_executable(
        fake_bin / "uv",
        f"printf '%s\\n' \"$*\" > {uv_trace}\n",
    )

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode == 0, result.stderr
    assert uv_trace.read_text().strip() == "tool install bobi"
    assert "Done." in result.stdout
