"""Integration tests for the bobi instance image (containerized-8 / #338).

These verify the image contract from docs/CONTAINERIZED_DEPLOYMENT.md §2 (The image):
non-root agent, no Node, native `claude` CLI on PATH, fastembed's runtime cache,
and the entrypoint's auth-mode guards. They build the image once per session.

The full acceptance criterion — a `docker run` reaching a healthy manager that
completes one named ask round-trip against the real API — needs live
credentials and is covered by ``test_image_ask_roundtrip`` (skipped unless
ANTHROPIC_API_KEY is present).

Gated on a working Docker daemon, so they no-op in environments without one.
Set BOBI_TEST_IMAGE=<tag> to reuse an already-built image and skip the build.

Building the image here needs Node.js major 20 EXACTLY on PATH, because
staging the wheel runs hatch_build.py's embedded-event-server build. Node 22
and 24 are both rejected outright. Reusing a prebuilt image via
BOBI_TEST_IMAGE needs no Node at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
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


def _stage_wheel() -> None:
    """Build the wheel into `dist/`, the way the release pipeline stages it.

    Wheel mode installs `dist/*.whl` and the Dockerfile expects EXACTLY one, so
    clear the directory first: a stale wheel from an earlier version would make
    `pip install /dist/*.whl` install two, and which one wins is arbitrary.
    """
    dist = REPO_ROOT / "dist"
    for stale in dist.glob("*.whl"):
        stale.unlink()
    proc = _run(
        sys.executable, "-m", "build", "--wheel", "--outdir", str(dist),
        cwd=str(REPO_ROOT), timeout=900,
    )
    if proc.returncode != 0:
        pytest.fail(
            "could not build the wheel to stage into the image build context.\n"
            "This build runs hatch_build.py, which needs Node.js major 20 "
            "EXACTLY on PATH (not 22, not 24) to build the embedded event "
            f"server.\n\nstdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
        )


@pytest.fixture(scope="session")
def image() -> str:
    """Build (or reuse) the instance image; return its tag.

    Builds in WHEEL mode - the mode the release pipeline and this repo's own
    CI use - not the Dockerfile's nominal `source` default.

    Source mode cannot build in ANY repo: `.dockerignore` drops both `.git`
    and `event-server/dist`, so hatch_build.py's `initialize` finds neither a
    VCS checkout nor a prebuilt artifact, takes the rebuild path, and needs
    Node inside `python:3.11-slim` - where there is none, by design (the image
    is deliberately Node-free). Defaulting to it made this entire module look
    environment-blocked when it was really asking for an impossible build.
    """
    prebuilt = os.environ.get("BOBI_TEST_IMAGE")
    if prebuilt:
        return prebuilt

    _stage_wheel()
    tag = "bobi:pytest"
    proc = _run(
        "docker", "build", "--build-arg", "BOBI_BUILD=wheel",
        "-t", tag, str(REPO_ROOT),
        timeout=1800,
    )
    if proc.returncode != 0:
        pytest.fail(f"docker build failed:\n{proc.stdout}\n{proc.stderr}")
    return tag


TEAM_DEPS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "team-deps-bake"
TEAM_DEPS_SECRET_DIGEST = "/opt/bobi/mod361-secret.sha256"


def _ensure_wheel_staged() -> None:
    """The flavored build uses the same wheel-mode context as the base image."""
    if len(list((REPO_ROOT / "dist").glob("*.whl"))) != 1:
        _stage_wheel()


def _build_team_deps_image(
    team_dir: Path,
    *,
    tag: str,
    secret: str,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Render a fixture hook into the real build context and build the image."""
    from bobi.build_render import load_team_config, render_team_deps_script

    _ensure_wheel_staged()
    hook_dir = REPO_ROOT / "dist" / "team-deps"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook = hook_dir / f"mod361-{uuid.uuid4().hex}.sh"
    script = render_team_deps_script(load_team_config(team_dir))
    # BuildKit secrets do not invalidate cache, so vary the hook itself to prove
    # this build consumed this invocation's secret rather than a cached layer.
    hook.write_text(script + f"# test cache nonce: {uuid.uuid4().hex}\n")

    env = os.environ.copy()
    env["DOCKER_BUILDKIT"] = "1"
    env["MODA_SKILLS_TOKEN"] = secret
    proc = _run(
        "docker", "build",
        "--build-arg", "BOBI_BUILD=wheel",
        "--build-arg", f"TEAM_DEPS={hook.relative_to(REPO_ROOT)}",
        "--secret", "id=MODA_SKILLS_TOKEN,env=MODA_SKILLS_TOKEN",
        "-t", tag, str(REPO_ROOT),
        env=env,
        timeout=1800,
    )
    return proc, hook


