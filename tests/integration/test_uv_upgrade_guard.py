"""#858 regression: the write guard must not block a real package-manager upgrade.

The failure lives in the package manager, not in Bobi: `uv` replaces a tool by
deleting its tree, and POSIX governs unlink by the *directory* write bit. A
mocked removal cannot prove the fix, because uv and pip already disagree about
it - uv aborts hard, pip renames the tree aside and exits 0. So this drives the
real `uv` against a real installation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="needs the uv CLI - this exercises the real package manager",
)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> str:
    """Build Bobi once, then install that artifact repeatedly.

    Installing from the source tree instead would rebuild the wheel on every
    `uv tool install`, which dominates the runtime of this test.
    """
    out = tmp_path_factory.mktemp("wheel")
    built = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out),
         str(PROJECT_ROOT)],
        capture_output=True, text=True, timeout=1800,
    )
    assert built.returncode == 0, f"wheel build failed\n{built.stderr}"
    wheels = list(out.glob("bobi-*.whl"))
    assert len(wheels) == 1, wheels
    return str(wheels[0])


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "UV_TOOL_DIR": str(tmp_path / "tools"),
            "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
            "BOBI_HOME": str(tmp_path / "home"),
        }
    )
    return env


def _uv(env: dict[str, str], *args: str, check: bool = True):
    result = subprocess.run(["uv", *args], env=env, capture_output=True,
                            text=True, timeout=900)
    if check:
        assert result.returncode == 0, f"uv {' '.join(args)}\n{result.stderr}"
    return result


def _installed(tmp_path: Path, env: dict[str, str], code: str):
    """Run code inside the installation under test.

    cwd is deliberately away from the repo: from the source tree `import bobi`
    resolves to the checkout, and the guard would then correctly decide there is
    nothing to protect.
    """
    return subprocess.run(
        [str(tmp_path / "tools" / "bobi" / "bin" / "python"), "-c", code],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )


def _agent_launch(tmp_path: Path, env: dict[str, str]) -> str:
    """Run the guard exactly as every agent launch does.

    Returns the roots it locked, so a caller can tell a real lock from a launch
    that deferred to an open release window.
    """
    result = _installed(
        tmp_path, env,
        "from bobi.runtime_guard import prepare_brain_runtime;"
        "r = prepare_brain_runtime();"
        "print('locked', sorted(x.kind for x in r.protected));"
        "print('released', sorted(x.kind for x in r.released))",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _lock(tmp_path: Path, env: dict[str, str]) -> None:
    output = _agent_launch(tmp_path, env)
    assert "locked ['bobi-dist-info', 'bobi-package']" in output, output


def _bobi_runs(tmp_path: Path, env: dict[str, str]) -> bool:
    result = subprocess.run([str(tmp_path / "bin" / "bobi"), "--version"],
                            cwd=tmp_path, env=env, capture_output=True,
                            text=True, timeout=300)
    return result.returncode == 0


@pytest.mark.timeout(1800)
def test_guarded_install_blocks_uv_upgrade_until_released(tmp_path, wheel):
    env = _env(tmp_path)
    _uv(env, "tool", "install", wheel)
    assert _bobi_runs(tmp_path, env)

    # 1. The reported failure. uv removes site-packages entry by entry and only
    #    stops at the locked package, so the executable and every dependency are
    #    already gone: the operator loses the CLI they would diagnose with.
    _lock(tmp_path, env)
    failed = _uv(env, "tool", "install", "--force", wheel,
                 check=False)
    assert failed.returncode != 0, "guard no longer blocks uv - is it still applied?"
    assert "Permission denied" in failed.stderr
    assert not _bobi_runs(tmp_path, env), "expected the aborted upgrade to break bobi"

    # 2. Recovery from that state cannot go through `bobi`, so it goes through
    #    the shell, which is what scripts/install.sh does. A plain
    #    `uv tool install` would exit 0 here without rebuilding the gutted
    #    environment, so the installer forces it.
    subprocess.run(["chmod", "-R", "u+w", str(tmp_path / "tools" / "bobi")],
                   check=True, timeout=300)
    _uv(env, "tool", "install", "--force", wheel)
    assert _bobi_runs(tmp_path, env), "installer path did not repair the install"

    # 3. The ergonomic path, for an operator whose CLI still works.
    _lock(tmp_path, env)
    released = subprocess.run([str(tmp_path / "bin" / "bobi"), "guard", "release"],
                              cwd=tmp_path, env=env, capture_output=True,
                              text=True, timeout=300)
    assert released.returncode == 0, released.stderr + released.stdout
    _uv(env, "tool", "install", "--force", wheel)
    assert _bobi_runs(tmp_path, env)


@pytest.mark.timeout(1800)
def test_release_window_survives_an_agent_launch_mid_upgrade(tmp_path, wheel):
    """The race a bare unlock loses on any running host.

    Monitor ticks land every 30s and re-apply the guard, well inside the time uv
    needs to resolve, build, and swap the tree.
    """
    env = _env(tmp_path)
    _uv(env, "tool", "install", wheel)
    _lock(tmp_path, env)

    released = subprocess.run([str(tmp_path / "bin" / "bobi"), "guard", "release"],
                              cwd=tmp_path, env=env, capture_output=True,
                              text=True, timeout=300)
    assert released.returncode == 0, released.stderr + released.stdout

    # The agent launch that used to re-lock the tree mid-upgrade.
    output = _agent_launch(tmp_path, env)
    assert "released ['bobi-dist-info', 'bobi-package']" in output, output

    _uv(env, "tool", "install", "--force", wheel)
    assert _bobi_runs(tmp_path, env)
