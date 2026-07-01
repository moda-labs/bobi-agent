"""Integration tests for the bobi instance image (containerized-8 / #338).

These verify the image contract from docs/CONTAINERIZED_DEPLOYMENT.md §2 (The image):
non-root agent, no Node, native `claude` CLI on PATH, fastembed model baked in,
and the entrypoint's auth-mode guards. They build the image once per session.

The full acceptance criterion — a `docker run` reaching a healthy manager that
completes one named ask round-trip against the real API — needs live
credentials and is covered by ``test_image_ask_roundtrip`` (skipped unless
ANTHROPIC_API_KEY is present).

Gated on a working Docker daemon, so they no-op in environments without one.
Set BOBI_TEST_IMAGE=<tag> to reuse an already-built image and skip the build.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Whole module builds/runs the image — excluded from integration-fast via
# `-m "not docker"` so it never triggers a multi-minute build on every PR.
pytestmark = pytest.mark.docker


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
    prebuilt = os.environ.get("BOBI_TEST_IMAGE")
    if prebuilt:
        return prebuilt

    tag = "bobi:pytest"
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
@pytest.mark.timeout(1900)
def test_codex_cli_present_and_native(image: str):
    """The native `codex` binary is on PATH and runnable - the second first-class
    brain baked alongside `claude`, so a `brain: codex` team (or the per-task
    brain switch) runs on the generic image at parity with Claude (#428). Taken
    from the GitHub-release musl binary, NOT npm, so it needs no Node (asserted by
    test_no_node_runtime)."""
    proc = _run("docker", "run", "--rm", "--entrypoint", "codex", image, "--version")
    assert proc.returncode == 0, proc.stderr
    assert "codex" in (proc.stdout + proc.stderr).lower()


@requires_docker
@pytest.mark.timeout(120)
def test_no_node_runtime(image: str):
    """No Node.js in the image — both the claude and codex CLIs are native
    binaries and the local event server (Node) is never run in deployed
    instances (C6). Codex ships via npm upstream but we bake the standalone
    musl binary precisely to keep this invariant."""
    proc = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", image,
        "-c", "command -v node && echo HAS_NODE || echo NO_NODE",
    )
    assert "NO_NODE" in proc.stdout, proc.stdout + proc.stderr


@requires_docker
@pytest.mark.timeout(120)
def test_fastembed_model_baked(image: str):
    """The embedding model is pre-downloaded into fastembed's runtime cache."""
    code = """
import os
from pathlib import Path
from fastembed import TextEmbedding

cache = Path(os.environ["FASTEMBED_CACHE_PATH"])
models_root = Path("/opt/bobi/models")
assert cache == models_root / "fastembed"
baked_model_manifest = {
    (path.relative_to(models_root).as_posix(), path.is_dir(), path.stat().st_size)
    for path in models_root.rglob("*")
}
assert any(
    path == "fastembed" or path.startswith("fastembed/")
    for path, _, _ in baked_model_manifest
)

model = TextEmbedding(
    model_name="sentence-transformers/all-MiniLM-L6-v2",
    cache_dir=str(cache),
)
list(model.embed(["cache smoke"]))
assert {
    (path.relative_to(models_root).as_posix(), path.is_dir(), path.stat().st_size)
    for path in models_root.rglob("*")
} == baked_model_manifest
print("BAKED")
"""
    proc = _run(
        "docker", "run", "--rm", "--network", "none",
        "--entrypoint", "/opt/venv/bin/python", image,
        "-c", code,
    )
    assert "BAKED" in proc.stdout, proc.stdout + proc.stderr


@requires_docker
@pytest.mark.timeout(120)
def test_api_key_mode_requires_key(image: str):
    """api_key mode with no ANTHROPIC_API_KEY fails fast, before touching the volume."""
    proc = _run(
        "docker", "run", "--rm",
        "-e", "BOBI_AUTH=api_key",
        "-e", "BOBI_AGENT=pytest",
        image,
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
        "-e", "BOBI_AUTH=subscription",
        "-e", "BOBI_AGENT=pytest",
        "-e", "ANTHROPIC_API_KEY=sk-ant-should-not-be-here",
        image,
    )
    assert proc.returncode != 0
    assert "overrides subscription auth" in (proc.stdout + proc.stderr)


@requires_docker
@pytest.mark.timeout(120)
def test_unknown_auth_mode_rejected(image: str):
    proc = _run(
        "docker", "run", "--rm",
        "-e", "BOBI_AUTH=bogus",
        "-e", "BOBI_AGENT=pytest",
        image,
    )
    assert proc.returncode != 0
    assert "unknown BOBI_AUTH" in (proc.stdout + proc.stderr)


@requires_docker
@pytest.mark.timeout(120)
def test_config_dir_survives_privilege_drop(image: str):
    """Regression: the agent's HOME stays on the IMAGE (/home/bobi) so baked
    tools are read in place; only Claude's DURABLE state is redirected to the
    volume via CLAUDE_CONFIG_DIR. The entrypoint carries both through the gosu
    privilege drop with `env HOME=... CLAUDE_CONFIG_DIR=...` — verify that
    mechanism yields the image HOME and the volume config dir in-image."""
    proc = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
        'gosu bobi env HOME=/home/bobi CLAUDE_CONFIG_DIR=/data/claude '
        'sh -c \'printf "%s:%s" "$HOME" "$CLAUDE_CONFIG_DIR"\'',
    )
    assert proc.stdout.strip() == "/home/bobi:/data/claude", proc.stdout + proc.stderr