@pytest.fixture(scope="module")
def team_deps_image(image: str) -> tuple[str, str]:
    """Build the published recipe with a rendered hook and a dummy secret."""
    del image  # Force the base build/cache before changing only TEAM_DEPS.
    tag = f"bobi-team-deps-mod361:{uuid.uuid4().hex[:12]}"
    secret = f"mod361-buildkit-secret-{uuid.uuid4().hex}"
    proc, hook = _build_team_deps_image(
        TEAM_DEPS_FIXTURE,
        tag=tag,
        secret=secret,
    )
    try:
        if proc.returncode != 0:
            pytest.fail(
                "TEAM_DEPS image build failed:\n"
                f"{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"
            )
        yield tag, secret
    finally:
        hook.unlink(missing_ok=True)
        _run("docker", "image", "rm", "-f", tag)


@requires_docker
@pytest.mark.timeout(1900)
def test_team_deps_hook_receives_secret_without_leaking(
    team_deps_image: tuple[str, str],
):
    """The published hook sees the secret, while image metadata never does."""
    tag, secret = team_deps_image
    expected_digest = hashlib.sha256(secret.encode()).hexdigest()

    observed = _run(
        "docker", "run", "--rm", "--entrypoint", "cat",
        tag, TEAM_DEPS_SECRET_DIGEST,
        timeout=120,
    )
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout.strip() == expected_digest

    history = _run(
        "docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}",
        tag, timeout=60,
    )
    inspect = _run("docker", "image", "inspect", tag, timeout=60)
    assert history.returncode == 0, history.stderr
    assert inspect.returncode == 0, inspect.stderr
    assert secret not in history.stdout
    assert secret not in inspect.stdout

    leftover = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", tag, "-c",
        "find /run/secrets -mindepth 1 -maxdepth 1 -print 2>/dev/null || true",
        timeout=120,
    )
    assert leftover.stdout.strip() == "", leftover.stdout


@requires_docker
@pytest.mark.timeout(1900)
def test_team_deps_requires_gate_rejects_missing_tool(
    image: str,
    tmp_path: Path,
):
    """A failed fixture requires check stops the image build in CI."""
    del image  # Reuse the warmed base-image layers under the failing hook.
    team = tmp_path / "team-deps-missing-tool"
    shutil.copytree(TEAM_DEPS_FIXTURE, team)
    agent_yaml = team / "agent.yaml"
    broken = agent_yaml.read_text().replace(
        "name: hook-tool\n    check: 'command -v mod361-hook-tool >/dev/null'",
        "name: missing-tool\n    check: 'command -v mod361-missing-tool >/dev/null'",
    )
    assert broken != agent_yaml.read_text()
    agent_yaml.write_text(broken)

    tag = f"bobi-team-deps-mod361-fail:{uuid.uuid4().hex[:12]}"
    proc, hook = _build_team_deps_image(
        team,
        tag=tag,
        secret=f"mod361-buildkit-secret-{uuid.uuid4().hex}",
    )
    try:
        text = proc.stdout + proc.stderr
        assert proc.returncode != 0, "a missing requires tool did not fail the build"
        assert "verify missing-tool" in text, text[-4000:]
    finally:
        hook.unlink(missing_ok=True)
        _run("docker", "image", "rm", "-f", tag)


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
def test_home_has_no_root_owned_entries(image: str):
    """Everything under /home/bobi is owned by bobi. The Dockerfile exports
    HOME=/home/bobi globally, so a root-run step that writes to $HOME (the
    codex --version smoke did this: codex drops PATH-alias helpers under
    ~/.codex on any invocation) bakes a root-owned dir into the agent HOME.
    Team-deps `run:` steps execute as bobi and then fail on it — the gstack
    `--host auto` bake died on root-owned /home/bobi/.codex exactly this way
    at the 0.47.0 fleet roll."""
    proc = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", image,
        "-c", "find /home/bobi ! -user bobi -print",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        f"root-owned entries baked into /home/bobi:\n{proc.stdout}"
    )


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
def test_fastembed_cache_downloads_on_first_use(image: str):
    """The image should not bake the model; first KB use downloads to /data."""
    proc = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", image,
        "-c",
        "test \"$FASTEMBED_CACHE_PATH\" = /data/.bobi/cache/fastembed "
        "&& test ! -e /opt/bobi/models "
        "&& echo FIRST_USE_CACHE",
    )
    assert "FIRST_USE_CACHE" in proc.stdout, proc.stdout + proc.stderr


