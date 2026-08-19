"""Installer prerequisite coverage for the embedded local event server."""

import os
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _run_installer(fake_bin: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("BOBI_HOME", None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join([str(fake_bin), "/usr/bin", "/bin"]),
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
    tool_dir = tmp_path / "uv-tools"
    _write_executable(fake_bin / "chmod", "exit 0\n")
    _write_executable(
        fake_bin / "uv",
        f"if [ \"$*\" = \"tool dir\" ]; then printf '%s\\n' {tool_dir}; "
        f"else printf '%s\\n' \"$*\" > {uv_trace}; fi\n",
    )

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode == 0, result.stderr
    assert uv_trace.read_text().strip() == "tool install --force bobi"
    assert not (tmp_path / "home" / ".bobi" / "runtime-guard-released").exists()


def test_installer_does_not_open_window_for_first_install(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    tool_dir = tmp_path / "uv-tools"
    observed = tmp_path / "marker-observed"
    _write_executable(
        fake_bin / "uv",
        f"if [ \"$*\" = \"tool dir\" ]; then printf '%s\\n' {tool_dir}; "
        f"elif [ -e \"$HOME/.bobi/runtime-guard-released\" ]; then touch {observed}; fi\n",
    )

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode == 0, result.stderr
    assert not observed.exists()
    assert "Done." in result.stdout


def test_installer_unlocks_existing_uv_tool_before_install(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    tool_dir = tmp_path / "uv-tools"
    (tool_dir / "bobi").mkdir(parents=True)
    trace = tmp_path / "trace"
    _write_executable(
        fake_bin / "chmod",
        f"printf 'chmod %s\\n' \"$*\" >> {trace}\n",
    )
    _write_executable(
        fake_bin / "uv",
        f"if [ \"$*\" = \"tool dir\" ]; then printf '%s\\n' {tool_dir}; "
        f"else printf 'uv %s\\n' \"$*\" >> {trace}; fi\n",
    )

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        f"chmod -R u+w {tool_dir}/bobi",
        "uv tool install --force bobi",
    ]
    assert not (tmp_path / "home" / ".bobi" / "runtime-guard-released").exists()


def test_installer_marker_covers_uv_tree_and_survives_failed_install(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    tool_dir = tmp_path / "uv-tools"
    (tool_dir / "bobi").mkdir(parents=True)
    marker_copy = tmp_path / "marker.json"
    _write_executable(fake_bin / "chmod", "exit 0\n")
    _write_executable(
        fake_bin / "uv",
        f"if [ \"$*\" = \"tool dir\" ]; then printf '%s\\n' {tool_dir}; "
        f"else cp \"$HOME/.bobi/runtime-guard-released\" {marker_copy}; exit 7; fi\n",
    )

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode == 7
    marker = __import__("json").loads(marker_copy.read_text())
    assert marker["prefix"] == str(tool_dir / "bobi")
    assert marker["opened_by"] == "scripts/install.sh"
    assert isinstance(marker["pid"], int)
    assert marker["expires_at"] > 0
    assert (tmp_path / "home" / ".bobi" / "runtime-guard-released").exists()


def test_installer_partial_chmod_failure_closes_window_and_relocks(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    tool_dir = tmp_path / "uv-tools"
    framework = tool_dir / "bobi" / "lib" / "python3.13" / "site-packages" / "bobi"
    framework.mkdir(parents=True)
    module = framework / "module.py"
    module.write_text("x = 1\n")
    framework.chmod(0o555)
    module.chmod(0o444)
    uv_install_called = tmp_path / "uv-install-called"
    _write_executable(
        fake_bin / "chmod",
        f"if [ \"$2\" = \"u+w\" ]; then /bin/chmod u+w {module}; exit 1; fi\n"
        "/bin/chmod \"$@\"\n",
    )
    _write_executable(
        fake_bin / "uv",
        f"if [ \"$*\" = \"tool dir\" ]; then printf '%s\\n' {tool_dir}; "
        f"else touch {uv_install_called}; fi\n",
    )

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode != 0
    assert not uv_install_called.exists()
    assert not (tmp_path / "home" / ".bobi" / "runtime-guard-released").exists()
    assert not module.stat().st_mode & 0o200


@pytest.mark.parametrize(
    ("configured_home", "expected_suffix"),
    [("~/custom-bobi", Path("home/custom-bobi")),
     ("relative-bobi", Path("relative-bobi"))],
)
def test_installer_resolves_relative_and_tilde_bobi_home_like_python(
    tmp_path, configured_home, expected_suffix,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    tool_dir = tmp_path / "uv-tools"
    (tool_dir / "bobi").mkdir(parents=True)
    marker_trace = tmp_path / "marker-path"
    expected_marker = tmp_path / expected_suffix / "runtime-guard-released"
    _write_executable(fake_bin / "chmod", "exit 0\n")
    _write_executable(
        fake_bin / "uv",
        f"if [ \"$*\" = \"tool dir\" ]; then printf '%s\\n' {tool_dir}; "
        f"elif [ -e {expected_marker} ]; then printf '%s\\n' {expected_marker} > {marker_trace}; "
        "else exit 9; fi\n",
    )

    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "BOBI_HOME": configured_home,
        "PATH": os.pathsep.join([str(fake_bin), "/usr/bin", "/bin"]),
    })
    result = subprocess.run(
        ["/bin/bash", str(INSTALL_SCRIPT)], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert marker_trace.read_text().strip() == str(expected_marker)
