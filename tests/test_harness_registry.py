"""Tests for the harness registry in modastack.mcp.inject."""

import pytest

from modastack.config import ConnectionEntry
from modastack.mcp.inject import (
    HarnessSpec,
    get_registry,
    inject_builtin_mcp_servers,
    register_harness,
    _HARNESS_REGISTRY,
)


class TestHarnessRegistry:
    def test_default_registry_has_three_entries(self):
        specs = get_registry()
        names = {s.name for s in specs}
        assert "modastack-image" in names
        assert "modastack-codex" in names
        assert "modastack-gateway" in names

    def test_get_registry_returns_copy(self):
        a = get_registry()
        b = get_registry()
        assert a is not b

    def test_register_new_harness(self):
        spec = HarnessSpec(name="modastack-test", kind="test",
                           module="modastack.mcp.test_server")
        original_len = len(_HARNESS_REGISTRY)
        try:
            register_harness(spec)
            assert any(s.name == "modastack-test" for s in _HARNESS_REGISTRY)
        finally:
            # Cleanup
            _HARNESS_REGISTRY[:] = [s for s in _HARNESS_REGISTRY
                                    if s.name != "modastack-test"]
            assert len(_HARNESS_REGISTRY) == original_len

    def test_register_replaces_existing(self):
        original = [s for s in _HARNESS_REGISTRY if s.name == "modastack-image"][0]
        replacement = HarnessSpec(name="modastack-image", kind="image",
                                  module="modastack.mcp.custom_image")
        try:
            register_harness(replacement)
            found = [s for s in _HARNESS_REGISTRY if s.name == "modastack-image"]
            assert len(found) == 1
            assert found[0].module == "modastack.mcp.custom_image"
        finally:
            # Restore original
            register_harness(original)

    def test_spec_frozen(self):
        spec = HarnessSpec(name="x", kind="y", module="z")
        with pytest.raises(AttributeError):
            spec.name = "changed"  # type: ignore[misc]


class TestRegistryDrivenInjection:
    def test_all_kinds_injected(self):
        connections = [
            ConnectionEntry(name="img", kind="image", provider="openai",
                            api_key="sk-1"),
            ConnectionEntry(name="cx", kind="codex", provider="openai",
                            api_key="sk-2"),
            ConnectionEntry(name="gw", kind="gateway", provider="openai",
                            api_key="sk-3"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-image" in result
        assert "modastack-codex" in result
        assert "modastack-gateway" in result

    def test_only_matching_kinds_injected(self):
        connections = [
            ConnectionEntry(name="gw", kind="gateway", provider="openai",
                            api_key="sk-1"),
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-gateway" in result
        assert "modastack-image" not in result
        assert "modastack-codex" not in result

    def test_user_override_not_clobbered(self):
        connections = [
            ConnectionEntry(name="gw", kind="gateway", provider="openai",
                            api_key="sk-1"),
        ]
        existing = {"modastack-gateway": {"type": "stdio", "command": "custom"}}
        result = inject_builtin_mcp_servers(existing, connections)
        assert result["modastack-gateway"]["command"] == "custom"

    def test_empty_connections(self):
        result = inject_builtin_mcp_servers(None, [])
        assert result == {}

    def test_dict_connections_supported(self):
        connections = [
            {"name": "gw", "kind": "gateway", "provider": "openai",
             "api_key": "sk-1"},
        ]
        result = inject_builtin_mcp_servers(None, connections)
        assert "modastack-gateway" in result
