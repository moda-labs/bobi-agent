"""GitHub comment redaction at the agent command boundary."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("import_path", ["cwd", "pythonpath"])
def test_shim_ignores_project_redaction_modules(tmp_path, monkeypatch, import_path):
    project = tmp_path / "project"
    package = project / "bobi"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "github_redaction.py").write_text(
        "import os, subprocess, sys\n"
        "raise SystemExit(subprocess.call([os.environ['BOBI_REAL_GH'], *sys.argv[1:]]))\n"
    )
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.chdir(tmp_path)
    if import_path == "cwd":
        monkeypatch.chdir(project)
    else:
        monkeypatch.setenv("PYTHONPATH", str(project))
    secret = "github_pat_" + "A" * 30

    result, payload = _run_shim(
        tmp_path, "issue", "comment", "12", "--body", secret,
    )

    assert result.returncode == 0
    assert payload == {
        "argv": ["issue", "comment", "12", "--body-file", "-"],
        "stdin": "[redacted]",
    }
    assert secret not in json.dumps(payload)

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

@pytest.mark.parametrize("flag", ["-b", "--body", "-b=", "--body=", "-b{body}"])
def test_inline_flag_forms_are_redacted(tmp_path, flag):
    secret = "github_pat_" + "A" * 30
    body = f"output: {secret}"
    args = ([flag.format(body=body)] if "{body}" in flag else
            [flag + body] if flag.endswith("=") else [flag, body])

    result, payload = _run_shim(tmp_path, "pr", "comment", "42", *args)

    assert result.returncode == 0
    assert payload["stdin"] == "output: [redacted]"
    assert secret not in json.dumps(payload)

@pytest.mark.parametrize("flag", ["-F", "--body-file", "-F=", "--body-file=", "-F{file}"])
def test_body_file_flag_forms_are_redacted(tmp_path, flag):
    body_file = tmp_path / "body.txt"
    body_file.write_text("github_pat_" + "A" * 30)
    args = ([flag.format(file=body_file)] if "{file}" in flag else
            [flag + str(body_file)] if flag.endswith("=") else [flag, str(body_file)])

    result, payload = _run_shim(tmp_path, "issue", "comment", "12", *args)

    assert result.returncode == 0
    assert payload["stdin"] == "[redacted]"
    assert body_file.read_text() == "github_pat_" + "A" * 30

@pytest.mark.parametrize("flag", ["--body-file", "-F"])
def test_stdin_body_is_redacted(tmp_path, flag):
    result, payload = _run_shim(
        tmp_path, "issue", "comment", "12", flag, "-",
        stdin="github_pat_" + "A" * 30,
    )

    assert result.returncode == 0
    assert payload["stdin"] == "[redacted]"

@pytest.mark.parametrize("flag", ["--help", "--delete-last"])
@pytest.mark.parametrize("interactive", ["-e", "-w"])
def test_overridden_read_only_flags_cannot_bypass_inspection(tmp_path, flag, interactive):
    result, payload = _run_shim(
        tmp_path, "issue", "comment", "12", flag, flag + "=false", interactive,
    )

    assert result.returncode == 2
    assert payload is None

@pytest.mark.parametrize("args", [
    ["--body"], ["--body-file"], ["--body-file", "/nonexistent/comment.md"],
    ["--body", "first", "--body", "second"],
    ["--editor"], ["--web"], ["--repo", "--help"],
])
def test_uninspectable_or_ambiguous_bodies_fail_closed(tmp_path, args):
    result, payload = _run_shim(tmp_path, "issue", "comment", "12", *args)

    assert result.returncode == 2
    assert payload is None

def test_non_utf8_body_file_fails_closed(tmp_path):
    body_file = tmp_path / "body.txt"
    body_file.write_bytes(b"\xff")

    result, payload = _run_shim(
        tmp_path, "pr", "comment", "42", "-F", str(body_file),
    )

    assert result.returncode == 2
    assert payload is None

def test_non_comment_arguments_named_issue_comment_pass_through(tmp_path):
    args = ["api", "issue", "comment", "--body", "unchanged"]
    result, payload = _run_shim(tmp_path, *args, stdin="unchanged input")

    assert result.returncode == 0
    assert payload == {"argv": args, "stdin": "unchanged input"}

def test_reinstall_removes_shim_when_real_gh_disappears(tmp_path):
    env = {"PATH": str(tmp_path)}
    real_gh = tmp_path / "gh"
    real_gh.write_text("#!/bin/sh\n")
    real_gh.chmod(0o755)
    install_github_comment_redaction(env)
    real_gh.unlink()

    install_github_comment_redaction(env)

    assert env == {"PATH": str(tmp_path)}

@pytest.mark.parametrize("prefix", [
    ["-R", "example/support"], ["--repo=example/support"],
    ["--help=false"], ["issue", "-R", "example/support"],
])
def test_comment_with_inherited_flags_is_protected(tmp_path, prefix):
    commands = ["comment"] if prefix[0] == "issue" else ["issue", "comment"]
    result, payload = _run_shim(
        tmp_path, *prefix, *commands, "12", "--body", "github_pat_" + "A" * 30,
    )

    assert result.returncode == 0
    assert payload["stdin"] == "[redacted]"

@pytest.mark.parametrize("args", [
    ["--delete-last", "--yes"], ["--delete-last=true", "--yes"],
    ["--help=true"], ["-h"],
])
def test_read_only_comment_modes_pass_through(tmp_path, args):
    result, payload = _run_shim(tmp_path, "issue", "comment", "12", *args)

    assert result.returncode == 0
    assert payload["argv"] == ["issue", "comment", "12", *args]

def test_shim_does_not_read_flags_after_delimiter(tmp_path):
    result, payload = _run_shim(
        tmp_path, "pr", "comment", "--body", "safe", "--", "--body-file",
    )

    assert result.returncode == 0
    assert payload["argv"] == ["pr", "comment", "--body-file", "-", "--", "--body-file"]
    assert payload["stdin"] == "safe"

def test_empty_path_entries_are_preserved_when_gh_is_missing(tmp_path):
    env = {"PATH": str(tmp_path) + os.pathsep}
    original = dict(env)

    install_github_comment_redaction(env)

    assert env == original

@pytest.mark.parametrize("runtime", ["spawn", "direct"])
def test_runtime_installs_working_comment_protection(tmp_path, monkeypatch, runtime):
    from bobi.env import agent_spawn_env
    from bobi.runtime_guard import prepare_brain_runtime

    real_gh, capture = _fake_gh(tmp_path)
    real_gh.rename(tmp_path / "gh")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    monkeypatch.delenv(REAL_GH_ENV, raising=False)
    monkeypatch.setenv("GH_CAPTURE", str(capture))
    monkeypatch.setattr("bobi.runtime_guard.verify_framework_integrity_or_raise", lambda: None)
    monkeypatch.setattr("bobi.runtime_guard.apply_runtime_write_policy", lambda root: None)
    if runtime == "spawn":
        env = agent_spawn_env()
    else:
        prepare_brain_runtime()
        env = dict(os.environ)
    install_github_comment_redaction(env)

    result = subprocess.run(
        ["/bin/sh", "-c", 'gh issue comment 12 --body "$COMMENT"'],
        env={**env, "COMMENT": "github_pat_" + "A" * 30},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text())["stdin"] == "[redacted]"
    assert env["PATH"].split(os.pathsep).count(str(shim_dir())) == 1

def test_edit_last_comment_is_redacted(tmp_path):
    result, payload = _run_shim(
        tmp_path, "pr", "comment", "42", "--edit-last", "--create-if-none",
        "--body", "github_pat_" + "A" * 30,
    )

    assert result.returncode == 0
    assert payload["stdin"] == "[redacted]"
    assert payload["argv"] == [
        "pr", "comment", "42", "--edit-last", "--create-if-none", "--body-file", "-",
    ]

@pytest.mark.asyncio
async def test_claude_hook_protects_comments_after_login_shell_startup(tmp_path, monkeypatch):
    from claude_agent_sdk import HookMatcher
    from bobi.brain.claude import ClaudeBrain
    from bobi.env import agent_spawn_env

    real_gh, capture = _fake_gh(tmp_path)
    real_gh.rename(tmp_path / "gh")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    monkeypatch.delenv(REAL_GH_ENV, raising=False)
    monkeypatch.setenv("GH_CAPTURE", str(capture))
    env = agent_spawn_env()
    monkeypatch.setenv(REAL_GH_ENV, env[REAL_GH_ENV])
    existing = HookMatcher(matcher="Read", hooks=[])
    hooks = {"PreToolUse": [existing]}
    session = ClaudeBrain().make_session(
        cwd=str(tmp_path), system_prompt=None, options={"hooks": hooks},
    )
    installed = session._options.hooks["PreToolUse"]
    assert len(installed) == 2
    assert installed[0] is existing
    assert hooks == {"PreToolUse": [existing]}
    command = 'gh issue comment 12 --body "$COMMENT"'
    output = await installed[-1].hooks[0](
        {"tool_name": "Bash", "tool_input": {"command": command, "timeout": 1000}},
        "tool-1", {},
    )
    specific = output["hookSpecificOutput"]
    assert "permissionDecision" not in specific
    assert specific["updatedInput"]["timeout"] == 1000
    result = subprocess.run(
        ["/bin/bash", "-lc", specific["updatedInput"]["command"]],
        env={**env, "COMMENT": "github_pat_" + "A" * 30},
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text())["stdin"] == "[redacted]"

@pytest.mark.parametrize("resume", [None, "thread-1"])
def test_codex_disables_login_and_snapshots_only_with_redaction_installed(monkeypatch, resume):
    from bobi.brain.codex import _CodexSession

    session = _CodexSession(cwd="/tmp", instructions="", resume=resume)
    monkeypatch.delenv(REAL_GH_ENV, raising=False)
    original = session._build_argv()
    assert "allow_login_shell=false" not in original
    monkeypatch.setenv(REAL_GH_ENV, "/fake/real-gh")

    protected = session._build_argv()

    assert "allow_login_shell=false" in protected
    assert "features.shell_snapshot=false" in protected

def test_claude_without_gh_keeps_existing_hooks(monkeypatch):
    from bobi.brain.claude import ClaudeBrain

    monkeypatch.delenv(REAL_GH_ENV, raising=False)
    session = ClaudeBrain().make_session(cwd="/tmp", system_prompt=None)

    assert not session._options.hooks

@pytest.mark.asyncio
async def test_claude_one_shot_keeps_comment_protection(monkeypatch):
    from bobi.brain.claude import ClaudeBrain

    monkeypatch.setenv(REAL_GH_ENV, "/fake/real-gh")
    captured = []

    async def query(prompt, options):
        captured.append(options)
        return
        yield

    monkeypatch.setattr("claude_agent_sdk.query", query)
    async for message in ClaudeBrain().stream_once(system_prompt=None, user_prompt="local test"):
        pass

    assert captured[0].hooks["PreToolUse"][-1].matcher == "Bash"

@pytest.mark.asyncio
async def test_claude_hook_does_not_change_permissions_or_unrelated_tools():
    from bobi.github_redaction import _protect_github_shell

    assert await _protect_github_shell({"tool_name": "Read", "tool_input": {}}, None, {}) == {}
    output = await _protect_github_shell({"tool_name": "Bash", "tool_input": {}}, None, {})
    assert output["continue"] is False
