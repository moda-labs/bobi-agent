"""Regression tests for the stdio MCP preflight bugs.

- MDS-63: a healthy stdio server caught mid-spawn in the ``pending`` window
  must not false-fail — the probe polls until it settles.
- MDS-64: a bare-name stdio command (PATH-resolved) earns a non-blocking ``⚠``
  warning in ``_check_mcp_servers``.
"""

import asyncio

import pytest

import modastack.validate as validate
from modastack.validate import _check_mcp_servers, _async_probe_mcp
from modastack.config import Config


class _FakeClient:
    """Fake ClaudeSDKClient: returns scripted get_mcp_status() snapshots."""

    def __init__(self, options=None):
        self.options = options
        self._snapshots = list(_FakeClient.SNAPSHOTS)
        self.connects = 0

    async def connect(self):
        self.connects += 1

    async def disconnect(self):
        pass

    async def get_mcp_status(self):
        # Return the next scripted snapshot; repeat the last one forever.
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


class _FakeBrainWithoutMcpStatus:
    name = "codex"
    provider = "openai"

    def make_session(self, **kwargs):
        class Session:
            async def connect(self):
                raise AssertionError("unsupported MCP probe should not connect")

            async def disconnect(self):
                raise AssertionError("unsupported MCP probe should not disconnect")

        return Session()


class _FakeClaudeBrain:
    name = "claude"
    provider = "anthropic"

    def make_session(self, **kwargs):
        return _FakeClient(kwargs.get("options"))


def _install_fake_client(monkeypatch, snapshots, interval=0.0):
    import modastack.brain

    _FakeClient.SNAPSHOTS = snapshots
    monkeypatch.setattr(modastack.brain, "get_brain", lambda: _FakeClaudeBrain())
    monkeypatch.setattr("modastack.sdk.get_cli_path", lambda: "claude")
    # Don't actually sleep between polls.
    monkeypatch.setattr(validate, "MCP_PROBE_POLL_INTERVAL", interval)


class TestPollRace:
    """MDS-63 — pending-then-connected must resolve to ok=True."""

    def test_pending_then_connected(self, monkeypatch, tmp_path):
        snapshots = [
            {"mcpServers": [{"name": "substack", "status": "pending"}]},
            {"mcpServers": [{"name": "substack", "status": "pending"}]},
            {"mcpServers": [{"name": "substack", "status": "connected",
                             "tools": ["a", "b", "c"]}]},
        ]
        _install_fake_client(monkeypatch, snapshots)

        results = asyncio.run(
            _async_probe_mcp(["substack"], {"substack": {"type": "stdio",
                             "command": "/abs/substack-mcp"}}, tmp_path)
        )
        assert len(results) == 1
        assert results[0].ok is True
        assert "3 tools" in results[0].detail

    def test_failed_settles_immediately(self, monkeypatch, tmp_path):
        snapshots = [
            {"mcpServers": [{"name": "x", "status": "failed",
                             "error": "boom"}]},
        ]
        _install_fake_client(monkeypatch, snapshots)
        results = asyncio.run(
            _async_probe_mcp(["x"], {"x": {"type": "stdio",
                             "command": "/abs/x"}}, tmp_path)
        )
        assert results[0].ok is False
        assert "boom" in results[0].detail

    def test_stuck_pending_times_out_without_hang(self, monkeypatch, tmp_path):
        snapshots = [
            {"mcpServers": [{"name": "slow", "status": "pending"}]},
        ]
        _install_fake_client(monkeypatch, snapshots)
        # Keep the poll budget tiny so the test is fast but still bounded.
        monkeypatch.setattr(validate, "MCP_PROBE_MAX_POLLS", 3)
        results = asyncio.run(
            _async_probe_mcp(["slow"], {"slow": {"type": "stdio",
                             "command": "/abs/slow"}}, tmp_path)
        )
        assert results[0].ok is False
        assert "pending" in results[0].detail

    def test_one_connect_for_many_servers(self, monkeypatch, tmp_path):
        """D-63b: a single connect()/poll judges every server."""
        snapshots = [
            {"mcpServers": [
                {"name": "a", "status": "connected", "tools": []},
                {"name": "b", "status": "connected", "tools": ["t"]},
            ]},
        ]
        _install_fake_client(monkeypatch, snapshots)
        created = []
        orig_init = _FakeClient.__init__

        def counting_init(self, options=None):
            created.append(1)
            orig_init(self, options)

        monkeypatch.setattr(_FakeClient, "__init__", counting_init)
        results = asyncio.run(
            _async_probe_mcp(["a", "b"], {
                "a": {"type": "stdio", "command": "/abs/a"},
                "b": {"type": "stdio", "command": "/abs/b"},
            }, tmp_path)
        )
        assert len(results) == 2
        assert {r.name for r in results} == {"a", "b"}
        assert sum(created) == 1  # one client, not one-per-server

    def test_brain_without_mcp_status_warns_without_blocking(
        self, monkeypatch, tmp_path
    ):
        import modastack.brain

        monkeypatch.setattr(
            modastack.brain, "get_brain",
            lambda: _FakeBrainWithoutMcpStatus(),
        )
        results = asyncio.run(
            _async_probe_mcp(["x"], {"x": {"type": "stdio",
                             "command": "/abs/x"}}, tmp_path)
        )
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].required is False
        assert "not supported for codex brain" in results[0].detail


class TestBareNameWarning:
    """MDS-64 — D-64c non-blocking warning on bare-name stdio commands."""

    def test_bare_name_warns_non_blocking(self, monkeypatch, tmp_path):
        # Stub the probe so the test exercises only the warning logic.
        monkeypatch.setattr(
            validate, "_probe_mcp_servers",
            lambda names, servers, p: [validate.CheckResult(n, ok=True,
                                       detail="mcp, 1 tools") for n in names],
        )
        cfg = Config(mcp_servers={"sub": {"type": "stdio",
                                          "command": "substack-mcp"}})
        checks = _check_mcp_servers(cfg, tmp_path)
        warnings = [c for c in checks if not c.ok and not c.required]
        assert len(warnings) == 1
        assert warnings[0].name == "sub"
        assert "substack-mcp" in warnings[0].detail

    def test_absolute_command_no_warning(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            validate, "_probe_mcp_servers",
            lambda names, servers, p: [validate.CheckResult(n, ok=True,
                                       detail="mcp, 1 tools") for n in names],
        )
        cfg = Config(mcp_servers={"sub": {"type": "stdio",
                                          "command": "/usr/local/bin/substack-mcp"}})
        checks = _check_mcp_servers(cfg, tmp_path)
        assert all(c.required or c.ok for c in checks)  # no non-blocking warning
