"""Real package-manager regressions for the runtime guard release window."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run(
    args: list[str], *, env: dict[str, str], timeout: int = 180,
    cwd: Path = PROJECT_ROOT,
):
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def bobi_wheel(tmp_path_factory) -> Path:
    dist = tmp_path_factory.mktemp("guard-wheel")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(dist.glob("bobi-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _uv_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "bobi-home"
    tool_dir = tmp_path / "uv-tools"
    bin_dir = tmp_path / "uv-bin"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update({
        "BOBI_HOME": str(home),
        "UV_TOOL_DIR": str(tool_dir),
        "UV_TOOL_BIN_DIR": str(bin_dir),
        "PYTHONSAFEPATH": "1",
    })
    return env, tool_dir, bin_dir


def _apply_installed_guard(venv: Path, env: dict[str, str]) -> None:
    result = _run([
        str(venv / "bin" / "python"),
        "-c",
        "from bobi.runtime_guard import apply_runtime_write_policy; "
        "r=apply_runtime_write_policy(None); "
        "assert any(x.kind == 'bobi-package' for x in r.protected)",
    ], env=env)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX chmod policy")
def test_uv_force_reinstall_survives_agent_launch_inside_release_window(
    tmp_path, bobi_wheel,
):
    uv = shutil.which("uv")
    assert uv, "uv is required for the guard upgrade regression"
    env, tool_dir, bin_dir = _uv_env(tmp_path)
    install = [uv, "tool", "install", "--force", str(bobi_wheel)]

    first = _run(install, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    _apply_installed_guard(tool_dir / "bobi", env)

    before = _run(install, env=env)
    assert before.returncode != 0
    assert "Permission denied" in before.stderr

    tool_root = tool_dir / "bobi"
    for path in [*tool_root.rglob("*"), tool_root]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
    repaired = _run(install, env=env)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    _apply_installed_guard(tool_dir / "bobi", env)

    release = _run([str(bin_dir / "bobi"), "guard", "release"], env=env)
    assert release.returncode == 0, release.stdout + release.stderr
    marker_path = Path(env["BOBI_HOME"]) / "runtime-guard-released"
    marker = json.loads(marker_path.read_text())
    assert marker["prefix"] == str(tool_root)

    launch = _run([
        str(tool_root / "bin" / "python"),
        "-c",
        "from bobi.runtime_guard import prepare_brain_runtime; "
        "r=prepare_brain_runtime(); "
        "assert any(x.kind == 'bobi-package' for x in r.released)",
    ], env=env)
    assert launch.returncode == 0, launch.stdout + launch.stderr

    after = _run(install, env=env)
    assert after.returncode == 0, after.stdout + after.stderr
    upgraded = _run(
        [uv, "tool", "upgrade", "--reinstall", "bobi"], env=env)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    version = _run([str(bin_dir / "bobi"), "--version"], env=env)
    assert version.returncode == 0, version.stdout + version.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX chmod policy")
def test_pipx_reinstall_replaces_guarded_framework_after_release(
    tmp_path, bobi_wheel,
):
    pipx = shutil.which("pipx")
    assert pipx, "pipx is required for the guard upgrade regression"
    pipx_home = tmp_path / "pipx-home"
    bin_dir = tmp_path / "pipx-bin"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update({
        "BOBI_HOME": str(tmp_path / "bobi-home"),
        "PIPX_HOME": str(pipx_home),
        "PIPX_BIN_DIR": str(bin_dir),
        "PYTHONSAFEPATH": "1",
    })

    installed = _run([pipx, "install", "--force", str(bobi_wheel)], env=env)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    venv = pipx_home / "venvs" / "bobi"
    _apply_installed_guard(venv, env)

    release = _run([str(bin_dir / "bobi"), "guard", "release"], env=env)
    assert release.returncode == 0, release.stdout + release.stderr
    upgraded = _run(
        [pipx, "upgrade", "--force", "bobi"], env=env, cwd=tmp_path)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert not list(venv.glob("lib/python*/site-packages/~obi*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX chmod policy")
def test_pip_force_reinstall_leaves_no_stale_bobi_after_release(
    tmp_path, bobi_wheel,
):
    venv = tmp_path / "venv"
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        capture_output=True, text=True, timeout=60,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update({
        "BOBI_HOME": str(tmp_path / "bobi-home"),
        "PYTHONSAFEPATH": "1",
    })
    python = venv / "bin" / "python"
    installed = _run(
        [str(python), "-m", "pip", "install", str(bobi_wheel)],
        env=env, cwd=tmp_path,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    _apply_installed_guard(venv, env)

    release = _run([str(venv / "bin" / "bobi"), "guard", "release"], env=env)
    assert release.returncode == 0, release.stdout + release.stderr
    reinstalled = _run(
        [str(python), "-m", "pip", "install", "--force-reinstall", str(bobi_wheel)],
        env=env, cwd=tmp_path,
    )

    assert reinstalled.returncode == 0, reinstalled.stdout + reinstalled.stderr
    site_packages = next(venv.glob("lib/python*/site-packages"))
    assert not list(site_packages.glob("~obi*"))