@requires_docker
@pytest.mark.timeout(120)
def test_fastembed_cache_writable_on_existing_volume(image: str, tmp_path: Path):
    """Existing stamped volumes still get the new runtime cache chowned."""
    data = tmp_path / "data"
    data.mkdir()
    (data / ".bobi-owned").write_text("")

    proc = _run(
        "docker", "run", "--rm",
        "-v", f"{data}:/data",
        "-e", "BOBI_AUTH=api_key",
        "-e", "BOBI_AGENT=pytest",
        "-e", "ANTHROPIC_API_KEY=dummy",
        "--entrypoint", "sh", image,
        "-c",
        "timeout 3 /usr/local/bin/docker-entrypoint.sh >/tmp/entrypoint.log 2>&1 || true; "
        "gosu bobi sh -c 'test -w \"$FASTEMBED_CACHE_PATH\" && test -w \"$HF_HOME\"' "
        "&& echo CACHE_WRITABLE",
    )
    assert "CACHE_WRITABLE" in proc.stdout, proc.stdout + proc.stderr


@requires_docker
@pytest.mark.timeout(120)
def test_api_key_mode_requires_provider_key(image: str):
    """Native Claude api_key mode fails without ANTHROPIC_API_KEY."""
    team = REPO_ROOT / "tests" / "fixtures" / "claude-smoke"
    proc = _run(
        "docker", "run", "--rm",
        "-e", "BOBI_AUTH=api_key",
        "-e", "BOBI_AGENT=pytest",
        "-e", "BOBI_EVENT_SERVER=http://127.0.0.1:9",
        "-e", "BOBI_TEAM=/mnt/team",
        "-v", f"{team}:/mnt/team:ro",
        image,
    )
    assert proc.returncode != 0
    text = proc.stdout + proc.stderr
    assert "ANTHROPIC_API_KEY is unset" in text
    assert "ANTHROPIC_AUTH_TOKEN" not in text


@requires_docker
@pytest.mark.timeout(120)
def test_gateway_auth_token_passes_post_install_auth_gate(
    image: str,
    tmp_path: Path,
):
    """A first-boot gateway team can authenticate with its bearer token alone.

    With no explicit BOBI_BRAIN, the entrypoint must install the team before it
    can resolve ``brain.kind`` and validate the provider credential. Reaching
    the supervisor proves that post-install gate accepted ANTHROPIC_AUTH_TOKEN;
    no request to the deliberately unreachable gateway is needed.
    """
    import time

    team = tmp_path / "team"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "claude-smoke", team)
    agent_yaml = team / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text()
        + "\nbrain:\n"
        + "  kind: claude\n"
        + "  base_url: http://127.0.0.1:9\n"
    )

    vol = tmp_path / "data"
    vol.mkdir()
    name = "bobi-gateway-auth"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "-e", "BOBI_AUTH=api_key",
            "-e", "BOBI_AGENT=claude-smoke",
            "-e", "ANTHROPIC_AUTH_TOKEN=gateway-test-token",
            "-e", "BOBI_EVENT_SERVER=http://127.0.0.1:9",
            "-e", "BOBI_TEAM=/mnt/team",
            "-v", f"{vol}:/data",
            "-v", f"{team}:/mnt/team:ro",
            image,
        )
        assert up.returncode == 0, up.stderr

        deadline = time.time() + 30
        text = ""
        while time.time() < deadline:
            logs = _run("docker", "logs", name)
            text = logs.stdout + logs.stderr
            if "Starting manager under the supervisor sidecar" in text:
                break
            if "FATAL:" in text:
                break
            time.sleep(1)

        assert "Starting manager under the supervisor sidecar" in text, text
        assert "ANTHROPIC_API_KEY is unset" not in text
    finally:
        _run("docker", "rm", "-f", name)


