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

from bobi import paths


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
        config_dir = paths.package_dir(root)
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "agent: eng-team\nbrain:\n  kind: codex\n  model: gpt-5-codex\n"
        )
        monkeypatch.setenv("BOBI_ROOT", "/stale/root")
        monkeypatch.setenv("BOBI_BRAIN", "claude")
        monkeypatch.setenv("BOBI_BRAIN_MODEL", "opus")

        env = child_agent_env(root)

        assert env["BOBI_ROOT"] == str(root)
        assert env["BOBI_BRAIN"] == "codex"
        assert env["BOBI_BRAIN_MODEL"] == "gpt-5-codex"

    def test_clears_stale_parent_brain_for_default_brain_team(
        self, tmp_path, monkeypatch,
    ):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = paths.package_dir(root)
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text("agent: eng-team\n")
        monkeypatch.setenv("BOBI_BRAIN", "codex")
        monkeypatch.setenv("BOBI_BRAIN_MODEL", "gpt-5-codex")

        env = child_agent_env(root)

        assert "BOBI_BRAIN" not in env
        assert "BOBI_BRAIN_MODEL" not in env

    def test_interpolates_brain_config_from_dotenv(self, tmp_path, monkeypatch):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = paths.package_dir(root)
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "agent: eng-team\nbrain:\n  kind: ${TEAM_BRAIN}\n  model: ${TEAM_MODEL}\n"
        )
        paths.env_path(root).write_text("TEAM_BRAIN=codex\nTEAM_MODEL=haiku\n")
        monkeypatch.delenv("TEAM_BRAIN", raising=False)
        monkeypatch.delenv("TEAM_MODEL", raising=False)

        env = child_agent_env(root)

        assert env["TEAM_BRAIN"] == "codex"
        assert env["TEAM_MODEL"] == "haiku"
        assert env["BOBI_BRAIN"] == "codex"
        assert env["BOBI_BRAIN_MODEL"] == "haiku"

    def test_carries_parent_tool_and_credential_environment(self, tmp_path, monkeypatch):
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        paths.package_dir(root).mkdir(parents=True)
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
        config_dir = paths.package_dir(root)
        config_dir.mkdir(parents=True)
        paths.env_path(root).write_text(
            "OPENAI_API_KEY=from-file\n"
            "VENN_API_KEY=from-file\n"
        )
        monkeypatch.setenv("OPENAI_API_KEY", "from-parent")
        monkeypatch.delenv("VENN_API_KEY", raising=False)

        env = child_agent_env(root)

        assert env["OPENAI_API_KEY"] == "from-parent"
        assert env["VENN_API_KEY"] == "from-file"
        assert os.environ.get("VENN_API_KEY") is None

    def test_child_env_replaces_dotenv_value_loaded_by_another_runtime(
        self, tmp_path, monkeypatch,
    ):
        from bobi.config import Config
        from bobi.env import child_agent_env

        monkeypatch.delenv("SHARED_CHILD_TOKEN", raising=False)
        first = tmp_path / "first"
        second = tmp_path / "second"
        for root, token in [(first, "first-token"), (second, "second-token")]:
            paths.package_dir(root).mkdir(parents=True)
            paths.agent_yaml_path(root).write_text(
                "services:\n"
                "  - name: slack\n"
                "    credentials:\n"
                "      bot_token: ${SHARED_CHILD_TOKEN}\n"
            )
            paths.env_path(root).write_text(f"SHARED_CHILD_TOKEN={token}\n")

        assert Config.load(first).credential("slack", "bot_token") == "first-token"

        env = child_agent_env(second)

        assert env["SHARED_CHILD_TOKEN"] == "second-token"

    def test_pins_gateway_config_with_dotenv_interpolation(
        self, tmp_path, monkeypatch,
    ):
        """A `kind: gateway` team pins base_url/small_model for its sessions
        (#655), with `${VAR}` resolved from the runtime .env."""
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = paths.package_dir(root)
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "agent: local-team\n"
            "brain:\n"
            "  kind: gateway\n"
            "  base_url: ${LLM_GATEWAY_URL}\n"
            "  model: qwen3:14b\n"
            "  small_model: qwen3:4b\n"
        )
        paths.env_path(root).write_text("LLM_GATEWAY_URL=http://localhost:4000\n")
        monkeypatch.delenv("LLM_GATEWAY_URL", raising=False)

        env = child_agent_env(root)

        assert env["BOBI_BRAIN"] == "gateway"
        assert env["BOBI_BRAIN_MODEL"] == "qwen3:14b"
        assert env["BOBI_GATEWAY_BASE_URL"] == "http://localhost:4000"
        assert env["BOBI_GATEWAY_SMALL_MODEL"] == "qwen3:4b"

    def test_pins_gateway_openai_config_with_dotenv_interpolation(
        self, tmp_path, monkeypatch,
    ):
        """A `kind: gateway-openai` team pins base_url/wire_api for Codex."""
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = paths.package_dir(root)
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "agent: local-team\n"
            "brain:\n"
            "  kind: gateway-openai\n"
            "  base_url: ${LLM_GATEWAY_URL}\n"
            "  model: gpt-5.5\n"
            "  small_model: should-not-pin\n"
        )
        paths.env_path(root).write_text("LLM_GATEWAY_URL=http://localhost:9000/v1\n")
        monkeypatch.delenv("LLM_GATEWAY_URL", raising=False)

        env = child_agent_env(root)

        assert env["BOBI_BRAIN"] == "gateway-openai"
        assert env["BOBI_BRAIN_MODEL"] == "gpt-5.5"
        assert env["BOBI_GATEWAY_BASE_URL"] == "http://localhost:9000/v1"
        assert env["BOBI_GATEWAY_WIRE_API"] == "responses"
        assert "BOBI_GATEWAY_SMALL_MODEL" not in env

    def test_brain_interpolation_matches_config_semantics(
        self, tmp_path, monkeypatch,
    ):
        """`${VAR:-default}` must resolve the same here as in Config.load -
        a divergence passes validate yet pins an empty gateway base URL into
        every child (#655 review finding)."""
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = paths.package_dir(root)
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "agent: local-team\n"
            "brain:\n"
            "  kind: gateway\n"
            "  base_url: ${UNSET_GATEWAY_URL:-http://localhost:11434}\n"
            "  model: qwen3:14b\n"
        )
        monkeypatch.delenv("UNSET_GATEWAY_URL", raising=False)

        env = child_agent_env(root)

        assert env["BOBI_GATEWAY_BASE_URL"] == "http://localhost:11434"

    def test_clears_stale_parent_gateway_pins_for_other_team(
        self, tmp_path, monkeypatch,
    ):
        """A stale gateway endpoint from another installation must never leak
        into a claude/default team's sessions."""
        from bobi.env import child_agent_env

        root = tmp_path / "install"
        config_dir = paths.package_dir(root)
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text("agent: eng-team\n")
        monkeypatch.setenv("BOBI_GATEWAY_BASE_URL", "http://stale:4000")
        monkeypatch.setenv("BOBI_GATEWAY_SMALL_MODEL", "stale-model")
        monkeypatch.setenv("BOBI_GATEWAY_WIRE_API", "responses")

        env = child_agent_env(root)

        assert "BOBI_GATEWAY_BASE_URL" not in env
        assert "BOBI_GATEWAY_SMALL_MODEL" not in env
        assert "BOBI_GATEWAY_WIRE_API" not in env

    def test_uses_same_path_normalization_as_spawn_env(self, tmp_path, monkeypatch):
        from bobi.env import agent_spawn_env, child_agent_env

        root = tmp_path / "install"
        paths.package_dir(root).mkdir(parents=True)
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

        assert validate_mod.child_agent_env is env_mod.child_agent_env
        assert subagent_mod.agent_spawn_env is env_mod.agent_spawn_env


