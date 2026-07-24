"""Production-shaped coverage for the event server shipped in the Bobi wheel."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import textwrap
import time
import zipfile
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_BUNDLE = "bobi/event-server/dist/local.js"
PACKAGED_MANIFEST = "bobi/event-server/dist/local.inputs.json"
PACKAGED_METAFILE = "bobi/event-server/dist/local.meta.json"
PACKAGED_NOTICE = "bobi/event-server/dist/THIRD_PARTY_NOTICES.txt"
SDIST_BUNDLE = "event-server/dist/local.js"
SDIST_MANIFEST = "event-server/dist/local.inputs.json"
SDIST_METAFILE = "event-server/dist/local.meta.json"
SDIST_NOTICE = "event-server/dist/THIRD_PARTY_NOTICES.txt"
CHECKOUT_BUILD_ROOTS = (
    Path("__pycache__"),
    Path("bobi/events/__pycache__"),
    Path("event-server/dist"),
    Path("event-server/node_modules"),
    Path("event-server/core/node_modules"),
    Path("event-server/.npm-cache"),
)
RUNTIME_ENVIRONMENT_DENYLIST = frozenset(
    {
        "BOBI_EVENT_SERVER",
        "BOBI_HOME",
        "BOBI_ROOT",
        "DISCORD_APPLICATION_ID",
        "DISCORD_BOT_TOKEN",
        "DISCORD_MESSAGE_CONTENT",
        "LINEAR_WEBHOOK_SECRET",
        "PYTHONHOME",
        "PYTHONPATH",
        "SLACK_SIGNING_SECRET",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_VERIFY_TOKEN",
    }
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 420,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _tree_snapshot(root: Path) -> dict[str, tuple]:
    if not root.exists():
        return {".": ("absent",)}
    snapshot = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_file():
            data = path.read_bytes()
            snapshot[relative] = (
                "file",
                mode,
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
        else:
            snapshot[relative] = ("directory", mode)
    return snapshot


def _checkout_build_snapshot() -> dict:
    status = _run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=PACKAGE_ROOT,
        timeout=30,
    )
    assert status.returncode == 0, status.stderr
    return {
        "git_status": status.stdout,
        "ignored_roots": {
            relative.as_posix(): _tree_snapshot(PACKAGE_ROOT / relative)
            for relative in CHECKOUT_BUILD_ROOTS
        },
    }


def _runtime_probe_environment() -> dict[str, str]:
    """Remove ambient auth/config that could alter the controlled smoke."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in RUNTIME_ENVIRONMENT_DENYLIST
        and not key.upper().startswith("BOBI_ES_")
    }


def _build_packaged_artifacts(tmp_path_factory):
    """Build the direct wheel and the Homebrew-shaped sdist-derived wheel."""

    root = tmp_path_factory.mktemp("packaged-event-server")
    direct_dir = root / "direct"
    direct_dir.mkdir()
    direct = _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(direct_dir),
            str(PACKAGE_ROOT),
        ],
        cwd=root,
    )
    assert direct.returncode == 0, (
        f"direct wheel build failed\nstdout:\n{direct.stdout}\nstderr:\n{direct.stderr}"
    )
    direct_wheels = list(direct_dir.glob("bobi-*.whl"))
    assert len(direct_wheels) == 1

    sdist_dir = root / "sdist"
    sdist_dir.mkdir()
    sdist_build = _run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(sdist_dir),
            str(PACKAGE_ROOT),
        ],
        cwd=root,
    )
    assert sdist_build.returncode == 0, (
        f"sdist build failed\nstdout:\n{sdist_build.stdout}\n"
        f"stderr:\n{sdist_build.stderr}"
    )
    sdists = list(sdist_dir.glob("bobi-*.tar.gz"))
    assert len(sdists) == 1

    unpacked = root / "unpacked"
    unpacked.mkdir()
    with tarfile.open(sdists[0]) as archive:
        archive.extractall(unpacked)
    source_archives = [path for path in unpacked.iterdir() if path.is_dir()]
    assert len(source_archives) == 1

    no_node_path = root / "no-node-path"
    no_node_path.mkdir()
    sdist_wheel_dir = root / "sdist-wheel"
    sdist_wheel_dir.mkdir()
    no_node_env = os.environ.copy()
    no_node_env["PATH"] = str(no_node_path)
    sdist_wheel_build = _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(sdist_wheel_dir),
            str(source_archives[0]),
        ],
        cwd=root,
        env=no_node_env,
    )
    assert sdist_wheel_build.returncode == 0, (
        "wheel-from-sdist build invoked Node/npm or rejected the carried artifact\n"
        f"stdout:\n{sdist_wheel_build.stdout}\n"
        f"stderr:\n{sdist_wheel_build.stderr}"
    )
    sdist_wheels = list(sdist_wheel_dir.glob("bobi-*.whl"))
    assert len(sdist_wheels) == 1

    return {
        "direct_wheel": direct_wheels[0],
        "no_node_env": no_node_env,
        "root": root,
        "sdist": sdists[0],
        "sdist_root": source_archives[0],
        "sdist_wheel": sdist_wheels[0],
    }