@requires_docker
@pytest.mark.timeout(120)
def test_empty_volume_without_team_waits_for_push(image: str, tmp_path: Path):
    """An empty volume with neither BOBI_TEAM nor BOBI_TEAM_URL enters
    the wait-for-team state (ssh-push delivery) — it does NOT crash; it logs that
    it's waiting and stays alive until a team is pushed onto the volume."""
    import time

    vol = tmp_path / "data"
    vol.mkdir()
    name = "bobi-waitforteam"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "-e", "BOBI_AUTH=api_key",
            "-e", "BOBI_AGENT=pytest",
            "-e", "ANTHROPIC_API_KEY=sk-ant-test",
            "-v", f"{vol}:/data",
            image,
        )
        assert up.returncode == 0, up.stderr

        # It should log that it's waiting for a pushed team, and keep running.
        deadline = time.time() + 30
        logs = ""
        while time.time() < deadline:
            out = _run("docker", "logs", name)
            logs = out.stdout + out.stderr
            if "waiting for" in logs.lower():
                break
            time.sleep(1)
        assert "waiting for" in logs.lower(), f"never entered wait state:\n{logs}"
        # Still alive (didn't crash/exit on the missing team).
        running = _run(
            "docker", "inspect", "-f", "{{.State.Running}}", name
        ).stdout.strip()
        assert running == "true", f"container exited instead of waiting:\n{logs}"
        # And it must NOT have used the old fatal path.
        assert "nothing to install" not in logs
    finally:
        _run("docker", "rm", "-f", name)


@requires_docker
@pytest.mark.timeout(120)
def test_unresolvable_team_fails_loudly(image: str, tmp_path: Path):
    """An unresolvable BOBI_TEAM (no team registry in the image) must fail
    with an ACTIONABLE error pointing at BOBI_TEAM_URL, not crash-loop on a
    bare `set -e` pipefail trace (C9/#339)."""
    import time

    vol = tmp_path / "data"
    vol.mkdir()
    name = "bobi-badteam"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "-e", "BOBI_AUTH=api_key",
            "-e", "BOBI_AGENT=pytest",
            "-e", "ANTHROPIC_API_KEY=sk-ant-test",
            "-e", "BOBI_TEAM=does-not-exist-anywhere",
            "-v", f"{vol}:/data",
            image,
        )
        assert up.returncode == 0, up.stderr

        # The container must STOP (clean exit 1), not sit hung or loop silently.
        deadline = time.time() + 60
        running = "true"
        while time.time() < deadline:
            running = _run(
                "docker", "inspect", "-f", "{{.State.Running}}", name
            ).stdout.strip()
            if running == "false":
                break
            time.sleep(1)
        logs = _run("docker", "logs", name)
        text = logs.stdout + logs.stderr
        assert running == "false", f"container didn't exit:\n{text}"
        # Actionable guidance, not a raw pipefail trace.
        assert "couldn't install team 'does-not-exist-anywhere'" in text, text
        assert "BOBI_TEAM_URL" in text, text
    finally:
        _run("docker", "rm", "-f", name)


SMOKE_TEAM = REPO_ROOT / "tests" / "fixtures" / "claude-smoke"


