"""GitHub comment redaction at the agent command boundary."""

import json
import os
import subprocess
import sys
from pathlib import Path

from hatchling.builders.wheel import WheelBuilder

from bobi.github_redaction import (
    PYTHON_EXECUTABLE_ENV,
    REAL_GH_ENV,
    install_github_comment_redaction,
    shim_dir,
)


def _fake_gh(tmp_path: Path) -> tuple[Path, Path]:
    capture = tmp_path / "capture.json"
    executable = tmp_path / "real-gh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['GH_CAPTURE']).write_text(json.dumps({\n"
        "    'argv': sys.argv[1:],\n"
        "    'stdin': sys.stdin.read(),\n"
        "}))\n"
    )
    executable.chmod(0o755)
    return executable, capture


def _run_shim(tmp_path: Path, *args: str, stdin: str = ""):
    real_gh, capture = _fake_gh(tmp_path)
    env = dict(os.environ)
    env.update({
        REAL_GH_ENV: str(real_gh),
        PYTHON_EXECUTABLE_ENV: sys.executable,
        "GH_CAPTURE": str(capture),
    })
    result = subprocess.run(
        [str(shim_dir() / "gh"), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    payload = json.loads(capture.read_text()) if capture.exists() else None
    return result, payload


def test_issue_comment_inline_body_is_redacted(tmp_path):
    secret = "github_pat_" + "A" * 30

    result, payload = _run_shim(
        tmp_path, "issue", "comment", "12", "--body", f"token: {secret}",
    )

    assert result.returncode == 0
    assert payload["argv"] == [
        "issue", "comment", "12", "--body-file", "-",
    ]
    assert payload["stdin"] == "token: [redacted]"
    assert secret not in payload["stdin"]


def test_pr_comment_body_file_is_redacted(tmp_path):
    secret = "ghp_" + "b" * 36
    body = tmp_path / "comment.md"
    body.write_text(f"logs contain {secret}\n")

    result, payload = _run_shim(
        tmp_path, "pr", "comment", "42", "--body-file", str(body),
    )

    assert result.returncode == 0
    assert payload["argv"] == ["pr", "comment", "42", "--body-file", "-"]
    assert payload["stdin"] == "logs contain [redacted]\n"


def test_clean_comment_body_is_unchanged(tmp_path):
    result, payload = _run_shim(
        tmp_path, "pr", "comment", "42", "--body", "tests are green",
    )

    assert result.returncode == 0
    assert payload["stdin"] == "tests are green"
    assert "redacted" not in result.stderr


def test_comment_without_inspectable_body_fails_closed(tmp_path):
    result, payload = _run_shim(tmp_path, "issue", "comment", "12")

    assert result.returncode == 2
    assert payload is None
    assert "--body or --body-file" in result.stderr


def test_non_comment_gh_command_passes_through(tmp_path):
    result, payload = _run_shim(tmp_path, "issue", "view", "12")

    assert result.returncode == 0
    assert payload == {"argv": ["issue", "view", "12"], "stdin": ""}


def test_comment_help_passes_through_without_a_body(tmp_path):
    result, payload = _run_shim(tmp_path, "issue", "comment", "--help")

    assert result.returncode == 0
    assert payload == {"argv": ["issue", "comment", "--help"], "stdin": ""}


def test_install_prepends_shim_and_remembers_real_gh(tmp_path):
    real_bin = tmp_path / "bin"
    real_bin.mkdir()
    real_gh = real_bin / "gh"
    real_gh.write_text("#!/bin/sh\n")
    real_gh.chmod(0o755)
    env = {"PATH": f"{real_bin}{os.pathsep}/usr/bin"}

    install_github_comment_redaction(env)

    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(shim_dir())
    assert env[REAL_GH_ENV] == str(real_gh)
    assert env[PYTHON_EXECUTABLE_ENV] == sys.executable


def test_install_preserves_missing_gh_for_dependency_checks(tmp_path):
    env = {
        "PATH": str(tmp_path),
        REAL_GH_ENV: "/stale/missing/gh",
        PYTHON_EXECUTABLE_ENV: "/stale/python",
    }

    install_github_comment_redaction(env)

    assert env == {"PATH": str(tmp_path)}


def test_wheel_includes_the_redaction_module_and_gh_shim():
    selected = {
        item.distribution_path
        for item in WheelBuilder(str(Path(__file__).parents[1]))
        .recurse_selected_project_files()
    }

    assert "bobi/github_redaction.py" in selected
    assert "bobi/bin/gh" in selected