class TestBrainPinReadsThroughConfig:
    """Q034: the spawn path expands brain config through Config's properties.

    ``pin_brain_from_root`` used to re-encode two things ``bobi.config``
    already defined - the ``wire_api`` default and the presence-based gateway
    declaration - so a change to either had to be made twice or the spawned
    child pinned something the manager did not.
    """

    def _install(self, tmp_path, brain_yaml: str):
        root = tmp_path / "install"
        paths.package_dir(root).mkdir(parents=True)
        paths.agent_yaml_path(root).write_text(brain_yaml)
        return root

    def test_wire_api_default_comes_from_config(self, tmp_path, monkeypatch):
        """Move Config's default, and the pin moves with it."""
        from bobi.config import Config
        from bobi.env import pin_brain_from_root

        root = self._install(
            tmp_path, "brain:\n  kind: codex\n  base_url: http://gw\n")

        env = {}
        pin_brain_from_root(root, env)
        assert env["BOBI_GATEWAY_WIRE_API"] == "responses"

        monkeypatch.setattr(
            Config, "brain_wire_api",
            property(lambda self: "moved-with-the-property"))
        env = {}
        pin_brain_from_root(root, env)
        assert env["BOBI_GATEWAY_WIRE_API"] == "moved-with-the-property"

    def test_gateway_declaration_comes_from_config(self, tmp_path, monkeypatch):
        """A base_url key that resolved empty fails the spawn loud.

        The predicate is Config.brain_is_gateway; flipping it flips the pin,
        which is what proves the spawn path is not carrying its own copy.
        """
        from bobi.config import Config
        from bobi.env import pin_brain_from_root

        root = self._install(
            tmp_path, "brain:\n  kind: codex\n  base_url: ${MISSING_GW:-}\n")

        with pytest.raises(RuntimeError, match="base_url"):
            pin_brain_from_root(root, {})

        monkeypatch.setattr(
            Config, "brain_is_gateway", property(lambda self: False))
        env = {}
        pin_brain_from_root(root, env)  # no declaration -> no fail-loud
        assert not env.get("BOBI_GATEWAY_BASE_URL")

    def test_alias_kind_still_declares_a_gateway(self, tmp_path):
        """Config.brain_is_gateway also covers the alias spellings."""
        from bobi.env import pin_brain_from_root

        root = self._install(tmp_path, "brain:\n  kind: gateway\n")

        with pytest.raises(RuntimeError, match="base_url"):
            pin_brain_from_root(root, {})

    def test_broken_sibling_section_still_pins_the_brain(self, tmp_path):
        """Why this reads only `brain:` rather than running Config._parse.

        This is the SPAWN path. ``Config._parse`` raises on a malformed
        ``services:``/``requires:`` block, which would turn a broken-but-live
        team's gateway pin into a silent native session dialing the real
        vendor with gateway credentials - the #789 leak.
        """
        import pytest as _pytest

        from bobi.config import Config
        from bobi.env import pin_brain_from_root

        root = self._install(
            tmp_path,
            "brain:\n  kind: codex\n  base_url: http://gw\nservices: 7\n")

        with _pytest.raises(TypeError):
            Config._parse(paths.agent_yaml_path(root), env={})

        env = {}
        pin_brain_from_root(root, env)
        assert env["BOBI_GATEWAY_BASE_URL"] == "http://gw"

    def test_startup_and_spawn_pin_identically(self, tmp_path, monkeypatch):
        """The two entry points must expand the same cfg the same way.

        A new ``brain.*`` field threaded into one and missed in the other is
        the drift this consolidation exists to prevent, so compare the pins
        the startup path writes to os.environ against the ones the spawn path
        writes into a child env.
        """
        import os

        from bobi.brain import set_process_brain_from_config
        from bobi.config import Config
        from bobi.env import pin_brain_from_root

        yaml = ("brain:\n  kind: codex\n  model: gpt-5.6\n  effort: high\n"
                "  base_url: http://gw\n  small_model: mini\n"
                "  wire_api: chat\n")
        root = self._install(tmp_path, yaml)
        cfg = Config._parse(paths.agent_yaml_path(root), env={})

        pins = ("BOBI_BRAIN", "BOBI_BRAIN_MODEL", "BOBI_BRAIN_EFFORT",
                "BOBI_GATEWAY_BASE_URL", "BOBI_GATEWAY_SMALL_MODEL",
                "BOBI_GATEWAY_WIRE_API")
        for var in pins:
            monkeypatch.delenv(var, raising=False)

        spawn_env = {}
        pin_brain_from_root(root, spawn_env)
        set_process_brain_from_config(cfg)

        startup = {v: os.environ.get(v) for v in pins}
        assert startup == {v: spawn_env.get(v) for v in pins}
        # and the values are the configured ones, not silently empty
        assert startup["BOBI_GATEWAY_WIRE_API"] == "chat"
        assert startup["BOBI_GATEWAY_BASE_URL"] == "http://gw"
