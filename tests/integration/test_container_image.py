"""Integration tests for the modastack instance image (containerized-8 / #338).

These verify the C8 image contract from CONTAINERIZED_INSTANCES.md §5/§6.1/§10:
non-root agent, no Node, native `claude` CLI on PATH, fastembed model baked in,
and the entrypoint's auth-mode guards. They build the image once per session.

The full acceptance criterion — a `docker run` reaching a healthy manager that
completes one `modastack ask` round-trip against the real API — needs live
credentials and is covered by ``test_image_ask_roundtrip`` (skipped unless
ANTHROPIC_API_KEY is present).

Gated on a working Docker daemon, so they no-op in environments without one.
Set MODASTACK_TEST_IMAGE=<tag> to reuse an already-built image and skip the build.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_ok(), reason="docker daemon not available"
)


def _run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), capture_output=True, text=True, **kw
    )


@pytest.fixture(scope="session")
def image() -> str:
    """Build (or reuse) the instance image; return its tag."""
    prebuilt = os.environ.get("MODASTACK_TEST_IMAGE")
    if prebuilt:
        return prebuilt

    tag = "modastack:pytest"
    proc = _run(
        "docker", "build", "-t", tag, str(REPO_ROOT),
        timeout=1800,
    )
    if proc.returncode != 0:
        pytest.fail(f"docker build failed:\n{proc.stdout}\n{proc.stderr}")
    return tag


@requires_docker
@pytest.mark.timeout(1900)
def test_claude_cli_present_and_native(image: str):
    """The native `claude` binary is on PATH and runnable (no Node needed)."""
    proc = _run("docker", "run", "--rm", "--entrypoint", "claude", image, "--version")
    assert proc.returncode == 0, proc.stderr
    assert "claude" in (proc.stdout + proc.stderr).lower()


@requires_docker
@pytest.mark.timeout(120)
def test_no_node_runtime(image: str):
    """No Node.js in the image — the claude CLI is native and the local event
    server (Node) is never run in deployed instances (C6)."""
    proc = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", image,
        "-c", "command -v node && echo HAS_NODE || echo NO_NODE",
    )
    assert "NO_NODE" in proc.stdout, proc.stdout + proc.stderr


@requires_docker
@pytest.mark.timeout(120)
def test_fastembed_model_baked(image: str):
    """The embedding model is pre-downloaded into the image at HF_HOME."""
    proc = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", image,
        "-c", "test -n \"$(ls -A /opt/modastack/models 2>/dev/null)\" && echo BAKED || echo EMPTY",
    )
    assert "BAKED" in proc.stdout, proc.stdout + proc.stderr


@requires_docker
@pytest.mark.timeout(120)
def test_api_key_mode_requires_key(image: str):
    """api_key mode with no ANTHROPIC_API_KEY fails fast, before touching the volume."""
    proc = _run(
        "docker", "run", "--rm", "-e", "MODASTACK_AUTH=api_key", image,
    )
    assert proc.returncode != 0
    assert "ANTHROPIC_API_KEY is unset" in (proc.stdout + proc.stderr)


@requires_docker
@pytest.mark.timeout(120)
def test_subscription_mode_rejects_api_key(image: str):
    """subscription mode must refuse to start if ANTHROPIC_API_KEY is set —
    it silently outranks subscription OAuth creds and bills the API (§6.1)."""
    proc = _run(
        "docker", "run", "--rm",
        "-e", "MODASTACK_AUTH=subscription",
        "-e", "ANTHROPIC_API_KEY=sk-ant-should-not-be-here",
        image,
    )
    assert proc.returncode != 0
    assert "overrides subscription auth" in (proc.stdout + proc.stderr)


@requires_docker
@pytest.mark.timeout(120)
def test_unknown_auth_mode_rejected(image: str):
    proc = _run(
        "docker", "run", "--rm", "-e", "MODASTACK_AUTH=bogus", image,
    )
    assert proc.returncode != 0
    assert "unknown MODASTACK_AUTH" in (proc.stdout + proc.stderr)


@requires_docker
@pytest.mark.timeout(120)
def test_empty_volume_without_team_fails_clearly(image: str, tmp_path: Path):
    """An empty volume with no MODASTACK_TEAM should fail with a clear message,
    not a confusing crash deep in the manager."""
    vol = tmp_path / "data"
    vol.mkdir()
    proc = _run(
        "docker", "run", "--rm",
        "-e", "MODASTACK_AUTH=api_key",
        "-e", "ANTHROPIC_API_KEY=sk-ant-test",
        "-v", f"{vol}:/data",
        image,
    )
    assert proc.returncode != 0
    assert "MODASTACK_TEAM is unset" in (proc.stdout + proc.stderr)


@requires_docker
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs a real ANTHROPIC_API_KEY for the live ask round-trip",
)
@pytest.mark.timeout(600)
def test_image_ask_roundtrip(image: str, tmp_path: Path):
    """C8 acceptance: docker run with an empty volume + env reaches a healthy
    manager that completes one `modastack ask` round-trip against the real API."""
    vol = tmp_path / "data"
    vol.mkdir()
    name = "modastack-c8-acceptance"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "-e", "MODASTACK_AUTH=api_key",
            "-e", f"ANTHROPIC_API_KEY={os.environ['ANTHROPIC_API_KEY']}",
            "-e", "MODASTACK_TEAM=eng-team",
            "-v", f"{vol}:/data",
            image,
        )
        assert up.returncode == 0, up.stderr

        # Wait for the container to report healthy (HEALTHCHECK probes /health).
        import json
        import time
        deadline = time.time() + 300
        status = ""
        while time.time() < deadline:
            insp = _run(
                "docker", "inspect", "-f", "{{json .State.Health.Status}}", name
            )
            status = insp.stdout.strip().strip('"')
            if status == "healthy":
                break
            if status == "unhealthy":
                logs = _run("docker", "logs", name)
                pytest.fail(f"container unhealthy:\n{logs.stdout}\n{logs.stderr}")
            time.sleep(5)
        assert status == "healthy", f"never became healthy (last={status!r})"

        ask = _run(
            "docker", "exec", name,
            "modastack", "ask", "Reply with the single word: pong",
            timeout=180,
        )
        assert ask.returncode == 0, ask.stderr
        assert "pong" in ask.stdout.lower()
    finally:
        _run("docker", "logs", name)
        _run("docker", "rm", "-f", name)