@pytest.fixture(scope="module")
def packaged_artifacts(tmp_path_factory):
    """Build artifacts while proving the source checkout remains unchanged."""
    exact_wheel = os.environ.get("BOBI_TEST_WHEEL")
    if exact_wheel:
        wheel = Path(exact_wheel).resolve()
        assert wheel.is_file(), f"BOBI_TEST_WHEEL does not exist: {wheel}"
        return {"sdist_wheel": wheel}

    before = _checkout_build_snapshot()
    try:
        return _build_packaged_artifacts(tmp_path_factory)
    finally:
        assert _checkout_build_snapshot() == before


def _terminate_process_group(pid_file: Path) -> None:
    """Best-effort outer cleanup if the isolated runtime probe is interrupted."""
    try:
        pid = int(pid_file.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _start_managed(
    cleanup: contextlib.ExitStack,
    resource,
):
    """Start a test driver whose cleanup also covers partial startup."""
    cleanup.callback(resource.stop)
    resource.start()
    return resource


RUNTIME_PROBE = textwrap.dedent(
    """
    import hashlib
    import json
    import os
    import signal
    import stat
    import sys
    import time
    import urllib.request
    from pathlib import Path

    sys.path.insert(0, os.environ["BOBI_TEST_INSTALL_DIR"])

    import websocket
    import bobi
    from bobi.events.server import ensure_running
    from bobi.events.signing import serialize_body, sign_headers
    from bobi.runtime_guard import (
        apply_runtime_write_policy,
        check_runtime_write_policy,
    )


    def snapshot(root):
        result = {}
        for path in [root, *sorted(root.rglob("*"))]:
            relative = "." if path == root else str(path.relative_to(root))
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[relative] = [mode, path.stat().st_size, digest]
            else:
                result[relative] = [mode, None, None]
        return result


    def health_payload(base_url):
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                return json.loads(response.read())
        except Exception:
            return None


    def post_json(base_url, path, data, headers=None):
        body = serialize_body(data)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = urllib.request.Request(
            base_url + path,
            data=body.encode(),
            headers=request_headers,
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())


    def register(base_url, name, subscriptions, bubble=None):
        body = {"name": name, "subscriptions": subscriptions}
        headers = {}
        if bubble:
            raw = serialize_body(body)
            headers.update(
                sign_headers(
                    bubble["bubble_id"],
                    bubble["bubble_key"],
                    "POST",
                    "/deployments",
                    raw,
                )
            )
        return post_json(base_url, "/deployments", body, headers)


    def seed_github_grant(base_url, bubble):
        path = "/__test/resource-grants"
        body = {
            "grants": [{"service": "github", "resource": "test-org/test-repo"}]
        }
        raw = serialize_body(body)
        headers = {
            "x-moda-test-secret": os.environ["BOBI_TEST_GRANTS_SECRET"],
        }
        headers.update(
            sign_headers(
                bubble["bubble_id"],
                bubble["bubble_key"],
                "POST",
                path,
                raw,
            )
        )
        post_json(base_url, path, body, headers)


    def exercise_protocol(base_url):
        bootstrap = register(base_url, "packaged-bootstrap", ["_bootstrap"])
        bubble = {
            "bubble_id": bootstrap["bubble_id"],
            "bubble_key": bootstrap["bubble_key"],
        }
        seed_github_grant(base_url, bubble)
        deployment = register(
            base_url,
            "packaged-subscriber",
            ["github:test-org/test-repo"],
            bubble,
        )
        ws = websocket.create_connection(
            base_url.replace("http://", "ws://")
            + f"/deployments/{deployment['deployment_id']}/subscribe?last_seen=0",
            header=[f"Authorization: Bearer {deployment['api_key']}"],
            timeout=10,
        )
        try:
            connected = json.loads(ws.recv())
            if connected.get("type") != "connected":
                raise RuntimeError(f"unexpected WebSocket greeting: {connected}")
            post_json(
                base_url,
                "/webhooks/github",
                {
                    "action": "opened",
                    "issue": {
                        "number": 798,
                        "state": "open",
                        "title": "packaged artifact probe",
                        "user": {"login": "testuser"},
                    },
                    "repository": {"full_name": "test-org/test-repo"},
                },
                {
                    "x-github-delivery": "packaged-798",
                    "x-github-event": "issues",
                },
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                message = json.loads(ws.recv())
                if message.get("type") in ("event", "replay"):
                    event = message["data"]
                    return {
                        "delivery": event.get("delivery"),
                        "source": event.get("source"),
                        "topics": event.get("topics"),
                        "type": event.get("type"),
                        "v": event.get("v"),
                    }, bubble
            raise RuntimeError("GitHub event was not delivered over WebSocket")
        finally:
            ws.close()


    def register_slack_socket(base_url, bubble):
        app_token = os.environ.get("BOBI_TEST_SLACK_APP_TOKEN", "")
        if not app_token:
            return
        path = "/slack/workspaces"
        body = {
            "app_id": os.environ["BOBI_TEST_SLACK_APP_ID"],
            "app_token": app_token,
            "bot_id": os.environ["BOBI_TEST_SLACK_BOT_ID"],
            "bot_token": os.environ["BOBI_TEST_SLACK_BOT_TOKEN"],
            "bot_user_id": os.environ["BOBI_TEST_SLACK_BOT_USER_ID"],
            "signing_secret": "packaged-slack-signing-secret",
            "workspace_id": os.environ["BOBI_TEST_SLACK_TEAM_ID"],
        }
        raw = serialize_body(body)
        headers = sign_headers(
            bubble["bubble_id"],
            bubble["bubble_key"],
            "POST",
            path,
            raw,
        )
        post_json(base_url, path, body, headers)


    def wait_for_drivers(base_url):
        discord_id = os.environ.get("BOBI_TEST_DISCORD_APP_ID", "")
        slack_id = os.environ.get("BOBI_TEST_SLACK_APP_ID", "")
        if not discord_id and not slack_id:
            return {}
        deadline = time.monotonic() + 20
        last = {}
        while time.monotonic() < deadline:
            last = health_payload(base_url) or {}
            discord_ready = not discord_id or any(
                entry.get("application_id") == discord_id
                and entry.get("state") == "connected"
                for entry in last.get("discord_gateway", [])
            )
            slack_ready = not slack_id or any(
                entry.get("application_id") == slack_id
                and entry.get("state") == "connected"
                for entry in last.get("slack_socket", [])
            )
            if discord_ready and slack_ready:
                return {"discord": discord_ready, "slack": slack_ready}
            time.sleep(0.1)
        raise RuntimeError(f"packaged socket drivers did not connect: {last}")


    def stop_server(pid_file):
        if not pid_file.exists():
            return
        try:
            pid = int(pid_file.read_text())
        except (OSError, ValueError):
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if waited == pid:
                return
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


    runtime_root = Path(os.environ["BOBI_TEST_RUNTIME_ROOT"])
    result_path = Path(os.environ["BOBI_TEST_RESULT"])
    npm_trace = Path(os.environ["BOBI_TEST_NPM_TRACE"])
    port = int(os.environ["BOBI_TEST_EVENT_SERVER_PORT"])
    base_url = f"http://127.0.0.1:{port}"
    package_dir = Path(bobi.__file__).resolve().parent
    event_server_dir = package_dir / "event-server"
    pid_file = runtime_root / "state" / "event-server.pid"

    guard = apply_runtime_write_policy(runtime_root)
    policy = check_runtime_write_policy(runtime_root)
    before = snapshot(event_server_dir)
    status = None
    error = None
    protocol = None
    drivers = None
    try:
        try:
            child_env = json.loads(os.environ.get("BOBI_TEST_CHILD_ENV", "{}"))
            child_env["BOBI_ES_TEST_GRANTS_SECRET"] = os.environ[
                "BOBI_TEST_GRANTS_SECRET"
            ]
            status = ensure_running(
                port,
                bind="127.0.0.1",
                project_path=runtime_root,
                extra_env=child_env,
            )
            if status == "started":
                protocol, bubble = exercise_protocol(base_url)
                register_slack_socket(base_url, bubble)
                drivers = wait_for_drivers(base_url)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        payload = {
            "bobi_package": str(package_dir),
            "bundle_exists": (event_server_dir / "dist" / "local.js").is_file(),
            "drivers": drivers,
            "error": error,
            "event_server_mode": stat.S_IMODE(event_server_dir.stat().st_mode),
            "event_server_read_only": all(
                not stat.S_IMODE(path.lstat().st_mode) & 0o222
                for path in [event_server_dir, *event_server_dir.rglob("*")]
                if not path.is_symlink()
            ),
            "guard_kinds": [root.kind for root in guard.protected],
            "health": health_payload(base_url),
            "node_modules_exists": (event_server_dir / "node_modules").exists(),
            "npm_trace": npm_trace.read_text() if npm_trace.exists() else "",
            "policy_detail": policy.detail,
            "policy_ok": policy.ok,
            "protocol": protocol,
            "snapshot_unchanged": before == snapshot(event_server_dir),
            "status": status,
        }
        result_path.write_text(json.dumps(payload, sort_keys=True))
    finally:
        stop_server(pid_file)
    """
)


def test_managed_driver_setup_cleans_partially_started_resources():
    events = []

    class Resource:
        def __init__(self, name, *, fail_start=False):
            self.name = name
            self.fail_start = fail_start

        def start(self):
            events.append(f"{self.name}:start")
            if self.fail_start:
                raise RuntimeError(f"{self.name} failed")

        def stop(self):
            events.append(f"{self.name}:stop")

    with pytest.raises(RuntimeError, match="second failed"):
        with contextlib.ExitStack() as cleanup:
            _start_managed(cleanup, Resource("first"))
            _start_managed(cleanup, Resource("second", fail_start=True))

    assert events == [
        "first:start",
        "second:start",
        "second:stop",
        "first:stop",
    ]


def test_managed_driver_cleanup_continues_after_one_stop_fails():
    events = []

    class Resource:
        def __init__(self, name, *, fail_stop=False):
            self.name = name
            self.fail_stop = fail_stop

        def start(self):
            events.append(f"{self.name}:start")

        def stop(self):
            events.append(f"{self.name}:stop")
            if self.fail_stop:
                raise RuntimeError(f"{self.name} cleanup failed")

    with pytest.raises(RuntimeError, match="second cleanup failed"):
        with contextlib.ExitStack() as cleanup:
            _start_managed(cleanup, Resource("first"))
            _start_managed(cleanup, Resource("second", fail_stop=True))

    assert events == [
        "first:start",
        "second:start",
        "second:stop",
        "first:stop",
    ]


def test_runtime_probe_environment_removes_ambient_server_auth(monkeypatch):
    monkeypatch.setenv("BOBI_ES_WEBHOOK_SECRET", "ambient-github")
    monkeypatch.setenv("BOBI_ES_SLACK_SIGNING_SECRET", "ambient-slack")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "mapped-slack")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "mapped-discord")
    monkeypatch.setenv("BOBI_ROOT", "/ambient/runtime")
    monkeypatch.setenv("BOBI_TEST_SAFE_SENTINEL", "preserved")

    environment = _runtime_probe_environment()

    assert "BOBI_ES_WEBHOOK_SECRET" not in environment
    assert "BOBI_ES_SLACK_SIGNING_SECRET" not in environment
    assert "SLACK_SIGNING_SECRET" not in environment
    assert "DISCORD_BOT_TOKEN" not in environment
    assert "BOBI_ROOT" not in environment
    assert environment["BOBI_TEST_SAFE_SENTINEL"] == "preserved"


