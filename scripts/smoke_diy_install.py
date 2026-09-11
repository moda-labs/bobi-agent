"""Safety helpers for the clean-wheel DIY installation smoke lane."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

_DROP_PREFIXES = ("AWS_", "AZURE_", "GITHUB_", "GOOGLE_", "OPENAI_", "ANTHROPIC_")
_DROP_NAMES = {"PYTHONPATH", "PYTHONHOME", "BOBI_ROOT", "CLAUDE_CODE_OAUTH_TOKEN"}


def build_child_env(case_root: Path, subject_bin: Path, tool_bin: Path | None = None) -> dict[str, str]:
    """Return a deliberately small environment for installed-package children."""
    root = case_root.resolve()
    home = root / "home"
    bobi_home = root / "bobi-home"
    for path in (home, bobi_home):
        path.mkdir(parents=True, exist_ok=True)
    browser = root / "browser"
    browser.write_text("#!/bin/sh\nexit 0\n")
    browser.chmod(0o755)
    path_entries = [str(subject_bin)]
    if tool_bin is not None:
        path_entries.insert(0, str(tool_bin))
    env = {
        "HOME": str(home),
        "BOBI_HOME": str(bobi_home),
        "TMPDIR": str(root / "tmp"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "BOBI_APP_PORT": "0",
        "PATH": os.pathsep.join(path_entries),
        "BROWSER": str(browser),
    }
    for key, value in os.environ.items():
        if key in _DROP_NAMES or key.startswith(_DROP_PREFIXES):
            continue
        if key in {"LANG", "LC_ALL", "SYSTEMROOT"}:
            env[key] = value
    return env


def run_checked(args: list[str], *, env: dict[str, str], cwd: Path, expected_exit: int = 0,
                timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode != expected_exit:
        raise AssertionError(f"{args[0]} exited {result.returncode}, expected {expected_exit}: {result.stderr[-2000:]}")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_wheel_identity(wheel: Path, manifest: dict[str, str]) -> None:
    if manifest.get("sha256") != sha256(wheel):
        raise AssertionError("wheel hash does not match producer manifest")
    if not wheel.name.startswith("bobi-") or not wheel.name.endswith(".whl"):
        raise AssertionError(f"unexpected wheel artifact: {wheel.name}")


def read_manifest(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(f"invalid DIY wheel manifest: {exc}") from exc
    if not isinstance(value, dict) or not value.get("version") or not value.get("sha256"):
        raise AssertionError("DIY wheel manifest must contain version and sha256")
    return value
