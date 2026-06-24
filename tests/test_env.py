"""Tests for the shared agent-spawn environment helper (MDS-64).

The whole point of ``agent_spawn_env()`` is that MCP preflight (validate.py)
and the actual agent spawn (subagent.py) build ``PATH`` identically, so a
bare-name stdio command (e.g. ``substack-mcp`` from ``uv tool install`` into
``~/.local/bin``) can never be green at preflight and broken at runtime.
"""

import os
import shutil
from pathlib import Path

import pytest


class TestAgentSpawnEnv:
    def test_prepends_local_bin_under_stripped_path(self, monkeypatch):
        from modastack.env import agent_spawn_env

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.delenv("XDG_BIN_HOME", raising=False)

        env = agent_spawn_env()
        local_bin = str(Path.home() / ".local" / "bin")
        parts = env["PATH"].split(os.pathsep)
        assert local_bin in parts
        # user-bin must win over system dirs → appear before them
        assert parts.index(local_bin) < parts.index("/usr/bin")

    def test_bare_command_resolves_through_returned_path(self, monkeypatch, tmp_path):
        """A bare name placed in the user-bin dir resolves via the helper's PATH
        even when the inherited PATH (the daemon's) does not contain it."""
        from modastack.env import agent_spawn_env

        user_bin = tmp_path / ".local" / "bin"
        user_bin.mkdir(parents=True)
        exe = user_bin / "substack-mcp"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.delenv("XDG_BIN_HOME", raising=False)

        env = agent_spawn_env()
        # Daemon-like inherited PATH cannot find it; the spawn env can.
        assert shutil.which("substack-mcp", path="/usr/bin:/bin") is None
        assert shutil.which("substack-mcp", path=env["PATH"]) == str(exe)

    def test_includes_xdg_bin_home_when_set(self, monkeypatch, tmp_path):
        from modastack.env import agent_spawn_env

        xdg = tmp_path / "xdgbin"
        xdg.mkdir()
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("XDG_BIN_HOME", str(xdg))

        env = agent_spawn_env()
        assert str(xdg) in env["PATH"].split(os.pathsep)

    def test_preserves_existing_path_entries(self, monkeypatch):
        from modastack.env import agent_spawn_env

        monkeypatch.setenv("PATH", "/opt/custom/bin:/usr/bin")
        env = agent_spawn_env()
        parts = env["PATH"].split(os.pathsep)
        assert "/opt/custom/bin" in parts
        assert "/usr/bin" in parts

    def test_no_duplicate_path_entries(self, monkeypatch):
        from modastack.env import agent_spawn_env

        local_bin = str(Path.home() / ".local" / "bin")
        # local_bin already present in inherited PATH → must not be duplicated.
        monkeypatch.setenv("PATH", f"{local_bin}:/usr/bin")
        monkeypatch.delenv("XDG_BIN_HOME", raising=False)
        env = agent_spawn_env()
        parts = env["PATH"].split(os.pathsep)
        assert parts.count(local_bin) == 1

    def test_carries_other_env_vars(self, monkeypatch):
        from modastack.env import agent_spawn_env

        monkeypatch.setenv("SOME_TOKEN", "abc123")
        env = agent_spawn_env()
        assert env["SOME_TOKEN"] == "abc123"


class TestProbeAndSpawnUseSameHelper:
    """Preflight and runtime must wire the *same* helper so they can't diverge."""

    def test_validate_and_subagent_share_helper(self):
        import modastack.env as env_mod
        import modastack.validate as validate_mod
        import modastack.subagent as subagent_mod

        assert validate_mod.agent_spawn_env is env_mod.agent_spawn_env
        assert subagent_mod.agent_spawn_env is env_mod.agent_spawn_env
