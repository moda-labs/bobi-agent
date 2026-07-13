from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


@dataclass
class FakeClaudeAgentOptions:
    kwargs: dict[str, Any]

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeClaudeSDKClient:
    def __init__(self, options: FakeClaudeAgentOptions) -> None:
        self.options = options


class TestProtectedAgentWrite:
    def test_write_inside_workspace_venv_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        target = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "x.py"
        assert is_protected_agent_write(
            "Write", {"file_path": str(target)}, cwd=tmp_path)

    def test_write_inside_normal_workspace_file_is_allowed(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        target = tmp_path / "src" / "app.py"
        assert not is_protected_agent_write(
            "Write", {"file_path": str(target)}, cwd=tmp_path)

    def test_relative_path_from_protected_cwd_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        cwd = tmp_path / ".venv" / "lib"
        cwd.mkdir(parents=True)
        assert is_protected_agent_write("Write", {"file_path": "site.py"}, cwd=cwd)

    def test_symlink_into_protected_root_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        protected = tmp_path / ".venv" / "lib"
        protected.mkdir(parents=True)
        link = tmp_path / "linked-venv"
        link.symlink_to(protected, target_is_directory=True)
        assert is_protected_agent_write(
            "Edit", {"file_path": str(link / "site.py")}, cwd=tmp_path)

    @pytest.mark.parametrize("tool,path_key", [
        ("Edit", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookEdit", "notebook_path"),
    ])
    def test_file_tools_inside_node_modules_are_protected(
        self, tmp_path, tool, path_key,
    ):
        from bobi.brain.claude_hooks import is_protected_agent_write

        target = tmp_path / "node_modules" / "pkg" / "index.js"
        assert is_protected_agent_write(tool, {path_key: str(target)}, cwd=tmp_path)

    def test_bash_mutation_with_explicit_protected_path_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        target = tmp_path / ".venv" / "lib" / "patched.py"
        command = f"echo patched > {target}"
        assert is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_mutation_after_cd_into_protected_path_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        protected = tmp_path / ".venv" / "lib"
        protected.mkdir(parents=True)
        command = f"cd {protected} && echo patched > site.py"
        assert is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_relative_cd_then_mutation_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        protected = tmp_path / ".venv" / "lib"
        protected.mkdir(parents=True)
        command = "cd .venv/lib && rm site.py"
        assert is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_normal_cd_then_relative_protected_mutation_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        (tmp_path / "src").mkdir()
        command = "cd src && rm ../.venv/file.py"
        assert is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_attached_redirection_to_protected_path_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        command = "echo patched >.venv/lib/site.py"
        assert is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_fd_redirection_to_protected_path_is_protected(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        command = "echo patched 2>.venv/log.txt"
        assert is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    @pytest.mark.parametrize("operator", [">|", "&>", "&>>"])
    def test_bash_redirection_operator_to_protected_path_is_protected(
        self, tmp_path, operator,
    ):
        from bobi.brain.claude_hooks import is_protected_agent_write

        command = f"echo patched {operator}.venv/log.txt"
        assert is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_read_only_command_against_protected_path_is_allowed(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        target = tmp_path / ".venv" / "lib" / "site.py"
        command = f"sed -n '1,20p' {target}"
        assert not is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_read_protected_file_redirecting_elsewhere_is_allowed(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        command = "cat .venv/lib/site.py > out.txt"
        assert not is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_read_protected_file_amp_redirecting_elsewhere_is_allowed(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        command = "cat .venv/lib/site.py &> out.txt"
        assert not is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_running_venv_python_is_allowed(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        python = tmp_path / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("")
        command = "./.venv/bin/python -m pytest tests/test_app.py"
        assert not is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    def test_bash_env_running_venv_python_with_comparison_is_allowed(self, tmp_path):
        from bobi.brain.claude_hooks import is_protected_agent_write

        command = 'env X=1 ./.venv/bin/python -c "print(1>0)"'
        assert not is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)

    @pytest.mark.parametrize("command", [
        "./.venv/bin/pip install sample-package",
        ".venv/bin/pip install sample-package",
        "./.venv/bin/python -m pip install sample-package",
    ])
    def test_bash_explicit_venv_package_install_is_protected(
        self, tmp_path, command,
    ):
        from bobi.brain.claude_hooks import is_protected_agent_write

        assert is_protected_agent_write("Bash", {"command": command}, cwd=tmp_path)


class TestDefaultHook:
    @pytest.mark.asyncio
    async def test_guard_hook_denies_protected_write_before_existing_hooks(self, tmp_path):
        from claude_agent_sdk import HookMatcher

        from bobi.brain.claude_hooks import make_default_pre_tool_use_hooks

        calls: list[str] = []

        async def existing_hook(input_data, tool_use_id, context):
            calls.append("existing")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }

        existing = {
            "PreToolUse": [HookMatcher(matcher="Write", hooks=[existing_hook])]
        }
        hooks = make_default_pre_tool_use_hooks(tmp_path, existing)
        assert hooks["PreToolUse"][0].matcher == "Write|Edit|MultiEdit|NotebookEdit|Bash"

        guard = hooks["PreToolUse"][0].hooks[0]
        result = await guard(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": str(tmp_path / ".venv" / "lib" / "patched.py")
                },
            },
            "tool-1",
            {},
        )

        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "source repo via PR" in output["permissionDecisionReason"]
        assert calls == []
        assert hooks["PreToolUse"][1:] == existing["PreToolUse"]

    @pytest.mark.asyncio
    async def test_guard_hook_allows_normal_workspace_write(self, tmp_path):
        from bobi.brain.claude_hooks import make_default_pre_tool_use_hooks

        hooks = make_default_pre_tool_use_hooks(tmp_path)
        guard = hooks["PreToolUse"][0].hooks[0]
        result = await guard(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "src" / "app.py")},
            },
            "tool-1",
            {},
        )

        assert result == {}