@requires_docker
@pytest.mark.timeout(120)
def test_subscription_claude_gateway_passes_post_install_auth_gate(
    image: str,
    tmp_path: Path,
):
    """A Claude gateway may use durable subscription OAuth credentials."""
    import time

    team = tmp_path / "team"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "claude-smoke", team)
    agent_yaml = team / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text()
        + "\nbrain:\n"
        + "  kind: claude\n"
        + "  base_url: http://127.0.0.1:9\n"
    )

    vol = tmp_path / "data"
    creds = vol / "claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({
        "claudeAiOauth": {"refreshToken": "refresh"},
    }))

    name = "bobi-gateway-subscription"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "-e", "BOBI_AUTH=subscription",
            "-e", "BOBI_AGENT=claude-smoke",
            "-e", "BOBI_EVENT_SERVER=http://127.0.0.1:9",
            "-e", "BOBI_TEAM=/mnt/team",
            "-v", f"{vol}:/data",
            "-v", f"{team}:/mnt/team:ro",
            image,
        )
        assert up.returncode == 0, up.stderr

        deadline = time.time() + 30
        text = ""
        while time.time() < deadline:
            logs = _run("docker", "logs", name)
            text = logs.stdout + logs.stderr
            if "Starting manager under the supervisor sidecar" in text:
                break
            if "FATAL:" in text:
                break
            time.sleep(1)

        assert "refresh token is present" in text, text
        assert "Starting manager under the supervisor sidecar" in text, text
    finally:
        _run("docker", "rm", "-f", name)


@requires_docker
@pytest.mark.timeout(120)
def test_credentialless_claude_gateway_passes_post_install_auth_gate(
    image: str,
    tmp_path: Path,
):
    """A gateway that serves unauthenticated needs no provider credential."""
    import time

    team = tmp_path / "team"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "claude-smoke", team)
    agent_yaml = team / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text()
        + "\nbrain:\n"
        + "  kind: claude\n"
        + "  base_url: http://127.0.0.1:9\n"
    )

    vol = tmp_path / "data"
    vol.mkdir()
    name = "bobi-gateway-no-auth"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "-e", "BOBI_AUTH=api_key",
            "-e", "BOBI_AGENT=claude-smoke",
            "-e", "BOBI_EVENT_SERVER=http://127.0.0.1:9",
            "-e", "BOBI_TEAM=/mnt/team",
            "-v", f"{vol}:/data",
            "-v", f"{team}:/mnt/team:ro",
            image,
        )
        assert up.returncode == 0, up.stderr

        deadline = time.time() + 30
        text = ""
        while time.time() < deadline:
            logs = _run("docker", "logs", name)
            text = logs.stdout + logs.stderr
            if "Starting manager under the supervisor sidecar" in text:
                break
            if "FATAL:" in text:
                break
            time.sleep(1)

        assert "Starting manager under the supervisor sidecar" in text, text
        assert "ANTHROPIC_API_KEY" not in text
    finally:
        _run("docker", "rm", "-f", name)


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


def _subscription_bootstrap_logs(
    image: str,
    tmp_path: Path,
    brain: str,
    credential: dict,
) -> str:
    """Boot through the real entrypoint and return its auth-decision logs."""
    import time

    team = REPO_ROOT / "tests" / "fixtures" / f"{brain}-smoke"
    data = tmp_path / f"data-{brain}"
    cred_dir = data / brain
    cred_dir.mkdir(parents=True)
    cred_file = ".credentials.json" if brain == "claude" else "auth.json"
    (cred_dir / cred_file).write_text(json.dumps(credential))

    name = f"bobi-subscription-credentials-{brain}"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "-e", "BOBI_AUTH=subscription",
            "-e", f"BOBI_AGENT={brain}-smoke",
            "-e", "BOBI_EVENT_SERVER=http://127.0.0.1:9",
            "-e", "BOBI_TEAM=/mnt/team",
            "-v", f"{data}:/data",
            "-v", f"{team}:/mnt/team:ro",
            image,
        )
        assert up.returncode == 0, up.stderr

        deadline = time.time() + 45
        text = ""
        while time.time() < deadline:
            logs = _run("docker", "logs", name)
            text = logs.stdout + logs.stderr
            if ("running login bootstrap" in text
                    or "skipping login bootstrap" in text
                    or "FATAL:" in text):
                break
            time.sleep(0.5)
        return text
    finally:
        _run("docker", "rm", "-f", name)