def test_archives_are_complete_reproducible_and_contain_no_install_state(
    packaged_artifacts, tmp_path,
):
    from bobi.events import artifact

    direct_wheel = packaged_artifacts["direct_wheel"]
    sdist_wheel = packaged_artifacts["sdist_wheel"]
    with zipfile.ZipFile(direct_wheel) as direct, zipfile.ZipFile(
        sdist_wheel
    ) as derived:
        direct_members = set(direct.namelist())
        derived_members = set(derived.namelist())
        required_wheel = {PACKAGED_BUNDLE, PACKAGED_MANIFEST, PACKAGED_NOTICE}
        assert required_wheel <= direct_members
        assert required_wheel <= derived_members
        assert PACKAGED_METAFILE not in direct_members
        assert PACKAGED_METAFILE not in derived_members
        assert direct.read(PACKAGED_BUNDLE) == derived.read(PACKAGED_BUNDLE)
        direct_root = tmp_path / "direct-wheel"
        derived_root = tmp_path / "derived-wheel"
        direct.extractall(direct_root)
        derived.extractall(derived_root)
        direct_manifest = artifact.validate_artifact(
            direct_root / "bobi" / "event-server",
            verify_inputs=False,
        )
        derived_manifest = artifact.validate_artifact(
            derived_root / "bobi" / "event-server",
            verify_inputs=False,
        )
        assert direct_manifest["outputs"] == derived_manifest["outputs"]
        assert direct.read(PACKAGED_NOTICE) == artifact.notice_bytes()
        assert derived.read(PACKAGED_NOTICE) == artifact.notice_bytes()
        all_wheel_members = direct_members | derived_members
        assert not any(
            "node_modules/" in name
            or ".npm-cache/" in name
            or "bobi-event-server-build-" in name
            for name in all_wheel_members
        )

    with tarfile.open(packaged_artifacts["sdist"]) as source:
        members = {member.name for member in source.getmembers()}
        assert any(name.endswith("/hatch_build.py") for name in members)
        for required in (SDIST_BUNDLE, SDIST_MANIFEST, SDIST_NOTICE):
            assert any(name.endswith(f"/{required}") for name in members)
        assert not any(name.endswith(f"/{SDIST_METAFILE}") for name in members)
        assert not any(
            "node_modules/" in name
            or ".npm-cache/" in name
            or "bobi-event-server-build-" in name
            for name in members
        )


