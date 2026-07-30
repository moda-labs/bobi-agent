"""Installer prerequisite coverage for the embedded local event server."""

import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _run_installer(fake_bin: Path, tmp_path: Path, *,
                   system_path: bool = False) -> subprocess.CompletedProcess[str]:
    # `system_path` keeps the real coreutils (mkdir, chmod) reachable while
    # `fake_bin` still shadows node and uv. The prerequisite tests need it off,
    # because they assert on a host where node genuinely is not installed.
    path = f"{fake_bin}:{os.environ['PATH']}" if system_path else str(fake_bin)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": path,
            # Pin it: the installer writes the guard release marker under
            # BOBI_HOME, and an inherited one would land in the developer's
            # live runtime.
            "BOBI_HOME": str(tmp_path / "home" / ".bobi"),
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


def _fake_uv(fake_bin: Path, trace: Path, tool_dir: Path | None = None) -> None:
    """A `uv` shim that records every invocation and can answer `uv tool dir`."""
    dir_reply = f'    printf \'%s\\n\' "{tool_dir}"\n' if tool_dir else "    :\n"
    _write_executable(
        fake_bin / "uv",
        f'printf \'%s\\n\' "$*" >> {trace}\n'
        'if [ "$1 $2" = "tool dir" ]; then\n'
        f"{dir_reply}"
        "fi\n",
    )


def test_installer_accepts_node_20_and_forces_the_install(tmp_path):
    """--force is what makes the installer an upgrade and a repair.

    A guard-aborted upgrade leaves a gutted environment whose uv receipt still
    matches, so a plain `uv tool install bobi` exits 0 without rebuilding it.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    uv_trace = tmp_path / "uv-trace"
    _fake_uv(fake_bin, uv_trace)

    result = _run_installer(fake_bin, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "tool install --force bobi" in uv_trace.read_text()
    assert "Done." in result.stdout


def test_installer_unlocks_a_guarded_install_before_upgrading(tmp_path):
    """#858: the guard's read-only sweep makes the tree undeletable.

    The installer is the only recovery path that works once an aborted upgrade
    has left `bobi` unable to import, so it must unlock without running `bobi`.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    tool_dir = tmp_path / "uv-tools"
    guarded = tool_dir / "bobi" / "lib" / "site-packages" / "bobi"
    guarded.mkdir(parents=True)
    (guarded / "cli.py").write_text("main = None\n")
    for path in [guarded / "cli.py", guarded]:
        path.chmod(path.stat().st_mode & ~0o222)
    _fake_uv(fake_bin, tmp_path / "uv-trace", tool_dir)

    result = _run_installer(fake_bin, tmp_path, system_path=True)

    assert result.returncode == 0, result.stderr
    assert guarded.stat().st_mode & 0o200, "package directory left undeletable"
    assert (guarded / "cli.py").stat().st_mode & 0o200


def test_installer_opens_the_release_window_before_uv_runs(tmp_path):
    """A running team re-locks the tree on its next agent launch.

    Monitor ticks land every 30s, well inside the time uv needs to resolve,
    build, and swap the tree, so unlocking alone is a race the operator loses.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "node", "printf 'v20.19.2\\n'\n")
    _fake_uv(fake_bin, tmp_path / "uv-trace")

    result = _run_installer(fake_bin, tmp_path, system_path=True)

    assert result.returncode == 0, result.stderr
    marker = tmp_path / "home" / ".bobi" / "runtime-guard-released"
    assert marker.exists(), "installer did not open the guard release window"