@requires_docker
@pytest.mark.timeout(1900)
@pytest.mark.parametrize(
    ("brain", "credential", "reason"),
    [
        (
            "claude",
            {"claudeAiOauth": {"accessToken": "", "refreshToken": ""}},
            "refresh token is missing or blank",
        ),
        (
            "codex",
            {"OPENAI_API_KEY": None, "tokens": {"refresh_token": ""}},
            "refresh token is missing or blank",
        ),
    ],
)
def test_subscription_bootstrap_runs_for_invalid_credentials(
    image: str,
    tmp_path: Path,
    brain: str,
    credential: dict,
    reason: str,
):
    text = _subscription_bootstrap_logs(
        image, tmp_path, brain, credential,
    )

    assert f"credentials invalid: {reason}" in text, text
    assert "running login bootstrap" in text, text


@requires_docker
@pytest.mark.timeout(1900)
@pytest.mark.parametrize(
    ("brain", "credential", "reason"),
    [
        (
            "claude",
            {
                "claudeAiOauth": {
                    "accessToken": "access",
                    "refreshToken": "refresh",
                    "refreshTokenExpiresAt": 4_102_444_800_000,
                },
            },
            "refresh token is present and unexpired",
        ),
        (
            "codex",
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "id",
                    "access_token": "access",
                    "refresh_token": "refresh",
                },
            },
            "refresh token is present",
        ),
    ],
)
def test_subscription_bootstrap_skips_valid_credentials(
    image: str,
    tmp_path: Path,
    brain: str,
    credential: dict,
    reason: str,
):
    text = _subscription_bootstrap_logs(
        image, tmp_path, brain, credential,
    )

    assert f"credentials valid: {reason}" in text, text
    assert "skipping login bootstrap" in text, text


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
    # test_event_server stayed in the public repo at the repo split, so the
    # live round-trip skips here until a private-side harness exists.
    try:
        from .test_event_server import _has_wrangler, _start_wrangler_server
    except ImportError:
        pytest.skip("wrangler-dev harness (test_event_server) not in this repo")

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

    # See the note in test_image_ask_roundtrip: the harness stayed public.
    try:
        from .test_event_server import _has_wrangler, _start_wrangler_server
    except ImportError:
        pytest.skip("wrangler-dev harness (test_event_server) not in this repo")

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


# --- installed-team identity guards ------------------------------------------
# These verify that the entrypoint refuses to boot when the identity stamped on
# the VOLUME disagrees with the team actually installed on it. They arrived here
# in the repo reorg (Lane 3): the coverage existed only in the private deploy
# repo, where it was asserted by grepping a COPY of docker-entrypoint.sh — and
# after Phase 6 published the real file, that copy went stale while its tests
# kept passing (two of them still demanded the pre-0.51.0 `bobi-supervisor`
# shim). Re-homed as behaviour against the real image, next to the file, so the
# two can no longer drift apart silently.


def _boot_failure(image: str, team_dir: Path, **env: str) -> str:
    """Run the image to its first fatal and return the combined output.

    The container is expected to REFUSE, so `--rm` and a synchronous run are
    enough — no log polling. The event server is deliberately unreachable: every
    guard here must fire before anything on the network is needed.
    """
    args = [
        "docker", "run", "--rm",
        "-e", "BOBI_AGENT=pytest",
        "-e", "BOBI_EVENT_SERVER=http://127.0.0.1:9",
        "-e", "BOBI_TEAM=/mnt/team",
    ]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += ["-v", f"{team_dir}:/mnt/team:ro", image]
    proc = _run(*args, timeout=180)
    assert proc.returncode != 0, (
        f"expected the entrypoint to refuse, but it exited 0:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    return proc.stdout + proc.stderr


def _gateway_team(tmp_path: Path, *, kind: str = "claude",
                  base_url: str = "https://gateway-b.test") -> Path:
    team = tmp_path / "team"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "claude-smoke", team)
    agent_yaml = team / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text()
        + f"\nbrain:\n  kind: {kind}\n  base_url: {base_url}\n"
    )
    return team


