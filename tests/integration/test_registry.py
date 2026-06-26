"""Integration tests for the agent team registry.

Exercises install-from-registry, update checking, cache listing,
and multi-registry resolution against an isolated bobi install.
Network calls are stubbed to avoid GitHub dependency in CI.
"""

import tarfile
from io import BytesIO
from unittest.mock import patch

import httpx
import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_tarball(name: str, agent_yaml: dict, extra_files: dict[str, str] | None = None) -> bytes:
    """Build an in-memory .tar.gz that mimics a GitHub repo tarball.

    Structure: <prefix>/agents/<name>/agent.yaml (+ any extras).
    """
    buf = BytesIO()
    prefix = "moda-labs-bobi-abc1234"

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # agent.yaml
        content = yaml.dump(agent_yaml).encode()
        info = tarfile.TarInfo(name=f"{prefix}/agents/{name}/agent.yaml")
        info.size = len(content)
        tar.addfile(info, BytesIO(content))

        # role stubs
        for role in agent_yaml.get("roles", ["default"]):
            role_content = f"# {role}\n".encode()
            role_path = f"{prefix}/agents/{name}/roles/{role}/ROLE.md"
            info = tarfile.TarInfo(name=role_path)
            info.size = len(role_content)
            tar.addfile(info, BytesIO(role_content))

        # extra files
        for rel_path, text in (extra_files or {}).items():
            data = text.encode()
            info = tarfile.TarInfo(name=f"{prefix}/agents/{name}/{rel_path}")
            info.size = len(data)
            tar.addfile(info, BytesIO(data))

    return buf.getvalue()


def _build_registry_yaml(agents: dict[str, dict]) -> str:
    return yaml.dump({"agents": agents})


def _mock_urlopen(responses: dict[str, bytes | str | None]):
    """Return a patched _urlopen that maps URL substrings to canned responses."""
    def _fake_urlopen(url: str, timeout: int = 10) -> httpx.Response:
        for pattern, body in responses.items():
            if pattern in url:
                if body is None:
                    raise httpx.HTTPStatusError(
                        "Not Found",
                        request=httpx.Request("GET", url),
                        response=httpx.Response(404),
                    )
                raw = body if isinstance(body, bytes) else body.encode()
                return httpx.Response(200, content=raw)
        raise httpx.HTTPStatusError(
            "Not Found",
            request=httpx.Request("GET", url),
            response=httpx.Response(404),
        )
    return _fake_urlopen


# ---------------------------------------------------------------------------
# Tests: fetch + install
# ---------------------------------------------------------------------------

