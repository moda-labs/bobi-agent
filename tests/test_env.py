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
        from bobi.env import agent_spawn_env

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
        from bobi.env import agent_spawn_env

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
        from bobi.env import agent_spawn_env

        xdg = tmp_path / "xdgbin"
        xdg.mkdir()
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("XDG_BIN_HOME", str(xdg))

        env = agent_spawn_env()
        assert str(xdg) in env["PATH"].split(os.pathsep)

    def test_preserves_existing_path_entries(self, monkeypatch):
        from bobi.env import agent_spawn_env

        monkeypatch.setenv("PATH", "/opt/custom/bin:/usr/bin")
        env = agent_spawn_env()
        parts = env["PATH"].split(os.pathsep)
        assert "/opt/custom/bin" in parts
        assert "/usr/bin" in parts

    def test_no_duplicate_path_entries(self, monkeypatch):
        from bobi.env import agent_spawn_env

        local_bin = str(Path.home() / ".local" / "bin")
        # local_bin already present in inherited PATH → must not be duplicated.
        monkeypatch.setenv("PATH", f"{local_bin}:/usr/bin")
        monkeypatch.delenv("XDG_BIN_HOME", raising=False)
        env = agent_spawn_env()
        parts = env["PATH"].split(os.pathsep)
        assert parts.count(local_bin) == 1

    def test_carries_other_env_vars(self, monkeypatch):
        from bobi.env import agent_spawn_env

        monkeypatch.setenv("SOME_TOKEN", "abc123")
        env = agent_spawn_env()
        assert env["SOME_TOKEN"] == "abc123"


class TestChildAgentEnv:
    def test_pins_root_and_overrides_stale_parent_brain(self, tmp_path, monkeypatch):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = root / ".bobi"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "agent: eng-team\nbrain:\n  kind: codex\n"
        )
        monkeypatch.setenv("BOBI_ROOT", "/stale/root")
        monkeypatch.setenv("BOBI_BRAIN", "claude")

        env = child_agent_env(root)

        assert env["BOBI_ROOT"] == str(root)
        assert env["BOBI_BRAIN"] == "codex"

    def test_clears_stale_parent_brain_for_default_brain_team(
        self, tmp_path, monkeypatch,
    ):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = root / ".bobi"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text("agent: eng-team\n")
        monkeypatch.setenv("BOBI_BRAIN", "codex")

        env = child_agent_env(root)

        assert "BOBI_BRAIN" not in env

    def test_interpolates_brain_kind_from_dotenv(self, tmp_path, monkeypatch):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = root / ".bobi"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "agent: eng-team\nbrain:\n  kind: ${TEAM_BRAIN}\n"
        )
        (config_dir / ".env").write_text("TEAM_BRAIN=codex\n")
        monkeypatch.delenv("TEAM_BRAIN", raising=False)

        env = child_agent_env(root)

        assert env["TEAM_BRAIN"] == "codex"
        assert env["BOBI_BRAIN"] == "codex"

    def test_legacy_modastack_root_loads_dotenv_and_brain_kind(
        self, tmp_path, monkeypatch,
    ):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = root / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "agent: eng-team\nbrain:\n  kind: ${TEAM_BRAIN}\n"
        )
        (config_dir / ".env").write_text(
            "TEAM_BRAIN=codex\n"
            "VENN_API_KEY=from-legacy-file\n"
        )
        monkeypatch.delenv("TEAM_BRAIN", raising=False)
        monkeypatch.delenv("VENN_API_KEY", raising=False)

        env = child_agent_env(root)

        assert env["TEAM_BRAIN"] == "codex"
        assert env["BOBI_BRAIN"] == "codex"
        assert env["VENN_API_KEY"] == "from-legacy-file"

    def test_carries_parent_tool_and_credential_environment(self, tmp_path, monkeypatch):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        (root / ".bobi").mkdir(parents=True)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("VENN_API_KEY", "venn-key")
        monkeypatch.setenv("GH_TOKEN", "gh-token")

        env = child_agent_env(root)

        assert env["OPENAI_API_KEY"] == "sk-openai"
        assert env["VENN_API_KEY"] == "venn-key"
        assert env["GH_TOKEN"] == "gh-token"

    def test_loads_dotenv_credentials_without_overriding_parent_env(
        self, tmp_path, monkeypatch,
    ):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = root / ".bobi"
        config_dir.mkdir(parents=True)
        (config_dir / ".env").write_text(
            "OPENAI_API_KEY=from-file\n"
            "VENN_API_KEY=from-file\n"
        )
        monkeypatch.setenv("OPENAI_API_KEY", "from-parent")
        monkeypatch.delenv("VENN_API_KEY", raising=False)

        env = child_agent_env(root)

        assert env["OPENAI_API_KEY"] == "from-parent"
        assert env["VENN_API_KEY"] == "from-file"
        assert os.environ.get("VENN_API_KEY") is None

    def test_uses_same_path_normalization_as_spawn_env(self, tmp_path, monkeypatch):
        from bobi.env import agent_spawn_env, child_agent_env

        root = tmp_path / "install"
        (root / ".bobi").mkdir(parents=True)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        spawn_env = agent_spawn_env()
        child_env = child_agent_env(root)

        assert child_env["PATH"] == spawn_env["PATH"]


class TestProbeAndSpawnUseSameHelper:
    """Preflight and runtime must wire the *same* helper so they can't diverge."""

    def test_validate_and_subagent_share_helper(self):
        import bobi.env as env_mod
        import bobi.validate as validate_mod
        import bobi.subagent as subagent_mod

        assert validate_mod.agent_spawn_env is env_mod.agent_spawn_env
        assert subagent_mod.agent_spawn_env is env_mod.agent_spawn_env