@requires_docker
@pytest.mark.timeout(240)
def test_changed_gateway_endpoint_is_rejected(image: str, tmp_path: Path):
    """A volume stamped for gateway A must not silently boot against gateway B.

    The stamp is the deploy-time fingerprint of the endpoint the instance was
    authorized for; the installed team is what it would actually talk to. If
    those disagree the credentials on the volume are being pointed somewhere
    they were never issued for, so the entrypoint fails closed rather than
    choose one.
    """
    team = _gateway_team(tmp_path)
    text = _boot_failure(
        image, team,
        BOBI_AUTH="api_key",
        ANTHROPIC_AUTH_TOKEN="gateway-test-token",
        BOBI_GATEWAY="1",
        # Any well-formed digest that is not sha256("https://gateway-b.test").
        BOBI_GATEWAY_ENDPOINT_SHA256="a" * 64,
    )
    assert "conflicts with the installed team's gateway endpoint" in text


@requires_docker
@pytest.mark.timeout(240)
def test_matching_gateway_endpoint_is_accepted(image: str, tmp_path: Path):
    """The positive control for the guard above.

    Without this, that test passes for any refusal at all — an unrelated fatal
    on the same boot would read as the endpoint guard firing. Same team, same
    stamp mechanism, only the digest now matches: the conflict must not appear
    and the container must reach the supervisor handoff.
    """
    import time

    team = _gateway_team(tmp_path)
    # sha256("https://gateway-b.test"), the endpoint the team declares.
    digest = hashlib.sha256(b"https://gateway-b.test").hexdigest()
    name = "bobi-gateway-endpoint-match"
    _run("docker", "rm", "-f", name)
    try:
        up = _run(
            "docker", "run", "-d", "--name", name,
            "-e", "BOBI_AUTH=api_key",
            "-e", "BOBI_AGENT=claude-smoke",
            "-e", "ANTHROPIC_AUTH_TOKEN=gateway-test-token",
            "-e", "BOBI_EVENT_SERVER=http://127.0.0.1:9",
            "-e", "BOBI_TEAM=/mnt/team",
            "-e", "BOBI_GATEWAY=1",
            "-e", f"BOBI_GATEWAY_ENDPOINT_SHA256={digest}",
            "-v", f"{team}:/mnt/team:ro", image,
        )
        assert up.returncode == 0, up.stderr
        deadline = time.time() + 60
        text = ""
        while time.time() < deadline:
            logs = _run("docker", "logs", name)
            text = logs.stdout + logs.stderr
            if ("Starting manager under the supervisor sidecar" in text
                    or "FATAL:" in text):
                break
            time.sleep(1)
        assert "conflicts with the installed team's gateway endpoint" not in text
        assert "Starting manager under the supervisor sidecar" in text, text
    finally:
        _run("docker", "rm", "-f", name)


@requires_docker
@pytest.mark.timeout(240)
def test_delimiter_injected_brain_identity_is_rejected(image: str,
                                                       tmp_path: Path):
    """A team cannot forge its own gateway identity through its `kind` string.

    The entrypoint reads four identity fields out of one python invocation. A
    `kind` carrying newlines is an attempt to supply the remaining fields — to
    claim, from inside the team package, whatever gateway stamp would satisfy
    the check above. Base64 framing makes that structurally impossible now, and
    the charset guard is the second line: assert the refusal, so a future
    refactor to a plainer framing cannot quietly reopen the vector.
    """
    stamp = "a" * 64
    team = _gateway_team(tmp_path, kind=f'"claude\\n1\\n{stamp}"')
    text = _boot_failure(
        image, team,
        BOBI_AUTH="api_key",
        ANTHROPIC_AUTH_TOKEN="gateway-test-token",
        BOBI_GATEWAY="1",
        BOBI_GATEWAY_ENDPOINT_SHA256=stamp,
    )
    assert "invalid installed team brain identity" in text.lower(), text


