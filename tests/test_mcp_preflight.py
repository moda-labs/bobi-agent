"""Regression tests for the stdio MCP preflight bugs.

- MDS-63: a healthy stdio server caught mid-spawn in the ``pending`` window
  must not false-fail — the probe polls until it settles.
- MDS-64: a bare-name stdio command (PATH-resolved) earns a non-blocking ``⚠``
  warning in ``_check_mcp_servers``.
"""

import asyncio

import pytest

import bobi.validate as validate
from bobi.validate import _check_mcp_servers, _async_probe_mcp
from bobi.config import Config


class _FakeClient:
    """Fake ClaudeSDKClient: returns scripted get_mcp_status() snapshots."""
    HANG_CONNECT = False
    HANG_STATUS = False

    def __init__(self, options=None):
        self.options = options
        self._snapshots = list(_FakeClient.SNAPSHOTS)
        self.connects = 0

    async def connect(self):
        if _FakeClient.HANG_CONNECT:
            await asyncio.sleep(10)
        self.connects += 1

    async def disconnect(self):
        pass

    async def get_mcp_status(self):
        if _FakeClient.HANG_STATUS:
            await asyncio.sleep(10)
        # Return the next scripted snapshot; repeat the last one forever.
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


class _FakeBrainWithoutMcpStatus:
    """A hypothetical brain that exposes no MCP status introspection at all —
    the generic fallback path. (Both shipped brains support it: Claude natively,
    Codex via a direct handshake, see the codex test below.)"""

    name = "future"
    provider = "future"

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
    import bobi.brain

    _FakeClient.SNAPSHOTS = snapshots
    _FakeClient.HANG_CONNECT = False
    _FakeClient.HANG_STATUS = False
    monkeypatch.setattr(bobi.brain, "get_brain", lambda: _FakeClaudeBrain())
    monkeypatch.setattr("bobi.sdk.get_cli_path", lambda: "claude")
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
        # Keep the derived poll budget tiny so the test is fast but bounded.
        monkeypatch.setenv("BOBI_MCP_PREFLIGHT_TIMEOUT", "3")
        results = asyncio.run(
            _async_probe_mcp(["slow"], {"slow": {"type": "stdio",
                             "command": "/abs/slow"}}, tmp_path)
        )
        assert results[0].ok is False
        assert "pending" in results[0].detail

    def test_default_poll_budget_remains_twenty_polls(
        self, monkeypatch, tmp_path
    ):
        snapshots = [
            {"mcpServers": [{"name": "slow", "status": "pending"}]}
            for _ in range(20)
        ] + [
            {"mcpServers": [{"name": "slow", "status": "connected",
                             "tools": []}]},
        ]
        _install_fake_client(monkeypatch, snapshots, interval=0.5)
        sleeps = []

        async def fake_sleep(duration):
            sleeps.append(duration)

        monkeypatch.delenv("BOBI_MCP_PREFLIGHT_TIMEOUT", raising=False)
        monkeypatch.setattr(validate.asyncio, "sleep", fake_sleep)
        results = asyncio.run(
            _async_probe_mcp(["slow"], {"slow": {"type": "stdio",
                             "command": "/abs/slow"}}, tmp_path)
        )
        assert results[0].ok is True
        assert sleeps == [0.5] * 20

    def test_env_timeout_derives_poll_budget(self, monkeypatch, tmp_path):
        snapshots = [
            {"mcpServers": [{"name": "slow", "status": "pending"}]},
            {"mcpServers": [{"name": "slow", "status": "pending"}]},
            {"mcpServers": [{"name": "slow", "status": "connected",
                             "tools": []}]},
        ]
        _install_fake_client(monkeypatch, snapshots, interval=0.5)
        sleeps = []

        async def fake_sleep(duration):
            sleeps.append(duration)

        monkeypatch.setenv("BOBI_MCP_PREFLIGHT_TIMEOUT", "1")
        monkeypatch.setattr(validate.asyncio, "sleep", fake_sleep)
        results = asyncio.run(
            _async_probe_mcp(["slow"], {"slow": {"type": "stdio",
                             "command": "/abs/slow"}}, tmp_path)
        )
        assert results[0].ok is True
        assert sleeps == [0.5, 0.5]

    def test_env_timeout_bounds_status_call(self, monkeypatch, tmp_path):
        snapshots = [
            {"mcpServers": [{"name": "slow", "status": "pending"}]},
        ]
        _install_fake_client(monkeypatch, snapshots, interval=0.01)
        _FakeClient.HANG_STATUS = True
        monkeypatch.setenv("BOBI_MCP_PREFLIGHT_TIMEOUT", "0.05")

        results = validate._probe_mcp_servers(
            ["slow"], {"slow": {"type": "stdio", "command": "/abs/slow"}},
            tmp_path,
        )
        assert len(results) == 1
        assert results[0].ok is False
        assert "probe failed" in results[0].detail

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
        import bobi.brain

        monkeypatch.setattr(
            bobi.brain, "get_brain",
            lambda: _FakeBrainWithoutMcpStatus(),
        )
        results = asyncio.run(
            _async_probe_mcp(["x"], {"x": {"type": "stdio",
                             "command": "/abs/x"}}, tmp_path)
        )
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].required is False
        assert "not supported for future brain" in results[0].detail

    def test_codex_brain_probes_via_direct_handshake(
        self, monkeypatch, tmp_path
    ):
        """#428 Stage 4: Codex no longer warn-degrades — its get_mcp_status runs a
        real initialize handshake, so the single-loop probe judges it like Claude.
        A connected server → an ok CheckResult with the tool count."""
        import bobi.brain
        from bobi.brain.codex import CodexBrain

        # Redirect config.toml render (make_session side effect) off the real home.
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        monkeypatch.setenv("BOBI_MCP_PREFLIGHT_TIMEOUT", "60")
        monkeypatch.setattr(bobi.brain, "get_brain", lambda: CodexBrain())
        seen_timeouts = []

        async def _fake_probe(mcp_servers, timeout=10.0, env=None):
            seen_timeouts.append(timeout)
            return {"mcpServers": [
                {"name": n, "status": "connected", "tools": ["t1", "t2"],
                 "error": None}
                for n in mcp_servers
            ]}
        monkeypatch.setattr("bobi.mcp_handshake.probe_servers", _fake_probe)
        monkeypatch.setattr(validate, "MCP_PROBE_POLL_INTERVAL", 0.0)

        results = asyncio.run(
            _async_probe_mcp(["weather"], {"weather": {
                "type": "stdio", "command": "/abs/weather-mcp"}}, tmp_path)
        )
        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].name == "weather"
        assert "2 tools" in results[0].detail
        assert seen_timeouts == [60.0]

    def test_codex_brain_reports_failed_handshake(self, monkeypatch, tmp_path):
        """A codex server that fails initialize is a blocking failure (per-brain
        success is the handshake, not a warn)."""
        import bobi.brain
        from bobi.brain.codex import CodexBrain

        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        monkeypatch.setattr(bobi.brain, "get_brain", lambda: CodexBrain())

        async def _fake_probe(mcp_servers, timeout=10.0, env=None):
            return {"mcpServers": [
                {"name": n, "status": "failed", "tools": [],
                 "error": "spawn /abs/weather-mcp ENOENT"}
                for n in mcp_servers
            ]}
        monkeypatch.setattr("bobi.mcp_handshake.probe_servers", _fake_probe)
        monkeypatch.setattr(validate, "MCP_PROBE_POLL_INTERVAL", 0.0)

        results = asyncio.run(
            _async_probe_mcp(["weather"], {"weather": {
                "type": "stdio", "command": "/abs/weather-mcp"}}, tmp_path)
        )
        assert results[0].ok is False
        assert "ENOENT" in results[0].detail


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