class TestClaudeBrainHookInjection:
    def test_make_session_adds_default_guard_hooks(self, tmp_path, monkeypatch):
        import claude_agent_sdk

        from bobi.brain.claude import ClaudeBrain

        monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", FakeClaudeAgentOptions)
        monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClaudeSDKClient)
        monkeypatch.setattr("bobi.sdk.get_cli_path", lambda: "/usr/bin/claude")

        existing_hook = MagicMock()
        existing = {"PreToolUse": [MagicMock(matcher="AskUserQuestion",
                                             hooks=[existing_hook])]}

        session = ClaudeBrain().make_session(
            cwd=str(tmp_path),
            system_prompt=None,
            options={"hooks": existing},
        )

        hooks = session._options.kwargs["hooks"]
        assert hooks["PreToolUse"][0].matcher == "Write|Edit|MultiEdit|NotebookEdit|Bash"
        assert hooks["PreToolUse"][1:] == existing["PreToolUse"]

    @pytest.mark.asyncio
    async def test_stream_once_adds_default_guard_hooks(self, tmp_path, monkeypatch):
        import claude_agent_sdk

        from bobi.brain.claude import ClaudeBrain

        captured: dict[str, Any] = {}

        async def fake_query(prompt, options):
            captured["options"] = options
            if False:
                yield None

        monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", FakeClaudeAgentOptions)
        monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
        monkeypatch.setattr("bobi.sdk.get_cli_path", lambda: "/usr/bin/claude")

        async for _ in ClaudeBrain().stream_once(
            system_prompt=None,
            user_prompt="hi",
            cwd=str(tmp_path),
        ):
            pass

        hooks = captured["options"].kwargs["hooks"]
        assert hooks["PreToolUse"][0].matcher == "Write|Edit|MultiEdit|NotebookEdit|Bash"