@requires_docker
@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live round-trip needs a real ANTHROPIC_API_KEY for the Claude call",
)
@pytest.mark.timeout(600)
def test_image_ask_roundtrip(image: str, tmp_path: Path):
    """C8 acceptance, live: an empty volume + api_key auth + a reachable event
    server reaches a healthy manager that completes one named ask
    round-trip against the real API.

    Spins up an EPHEMERAL event server (the real Worker code via `wrangler dev`,
    bound to 0.0.0.0) so CI never touches a production deployment — the
    container reaches it via host.docker.internal and mints its own bubble.
    Installs the dependency-free claude-smoke fixture so preflight needs no
    service secrets.
    """
    import time

    import sys

    # `--network host` only shares the real host netns on Linux; on Docker
    # Desktop (mac/Windows) the container's 127.0.0.1 is the VM's, not the host's,
    # so it can't reach a host-side wrangler. CI runs on Linux.
    if sys.platform != "linux":
        pytest.skip("live round-trip needs Linux (--network host reaches the host)")

    # Reuse the wrangler-dev harness from the event-server tests; needs node
    # deps (the helper npm-ci's them on demand). Skip cleanly if unavailable.
    from .test_event_server import _has_wrangler, _start_wrangler_server

    if not _has_wrangler():
        pytest.skip("wrangler not installed (run `npm ci` in event-server/)")

    base_url, port, stop_wrangler = _start_wrangler_server()
    # Share the host network so the container reaches wrangler over loopback.
    # The bubble layer refuses to mint a key over a cleartext *remote* URL
    # (host.docker.internal counts as remote); 127.0.0.1 is loopback, so it's
    # allowed. --network host is Linux-native (CI runs on ubuntu).
    container_es_url = f"http://127.0.0.1:{port}"

    vol = tmp_path / "data"
    vol.mkdir()
    name = "bobi-c8-acceptance"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "--network", "host",
            "-e", "BOBI_AUTH=api_key",
            "-e", "BOBI_AGENT=claude-smoke",
            "-e", f"ANTHROPIC_API_KEY={os.environ['ANTHROPIC_API_KEY']}",
            "-e", f"BOBI_EVENT_SERVER={container_es_url}",
            "-e", "BOBI_TEAM=/mnt/team",
            "-v", f"{vol}:/data",
            "-v", f"{SMOKE_TEAM}:/mnt/team:ro",
            image,
        )
        assert up.returncode == 0, up.stderr

        # Wait for the container to report healthy (HEALTHCHECK probes /health).
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

        # Run as the bobi user from the selected runtime root.
        ask = _run(
            "docker", "exec", "-u", "bobi",
            "-w", "/data/.bobi/agents/claude-smoke/run", name,
            "bobi", "agent", "claude-smoke", "ask", "Reply with the single word: pong",
            timeout=180,
        )
        if ask.returncode != 0 or "pong" not in ask.stdout.lower():
            logs = _run("docker", "logs", name)
            pytest.fail(
                f"ask failed (rc={ask.returncode})\n"
                f"STDOUT: {ask.stdout}\nSTDERR: {ask.stderr}\n"
                f"--- container logs ---\n{logs.stdout}\n{logs.stderr}"
            )
    finally:
        _run("docker", "rm", "-f", name)
        stop_wrangler()


# The Codex analog of SMOKE_TEAM (both dependency-free, github-only). Codex now
# ships in the base image, so this needs no flavored image - it boots on the same
# base `image` as the Claude round-trip, just with brain: codex.
CODEX_SMOKE_TEAM = REPO_ROOT / "tests" / "fixtures" / "codex-smoke"


@requires_docker
@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="live codex round-trip needs a real OPENAI_API_KEY for the Codex call",
)
@pytest.mark.timeout(600)
def test_image_codex_ask_roundtrip(image: str, tmp_path: Path):
    """Live: bobi works e2e with Codex as the brain - the symmetric analog of the
    Claude `test_image_ask_roundtrip` (#428).

    Boots the base image (which now bakes the Codex CLI) with BOBI_AUTH=api_key +
    OPENAI_API_KEY (the entrypoint materializes ~/.codex/auth.json from the key),
    reaches a healthy manager, and completes one named ask round-trip against the
    real OpenAI API. Same ephemeral wrangler-dev event server as the Claude
    round-trip; the dependency-free codex-smoke team needs no secrets.
    """
    import sys
    import time

    if sys.platform != "linux":
        pytest.skip("live round-trip needs Linux (--network host reaches the host)")

    from .test_event_server import _has_wrangler, _start_wrangler_server

    if not _has_wrangler():
        pytest.skip("wrangler not installed (run `npm ci` in event-server/)")

    base_url, port, stop_wrangler = _start_wrangler_server()
    container_es_url = f"http://127.0.0.1:{port}"

    vol = tmp_path / "data"
    vol.mkdir()
    name = "bobi-codex-acceptance"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "--network", "host",
            "-e", "BOBI_AUTH=api_key",
            "-e", "BOBI_AGENT=codex-smoke",
            "-e", f"OPENAI_API_KEY={os.environ['OPENAI_API_KEY']}",
            "-e", f"BOBI_EVENT_SERVER={container_es_url}",
            "-e", "BOBI_TEAM=/mnt/team",
            "-v", f"{vol}:/data",
            "-v", f"{CODEX_SMOKE_TEAM}:/mnt/team:ro",
            image,
        )
        assert up.returncode == 0, up.stderr

        # Wait for the container to report healthy (HEALTHCHECK probes /health).
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

        # Run as the bobi user from the codex-smoke runtime root.
        ask = _run(
            "docker", "exec", "-u", "bobi",
            "-w", "/data/.bobi/agents/codex-smoke/run", name,
            "bobi", "agent", "codex-smoke", "ask",
            "Reply with the single word: pong",
            timeout=180,
        )
        if ask.returncode != 0 or "pong" not in ask.stdout.lower():
            logs = _run("docker", "logs", name)
            pytest.fail(
                f"codex ask failed (rc={ask.returncode})\n"
                f"STDOUT: {ask.stdout}\nSTDERR: {ask.stderr}\n"
                f"--- container logs ---\n{logs.stdout}\n{logs.stderr}"
            )
    finally:
        _run("docker", "rm", "-f", name)
        stop_wrangler()