@pytest.mark.parametrize(
    "mutation",
    ["source-input", "bundle", "notice", "manifest"],
)
def test_changed_source_archive_cannot_reuse_carried_artifact(
    packaged_artifacts, tmp_path, mutation,
):
    source = tmp_path / "source"
    shutil.copytree(packaged_artifacts["sdist_root"], source)
    if mutation == "source-input":
        with (source / "event-server" / "src" / "local.ts").open("a") as stream:
            stream.write("\n// patched after artifact generation\n")
    elif mutation == "bundle":
        (source / SDIST_BUNDLE).write_bytes(b"tampered bundle")
    elif mutation == "notice":
        (source / SDIST_NOTICE).write_bytes(b"tampered notice")
    else:
        (source / SDIST_MANIFEST).write_text("{")
    output = tmp_path / "wheel"
    output.mkdir()

    result = _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
            str(source),
        ],
        cwd=tmp_path,
        env=packaged_artifacts["no_node_env"],
    )

    assert result.returncode != 0
    diagnostic = result.stdout + result.stderr
    assert "carried source-archive artifact is invalid" in diagnostic
    assert "Node.js 20" in diagnostic


def test_installed_wheel_starts_without_mutating_frozen_event_server(
    packaged_artifacts, tmp_path,
):
    """The sdist-derived wheel starts under the real guard without runtime npm."""
    from tests.integration.test_discord_gateway import (
        APP_ID as DISCORD_APP_ID,
        BOT_TOKEN as DISCORD_BOT_TOKEN,
        _GatewayStub,
        _RestStub as DiscordRestStub,
    )
    from tests.integration.test_slack_socket_mode import (
        APP_ID as SLACK_APP_ID,
        APP_TOKEN as SLACK_APP_TOKEN,
        BOT_ID as SLACK_BOT_ID,
        BOT_TOKEN as SLACK_BOT_TOKEN,
        BOT_USER_ID as SLACK_BOT_USER_ID,
        TEAM_ID as SLACK_TEAM_ID,
        _generate_tls_material,
        _SlackRestStub,
        _SocketStub,
    )

    wheel = packaged_artifacts["sdist_wheel"]
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = set(archive.namelist())

    install_dir = tmp_path / "installed"
    install = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-compile",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheel),
        ],
        cwd=tmp_path,
    )
    assert install.returncode == 0, (
        f"wheel install failed\nstdout:\n{install.stdout}\nstderr:\n{install.stderr}"
    )

    real_npm = shutil.which("npm")
    assert real_npm, "npm is required to reproduce the affected installed startup"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    npm_shim = shim_dir / "npm"
    npm_shim.write_text(
        "#!/bin/sh\n"
        'printf "%s\\t%s\\n" "$PWD" "$*" >> "$BOBI_TEST_NPM_TRACE"\n'
        'if [ "$BOBI_TEST_NPM_MODE" = "passthrough" ]; then\n'
        '  exec "$BOBI_TEST_REAL_NPM" "$@"\n'
        "fi\n"
        'printf "unexpected runtime npm invocation\\n" >&2\n'
        "exit 97\n"
    )
    npm_shim.chmod(0o755)

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    npm_cache = tmp_path / "npm-cache"
    npm_cache.mkdir()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    result_path = tmp_path / "runtime-result.json"
    npm_trace = tmp_path / "npm-trace.log"
    hostile_sentinel = tmp_path / "hostile-node-module-executed"
    preload = tmp_path / "hostile-preload.cjs"
    preload.write_text(
        "require('fs').writeFileSync(process.env.BOBI_TEST_HOSTILE_SENTINEL, "
        "'preload');\n"
    )
    hostile_modules = tmp_path / "hostile-modules"
    for name in ("bufferutil", "utf-8-validate"):
        module = hostile_modules / name / "index.js"
        module.parent.mkdir(parents=True)
        module.write_text(
            "require('fs').writeFileSync("
            "process.env.BOBI_TEST_HOSTILE_SENTINEL, "
            f"'{name}'); module.exports = {{}};\n"
        )
    has_bundle = PACKAGED_BUNDLE in wheel_members

    with contextlib.ExitStack() as cleanup:
        gateway = _start_managed(cleanup, _GatewayStub(tmp_path))
        discord_rest = _start_managed(
            cleanup,
            DiscordRestStub(gateway.ws_port),
        )
        ca_cert, server_cert, server_key = _generate_tls_material(tmp_path)
        slack_socket = _start_managed(
            cleanup,
            _SocketStub(tmp_path, server_cert, server_key),
        )
        slack_rest = _start_managed(cleanup, _SlackRestStub(slack_socket))

        env = _runtime_probe_environment()
        env.update(
            {
                "BOBI_HOME": str(home_dir),
                "BOBI_TEST_CHILD_ENV": json.dumps(
                    {
                        "BOBI_ES_DISCORD_API_URL": (
                            f"http://127.0.0.1:{discord_rest.port}/"
                        ),
                        "BOBI_ES_DISCORD_APPLICATION_ID": DISCORD_APP_ID,
                        "BOBI_ES_DISCORD_BOT_TOKEN": DISCORD_BOT_TOKEN,
                        "BOBI_ES_SLACK_API_URL": (
                            f"http://127.0.0.1:{slack_rest.port}/"
                        ),
                        "NODE_EXTRA_CA_CERTS": str(ca_cert),
                        "NODE_TLS_REJECT_UNAUTHORIZED": "1",
                    }
                ),
                "BOBI_TEST_DISCORD_APP_ID": DISCORD_APP_ID,
                "BOBI_TEST_EVENT_SERVER_PORT": str(_free_port()),
                "BOBI_TEST_GRANTS_SECRET": "packaged-artifact-grants",
                "BOBI_TEST_HOSTILE_SENTINEL": str(hostile_sentinel),
                "BOBI_TEST_INSTALL_DIR": str(install_dir),
                "BOBI_TEST_NPM_MODE": "fail" if has_bundle else "passthrough",
                "BOBI_TEST_NPM_TRACE": str(npm_trace),
                "BOBI_TEST_REAL_NPM": real_npm,
                "BOBI_TEST_RESULT": str(result_path),
                "BOBI_TEST_RUNTIME_ROOT": str(runtime_root),
                "BOBI_TEST_SLACK_APP_ID": SLACK_APP_ID,
                "BOBI_TEST_SLACK_APP_TOKEN": SLACK_APP_TOKEN,
                "BOBI_TEST_SLACK_BOT_ID": SLACK_BOT_ID,
                "BOBI_TEST_SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
                "BOBI_TEST_SLACK_BOT_USER_ID": SLACK_BOT_USER_ID,
                "BOBI_TEST_SLACK_TEAM_ID": SLACK_TEAM_ID,
                "NODE_OPTIONS": f"--require={preload}",
                "NODE_PATH": str(hostile_modules),
                "NO_PROXY": "127.0.0.1,localhost",
                "PATH": f"{shim_dir}{os.pathsep}{env['PATH']}",
                "PYTHONDONTWRITEBYTECODE": "1",
                "npm_config_cache": str(npm_cache),
            }
        )
        cleanup.callback(
            _terminate_process_group,
            runtime_root / "state" / "event-server.pid",
        )
        probe = _run(
            [sys.executable, "-c", RUNTIME_PROBE],
            cwd=tmp_path,
            env=env,
        )
    assert probe.returncode == 0, (
        f"installed runtime probe failed\nstdout:\n{probe.stdout}\n"
        f"stderr:\n{probe.stderr}"
    )
    result = json.loads(result_path.read_text())

    expected_package = install_dir / "bobi"
    failures = []
    if not has_bundle:
        failures.append(f"wheel is missing {PACKAGED_BUNDLE}")
    if Path(result["bobi_package"]) != expected_package:
        failures.append(
            f"runtime imported {result['bobi_package']}, expected {expected_package}"
        )
    if "bobi-package" not in result["guard_kinds"]:
        failures.append(f"real guard did not protect Bobi: {result['guard_kinds']}")
    if not result["policy_ok"] or not result["event_server_read_only"]:
        failures.append(
            "installed event server was not frozen: "
            f"mode={oct(result['event_server_mode'])}, "
            f"policy={result['policy_detail']}"
        )
    if result["status"] != "started":
        failures.append(
            f"installed startup did not start: status={result['status']!r}, "
            f"error={result['error']!r}"
        )
    health = result["health"] or {}
    if health.get("status") != "ok" or health.get("mode") != "local":
        failures.append(f"health endpoint unavailable: {result['health']!r}")
    if result["npm_trace"]:
        failures.append(f"installed startup invoked npm:\n{result['npm_trace']}")
    if result["node_modules_exists"]:
        failures.append("installed startup created event-server/node_modules")
    if any(npm_cache.iterdir()):
        failures.append("installed startup wrote to the npm cache")
    if result["protocol"] != {
        "delivery": "bulk",
        "source": "github",
        "topics": ["github:test-org/test-repo"],
        "type": "github.issues",
        "v": 2,
    }:
        failures.append(f"packaged protocol probe failed: {result['protocol']!r}")
    if result["drivers"] != {"discord": True, "slack": True}:
        failures.append(f"packaged socket drivers failed: {result['drivers']!r}")
    if hostile_sentinel.exists():
        failures.append(
            "embedded Node executed inherited preload or optional-addon code"
        )
    if not result["snapshot_unchanged"]:
        failures.append("installed event-server tree changed during startup")

    if failures:
        log_path = runtime_root / "state" / "event-server.log"
        if log_path.is_file():
            failures.append(f"event-server.log:\n{log_path.read_text()}")
    assert not failures, "\n".join(failures)
