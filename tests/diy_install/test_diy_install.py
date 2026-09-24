"""Behavioral checks run only against the downloaded, non-editable wheel."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, build_opener, HTTPError

import pytest


# These tests must run against the artifact produced by the DIY CI lane. Keep
# ordinary repository test runs from accidentally exercising the source tree.
pytestmark = pytest.mark.skipif(
    not (os.environ.get("BOBI_TEST_WHEEL") and os.environ.get("BOBI_TEST_MANIFEST")),
    reason="DIY consumer tests require BOBI_TEST_WHEEL and BOBI_TEST_MANIFEST",
)

_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "smoke_diy_install.py"
_SPEC = importlib.util.spec_from_file_location("smoke_diy_install", _HELPER)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
assert_wheel_identity = _MODULE.assert_wheel_identity
build_child_env = _MODULE.build_child_env
read_manifest = _MODULE.read_manifest
run_checked = _MODULE.run_checked


def _http(url: str, *, token: str = "", method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"x-bobi-webui-token": token} if token else {}
    if data:
        headers["content-type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        response = build_opener().open(request, timeout=10)
    except HTTPError as exc:
        return exc.code, exc.read().decode()
    return response.status, response.read().decode("utf-8", errors="replace")


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


@pytest.fixture(scope="module")
def subject(tmp_path_factory):
    wheel_raw = os.environ.get("BOBI_TEST_WHEEL")
    manifest_raw = os.environ.get("BOBI_TEST_MANIFEST")
    if not wheel_raw or not manifest_raw:
        pytest.skip("DIY consumer tests require BOBI_TEST_WHEEL and BOBI_TEST_MANIFEST")
    wheel = Path(wheel_raw).resolve()
    manifest = read_manifest(Path(manifest_raw).resolve())
    assert_wheel_identity(wheel, manifest)
    root = tmp_path_factory.mktemp("consumer")
    venv = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = _venv_bin(venv) / ("python.exe" if os.name == "nt" else "python")
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", str(wheel)], check=True)
    env = build_child_env(root, _venv_bin(venv))
    runtime_package = root / "bobi-home" / "agents" / "smoke" / "run" / "package"
    (runtime_package / "roles").mkdir(parents=True)
    (runtime_package / "agent.yaml").write_text("agent: smoke\n")
    return {"root": root, "venv": venv, "bin": _venv_bin(venv), "python": python,
            "env": env, "wheel": wheel, "manifest": manifest,
            "runtime": runtime_package.parent}


def _bobi(s):
    return str(s["bin"] / ("bobi.exe" if os.name == "nt" else "bobi"))


def _wait_stopped(pid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            raise AssertionError("isolated Bobi daemon is still alive under another uid")
        time.sleep(0.1)
    raise AssertionError(f"isolated Bobi daemon {pid} did not stop")


def _start_setup(s):
    result = run_checked([_bobi(s), "setup"], env=s["env"], cwd=s["root"])
    state = s["root"] / "bobi-home" / "webapp"
    port = int((state / "app.port").read_text())
    token = (state / "app.token").read_text().strip()
    pid = int((state / "app.pid").read_text())
    status, body = _http(f"http://127.0.0.1:{port}/api/ping", token=token)
    assert status == 200 and json.loads(body) == {"ok": True}
    return port, token, pid, result


def _stop_setup(s, pid):
    run_checked([_bobi(s), "app", "stop"], env=s["env"], cwd=s["root"])
    _wait_stopped(pid)


def _assert_local_assets(base: str, index: str, token: str = ""):
    refs = set(re.findall(r"(?:href|src)=['\"]([^'\"]+)['\"]", index))
    queue = [urljoin(base, ref) for ref in refs if not ref.startswith(("http:", "https:", "data:", "#"))]
    seen = set()
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        status, body = _http(url, token=token)
        assert status == 200 and body, f"missing local asset: {url}"
        if url.endswith(".css"):
            for ref in re.findall(r"url\((?:\"|')?([^)'\"]+)", body):
                if not ref.startswith(("http:", "https:", "data:", "#")):
                    queue.append(urljoin(url, ref))
        if url.endswith(".js"):
            for ref in re.findall(r"from\s+[\"']([^\"']+)", body):
                if ref.startswith("."):
                    queue.append(urljoin(url, ref))
    assert any(url.endswith(".css") for url in seen)
    assert any(url.endswith(".js") for url in seen)
    assert any(url.endswith(".svg") for url in seen)


def test_wheel_identity_and_version(subject):
    s = subject
    result = run_checked([_bobi(s), "--version"], env=s["env"], cwd=s["root"])
    probe = run_checked([str(s["python"]), "-c", "import bobi,importlib.metadata as m; print(m.version('bobi')); print(bobi.__file__)"], env=s["env"], cwd=s["root"])
    assert probe.stdout.splitlines()[0] == s["manifest"]["version"]
    assert str(s["venv"]) in probe.stdout.splitlines()[1]
    assert result.stdout.strip().endswith(s["manifest"]["version"])
    run_checked([str(s["python"]), "-m", "pip", "check"], env=s["env"], cwd=s["root"])


def test_doctor_fresh_install(subject):
    result = run_checked([_bobi(subject), "agent", "smoke", "doctor"], env=subject["env"], cwd=subject["root"], expected_exit=1)
    output = result.stdout + result.stderr
    assert "Claude CLI" in output and "Bobi install" in output
    assert "Traceback (most recent call last)" not in output


def test_doctor_without_node(subject):
    env = dict(subject["env"], PATH=str(subject["bin"]))
    result = run_checked([_bobi(subject), "agent", "smoke", "doctor"], env=env, cwd=subject["root"], expected_exit=1)
    output = result.stdout + result.stderr
    assert "Node.js 20+" in output and "Install Node.js 20+" in output
    assert "Traceback (most recent call last)" not in output


def test_doctor_with_old_node(subject):
    node18 = os.environ.get("BOBI_DIY_NODE18")
    if not node18:
        pytest.fail("BOBI_DIY_NODE18 must point to setup-node's Node 18 binary")
    assert subprocess.run([node18, "--version"], capture_output=True, text=True).stdout.startswith("v18.")
    env = dict(subject["env"], PATH=os.pathsep.join([str(Path(node18).parent), str(subject["bin"])]))
    result = run_checked([_bobi(subject), "agent", "smoke", "doctor"], env=env, cwd=subject["root"], expected_exit=1)
    output = result.stdout + result.stderr
    assert "Node.js 20+" in output and "reports 'v18" in output
    assert "Traceback (most recent call last)" not in output


def test_setup_shell_without_claude(subject):
    s = subject
    pid = None
    try:
        port, token, pid, _ = _start_setup(s)
        status, body = _http(f"http://127.0.0.1:{port}/api/setup/open", token=token, method="POST", body={"name": "no-claude"})
        assert status == 409
        assert "Claude Code CLI" in body and "claude.com/claude-code" in body
        assert "Traceback" not in body
    finally:
        if pid:
            _stop_setup(s, pid)


def test_setup_hosted_assets_offline(subject):
    s = subject
    tool_dir = s["root"] / "tools"
    tool = tool_dir / "claude"
    tool_dir.mkdir()
    log = s["root"] / "claude.log"
    tool.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\nexit 0\n")
    tool.chmod(0o755)
    root = s["root"] / "hosted"
    env = build_child_env(root, s["bin"], tool.parent)
    env["PATH"] = os.pathsep.join([str(tool.parent), str(s["bin"])])
    hs = dict(s, root=root, env=env)
    pid = None
    try:
        port, token, pid, _ = _start_setup(hs)
        status, _ = _http(f"http://127.0.0.1:{port}/api/setup/open", token=token, method="POST", body={"name": "hosted"})
        assert status == 200
        status, index = _http(f"http://127.0.0.1:{port}/setup/hosted/")
        assert status == 200
        _assert_local_assets(f"http://127.0.0.1:{port}/setup/hosted/", index)
        assert not log.exists() or not log.read_text().strip(), "Claude sentinel was executed"
    finally:
        if pid:
            _stop_setup(hs, pid)


def test_readonly_framework_startup(subject):
    s = subject
    probe = run_checked([str(s["python"]), "-c", "import bobi; from pathlib import Path; print(Path(bobi.__file__).resolve().parent)"], env=s["env"], cwd=s["root"])
    package = Path(probe.stdout.strip())
    dist_info = next(package.parent.glob("bobi-*.dist-info"), None)
    roots = [package] + ([dist_info] if dist_info else [])
    entries = [p for root in roots for p in root.rglob("*") if p.is_file() or p.is_dir()]
    modes = {p: p.stat().st_mode for p in entries}
    try:
        for path in entries:
            path.chmod(path.stat().st_mode & 0o555)
        run_checked([_bobi(s), "--version"], env=s["env"], cwd=s["root"])
        check = run_checked([str(s["python"]), "-c", "from bobi.runtime_guard import verify_framework_integrity_or_raise; print(verify_framework_integrity_or_raise().ok)"], env=s["env"], cwd=s["root"])
        assert check.stdout.strip() == "True"
        port = token = pid = None
        try:
            port, token, pid, _ = _start_setup(s)
        finally:
            if pid:
                _stop_setup(s, pid)
    finally:
        for path, mode in modes.items():
            path.chmod(mode & 0o777)


def test_unreadable_framework_diagnostic(subject):
    s = subject
    target = run_checked([str(s["python"]), "-c", "import bobi; from pathlib import Path; print(Path(bobi.__file__).parent / 'prompts' / 'setup.md')"], env=s["env"], cwd=s["root"]).stdout.strip()
    path = Path(target)
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        failed = subprocess.run([str(s["python"]), "-c", f"open({str(path)!r}, 'rb').read()"], env=s["env"], capture_output=True, text=True)
        assert failed.returncode != 0
        result = run_checked([_bobi(s), "agent", "smoke", "doctor"], env=s["env"], cwd=s["root"], expected_exit=1)
        output = result.stdout + result.stderr
        assert "Bobi install" in output and "unreadable" in output.lower()
        assert "Reinstall or upgrade Bobi" in output
        assert "Traceback (most recent call last)" not in output
    finally:
        path.chmod(mode & 0o777)


def test_guard_keeps_pip_reinstall_working(subject):
    s = subject
    runtime = s["root"] / "runtime"
    package = runtime / "package"
    (package / "roles").mkdir(parents=True)
    (package / "agent.yaml").write_text("agent: smoke\n")
    role = package / "roles" / "ROLE.md"
    role.write_text("role\n")
    before = run_checked([str(s["python"]), "-c", "import bobi; print(bobi.__file__)"], env=s["env"], cwd=s["root"]).stdout.strip()
    script = f"from pathlib import Path; from bobi.runtime_guard import prepare_brain_runtime; r=prepare_brain_runtime(Path({str(runtime)!r})); print(len(r.protected)); print((Path({str(role)!r}).stat().st_mode & 0o222)==0)"
    result = run_checked([str(s["python"]), "-c", script], env=s["env"], cwd=s["root"])
    assert result.stdout.splitlines()[-1] == "True"
    run_checked([str(s["python"]), "-m", "pip", "install", "--quiet", "--force-reinstall", "--no-deps", str(s["wheel"])], env=s["env"], cwd=s["root"])
    after = run_checked([str(s["python"]), "-c", "import bobi; print(bobi.__file__)"], env=s["env"], cwd=s["root"]).stdout.strip()
    assert before == after