class TestRegistryFetch:
    """Fetch an agent team from a (stubbed) remote registry."""

    def test_fetch_installs_to_cache(self, bobi_env):
        from bobi.registry import fetch, is_cached, _read_meta

        agent_cfg = {
            "version": "1.0.0",
            "agent": "test-team",
            "entry_point": "manager",
            "roles": ["manager", "engineer"],
        }
        tarball = _build_tarball("test-team", agent_cfg)
        registry_yaml = _build_registry_yaml({
            "test-team": {"description": "A test team", "version": "1.0.0"},
        })

        responses = {
            "registry.yaml": registry_yaml,
            "agents/test-team/agent.yaml": yaml.dump(agent_cfg),
            "tarball": tarball,
        }

        project = bobi_env.project_path
        with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
            dest = fetch(project, "test-team", repo="moda-labs/bobi")

        assert dest.exists()
        assert (dest / "agent.yaml").exists()
        assert is_cached(project, "test-team")

        meta = _read_meta(project, "test-team")
        assert meta["version"] == "1.0.0"
        assert meta["source"] == "github:moda-labs/bobi"

    def test_fetch_not_found_raises(self, bobi_env):
        from bobi.registry import fetch

        responses = {
            "registry.yaml": _build_registry_yaml({}),
            "agent.yaml": None,
            "tarball": None,
        }

        project = bobi_env.project_path
        with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
            with pytest.raises(RuntimeError, match="not found"):
                fetch(project, "nonexistent", repo="moda-labs/bobi")

    def test_fetch_overwrites_existing(self, bobi_env):
        """Re-fetching replaces the cached version."""
        from bobi.registry import fetch, _read_meta

        project = bobi_env.project_path

        for version in ("1.0.0", "2.0.0"):
            agent_cfg = {
                "version": version,
                "agent": "evolving-team",
                "entry_point": "manager",
            }
            tarball = _build_tarball("evolving-team", agent_cfg)
            responses = {
                "agent.yaml": yaml.dump(agent_cfg),
                "tarball": tarball,
            }
            with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
                fetch(project, "evolving-team", repo="moda-labs/bobi")

        meta = _read_meta(project, "evolving-team")
        assert meta["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# Tests: version checking
# ---------------------------------------------------------------------------

class TestCheckUpdate:
    """Compare local vs remote versions."""

    def test_detects_available_update(self, bobi_env):
        from bobi.registry import fetch, check_update

        project = bobi_env.project_path

        # Install v1
        agent_cfg = {"version": "1.0.0", "agent": "upd-team", "entry_point": "mgr"}
        tarball = _build_tarball("upd-team", agent_cfg)
        responses = {
            "agent.yaml": yaml.dump(agent_cfg),
            "tarball": tarball,
        }
        with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
            fetch(project, "upd-team", repo="moda-labs/bobi")

        # Remote reports v2
        remote_cfg = {"version": "2.0.0", "agent": "upd-team", "entry_point": "mgr"}
        responses = {"agent.yaml": yaml.dump(remote_cfg)}
        with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
            local_v, remote_v = check_update(project, "upd-team", repo="moda-labs/bobi")

        assert local_v == "1.0.0"
        assert remote_v == "2.0.0"

    def test_up_to_date(self, bobi_env):
        from bobi.registry import fetch, check_update

        project = bobi_env.project_path
        agent_cfg = {"version": "1.0.0", "agent": "same-team", "entry_point": "mgr"}
        tarball = _build_tarball("same-team", agent_cfg)
        responses = {
            "agent.yaml": yaml.dump(agent_cfg),
            "tarball": tarball,
        }
        with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
            fetch(project, "same-team", repo="moda-labs/bobi")
            local_v, remote_v = check_update(project, "same-team", repo="moda-labs/bobi")

        assert local_v == "1.0.0"
        assert remote_v == "1.0.0"


# ---------------------------------------------------------------------------
# Tests: list cached
# ---------------------------------------------------------------------------

class TestListCached:

    def test_lists_installed_packs(self, bobi_env):
        from bobi.registry import fetch, list_cached

        project = bobi_env.project_path
        for name in ("alpha-team", "beta-team"):
            agent_cfg = {"version": "1.0.0", "agent": name, "entry_point": "mgr"}
            tarball = _build_tarball(name, agent_cfg)
            responses = {
                "agent.yaml": yaml.dump(agent_cfg),
                "tarball": tarball,
            }
            with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
                fetch(project, name, repo="moda-labs/bobi")

        cached = list_cached(project)
        names = {p["name"] for p in cached}
        assert "alpha-team" in names
        assert "beta-team" in names

    def test_empty_cache(self, tmp_path):
        from bobi.registry import list_cached
        assert list_cached(tmp_path) == []


# ---------------------------------------------------------------------------
# Tests: list remote (browse)
# ---------------------------------------------------------------------------

class TestListRemote:

    def test_lists_remote_packs(self, bobi_env):
        from bobi.registry import list_remote

        registry_data = _build_registry_yaml({
            "eng-team": {"description": "Engineering team", "version": "2.0.0"},
            "ops-team": {"description": "Operations team", "version": "1.5.0"},
        })
        responses = {"registry.yaml": registry_data}

        with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
            packs = list_remote(repo="moda-labs/bobi")

        names = {p["name"] for p in packs}
        assert names == {"eng-team", "ops-team"}

    def test_empty_registry(self):
        from bobi.registry import list_remote

        responses = {"registry.yaml": yaml.dump({})}
        with patch("bobi.registry._urlopen", side_effect=_mock_urlopen(responses)):
            assert list_remote(repo="moda-labs/bobi") == []


# ---------------------------------------------------------------------------
# Tests: multi-registry (add-registry)
# ---------------------------------------------------------------------------

class TestMultiRegistry:

    def test_all_registries_includes_user_added(self, bobi_env):
        from bobi.registry import _all_registries

        # Add a custom registry to the config
        config_path = bobi_env.project_path / ".bobi" / "agent.yaml"
        data = yaml.safe_load(config_path.read_text())
        data["registries"] = ["myorg/my-agents"]
        config_path.write_text(yaml.dump(data))

        registries = _all_registries(bobi_env.project_path)
        assert "moda-labs/bobi" in registries  # default
        assert "myorg/my-agents" in registries

    def test_registries_deduplicates(self, bobi_env):
        from bobi.registry import _all_registries

        config_path = bobi_env.project_path / ".bobi" / "agent.yaml"
        data = yaml.safe_load(config_path.read_text())
        data["registries"] = ["moda-labs/bobi"]  # duplicate of default
        config_path.write_text(yaml.dump(data))

        registries = _all_registries(bobi_env.project_path)
        assert registries.count("moda-labs/bobi") == 1