@requires_docker
@pytest.mark.timeout(240)
def test_unparseable_team_fails_closed(image: str, tmp_path: Path):
    """A team package that cannot be read is a refusal, never a fallback.

    Downstream of this the entrypoint validates the volume's stamped gateway
    identity against the installed team. With no readable team there is nothing
    to validate against, so booting on defaults would authenticate against an
    unverified endpoint. It must stop instead.

    The private original reached the deeper guard — `Config.load` raising after
    a SUCCESSFUL install — by injecting a broken `bobi.config` over PYTHONPATH.
    That is a unit-level fault injection, not something a real container can be
    put into, so this asserts the reachable half: an unreadable package never
    gets past install.
    """
    team = tmp_path / "team"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "claude-smoke", team)
    (team / "agent.yaml").write_text("agent: [this is not\n  valid: yaml\n")
    text = _boot_failure(image, team, BOBI_AUTH="api_key",
                         ANTHROPIC_API_KEY="sk-test")
    assert "couldn't install team" in text, text
    assert "could not parse" in text.lower(), text


# --- Codex api-key auth materialization --------------------------------------
# Codex does not read OPENAI_API_KEY the way Claude reads ANTHROPIC_API_KEY; it
# expects ~/.codex/auth.json. The entrypoint writes that file, and WHEN it
# declines to write it is the security-relevant half.


def _codex_auth_json(image: str, team_dir: Path, **env: str) -> tuple[str, str]:
    """Boot a codex team and return (auth.json contents, octal mode).

    Returns ("", "") when the file is absent — the expected result whenever the
    entrypoint is supposed to decline to materialize it.
    """
    import time

    name = "bobi-codex-auth"
    _run("docker", "rm", "-f", name)
    args = [
        "docker", "run", "-d", "--name", name,
        "-e", "BOBI_AGENT=codex-smoke",
        "-e", "BOBI_EVENT_SERVER=http://127.0.0.1:9",
        "-e", "BOBI_TEAM=/mnt/team",
    ]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += ["-v", f"{team_dir}:/mnt/team:ro", image]
    try:
        up = _run(*args)
        assert up.returncode == 0, up.stderr
        # The file is written before the manager starts, so wait for either the
        # handoff line or a fatal rather than for a healthy agent — no live
        # OpenAI credential is involved.
        deadline = time.time() + 120
        text = ""
        while time.time() < deadline:
            logs = _run("docker", "logs", name)
            text = logs.stdout + logs.stderr
            if ("Starting manager under the supervisor sidecar" in text
                    or "FATAL:" in text):
                break
            time.sleep(1)
        assert "FATAL:" not in text, text
        probe = _run(
            "docker", "exec", name, "sh", "-c",
            'f=/home/bobi/.codex/auth.json; '
            '[ -f "$f" ] && printf "%s|%s" "$(cat "$f")" "$(stat -c %a "$f")" '
            '|| printf "|"',
        )
        contents, _, mode = probe.stdout.partition("|")
        return contents, mode
    finally:
        _run("docker", "rm", "-f", name)


@requires_docker
@pytest.mark.timeout(300)
def test_codex_api_key_auth_file_is_materialized(image: str):
    """api_key mode turns OPENAI_API_KEY into the auth.json Codex reads."""
    team = REPO_ROOT / "tests" / "fixtures" / "codex-smoke"
    contents, mode = _codex_auth_json(
        image, team, BOBI_AUTH="api_key", OPENAI_API_KEY="sk-materialize-test")
    assert "sk-materialize-test" in contents, contents
    # Private to the agent user: it holds a provider credential.
    assert mode == "600", f"auth.json mode is {mode!r}, expected 600"


# NOT re-homed: "subscription mode removes the codex api-key auth file".
#
# The private original called `materialize_codex_api_key_auth` directly and
# asserted the `BOBI_AUTH=subscription` branch declines to write. Through a real
# boot that branch is unreachable: an earlier guard refuses the container
# outright when subscription mode sees a provider key in the environment
# ("overrides subscription auth"), which fires before materialization is
# reached — verified against 0.51.1, where the branch's own log line never
# appears. The reachable invariant is the refusal, and
# `test_subscription_mode_rejects_api_key` above already covers it.
#
# Worth recording rather than quietly dropping: a test that only passes because
# it bypasses the code path that would have stopped it is not coverage of the
# shipped behaviour. That is the same failure mode as the stale-copy problem
# these tests came here to escape, one level down.
