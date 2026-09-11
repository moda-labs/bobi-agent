"""Consumer-side checks executed against the downloaded Bobi wheel."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import pytest

import importlib.util

_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "smoke_diy_install.py"
_SPEC = importlib.util.spec_from_file_location("smoke_diy_install", _HELPER)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_child_env = _MODULE.build_child_env
run_checked = _MODULE.run_checked


@pytest.fixture(scope="module")
def subject(tmp_path_factory):
    wheel = os.environ.get("BOBI_TEST_WHEEL")
    if not wheel:
        pytest.fail("BOBI_TEST_WHEEL is required; refusing an unverified source checkout")
    root = tmp_path_factory.mktemp("consumer")
    venv = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin" / "python"
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", wheel], check=True)
    env = build_child_env(root, venv / "bin")
    return root, venv, python, env


def test_wheel_identity_and_version(subject):
    _, venv, python, env = subject
    result = run_checked([str(venv / "bin" / "bobi"), "--version"], env=env, cwd=subject[0])
    assert result.stdout.strip()
    probe = run_checked([str(python), "-c", "import importlib.metadata as m; d=m.distribution('bobi'); print(d.version); print(d.locate_file('bobi'))"], env=env, cwd=subject[0])
    assert probe.stdout.splitlines()[0] == result.stdout.strip().split()[-1]
    assert str(venv) in probe.stdout.splitlines()[1]


def test_doctor_fresh_install(subject):
    result = run_checked([str(subject[2]), "-m", "bobi.cli", "doctor"], env=subject[3], cwd=subject[0], expected_exit=1)
    assert "Claude" in (result.stdout + result.stderr)
    assert "Traceback" not in result.stderr


def test_doctor_without_node(subject):
    env = dict(subject[3], PATH=str(subject[1] / "bin"))
    result = run_checked([str(subject[1] / "bin" / "bobi"), "doctor"], env=env, cwd=subject[0], expected_exit=1)
    assert "Node" in (result.stdout + result.stderr)


def test_doctor_with_old_node(subject):
    node18 = os.environ.get("BOBI_DIY_NODE18")
    if not node18:
        pytest.fail("BOBI_DIY_NODE18 is required; refusing to test an ambient Node runtime")
    env = dict(subject[3], PATH=f"{Path(node18).parent}{os.pathsep}{subject[1] / 'bin'}")
    result = run_checked([str(subject[1] / "bin" / "bobi"), "doctor"], env=env, cwd=subject[0], expected_exit=1)
    assert "20" in (result.stdout + result.stderr)


def test_setup_shell_without_claude(subject):
    result = run_checked([str(subject[1] / "bin" / "bobi"), "setup"], env=subject[3], cwd=subject[0])
    assert "bobi setup is open at" in result.stdout
    run_checked([str(subject[1] / "bin" / "bobi"), "app", "stop", "--force"], env=subject[3], cwd=subject[0])


def test_setup_hosted_assets_offline(subject):
    result = run_checked([str(subject[1] / "bin" / "bobi"), "setup"], env=subject[3], cwd=subject[0])
    assert "127.0.0.1" in result.stdout
    run_checked([str(subject[1] / "bin" / "bobi"), "app", "stop", "--force"], env=subject[3], cwd=subject[0])


def test_readonly_framework_startup(subject):
    package = Path(subject[1] / "lib").rglob("bobi/__init__.py")
    target = next(package, None)
    assert target is not None
    mode = target.stat().st_mode
    target.chmod(mode & 0o555)
    try:
        run_checked([str(subject[1] / "bin" / "bobi"), "--version"], env=subject[3], cwd=subject[0])
    finally:
        target.chmod(mode)


def test_unreadable_framework_diagnostic(subject):
    package = next((subject[1] / "lib").rglob("bobi/__init__.py"))
    mode = package.stat().st_mode
    package.chmod(0)
    try:
        result = run_checked([str(subject[1] / "bin" / "bobi"), "doctor"], env=subject[3], cwd=subject[0], expected_exit=1)
        assert "Bobi install" in (result.stdout + result.stderr)
    finally:
        package.chmod(mode)


def test_guard_keeps_pip_reinstall_working(subject):
    subprocess.run([str(subject[2]), "-m", "pip", "install", "--quiet", "--force-reinstall", "--no-deps", os.environ["BOBI_TEST_WHEEL"]], check=True, env=subject[3])
    run_checked([str(subject[1] / "bin" / "bobi"), "--version"], env=subject[3], cwd=subject[0])
