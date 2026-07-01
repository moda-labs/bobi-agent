"""Tests for rendering team-brought MCP servers into Codex's config.toml
(#428 Stage 4).

Pure render tests pin the TOML shape; a guarded round-trip proves the rendered
file is what the real ``codex`` CLI parses (the authoritative bar — the shape was
derived from ``codex mcp add`` / ``codex mcp list --json`` on rust-v0.142).
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib

import pytest

from bobi.brain import codex_config


# --- pure render -------------------------------------------------------------


def test_render_empty_is_empty():
    assert codex_config.render_mcp_tables({}) == ""
    assert codex_config.render_mcp_tables(None) == ""


def test_render_stdio_server_with_env():
    toml = codex_config.render_mcp_tables({
        "weather": {
            "type": "stdio",
            "command": "/usr/local/bin/weather-mcp",
            "args": ["--stdio", "-v"],
            "env": {"API_KEY": "abc"},
        }
    })
    data = tomllib.loads(toml)
    srv = data["mcp_servers"]["weather"]
    assert srv["command"] == "/usr/local/bin/weather-mcp"
    assert srv["args"] == ["--stdio", "-v"]
    assert srv["env"] == {"API_KEY": "abc"}


def test_render_defaults_to_stdio_when_type_absent():
    toml = codex_config.render_mcp_tables({
        "x": {"command": "/opt/x"}
    })
    assert tomllib.loads(toml)["mcp_servers"]["x"]["command"] == "/opt/x"


def test_render_http_server_with_headers():
    toml = codex_config.render_mcp_tables({
        "remote": {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer xyz"},
        }
    })
    srv = tomllib.loads(toml)["mcp_servers"]["remote"]
    assert srv["url"] == "https://example.com/mcp"
    assert srv["http_headers"] == {"Authorization": "Bearer xyz"}


def test_render_sse_maps_like_http():
    toml = codex_config.render_mcp_tables({
        "s": {"type": "sse", "url": "https://x/sse"}
    })
    assert tomllib.loads(toml)["mcp_servers"]["s"]["url"] == "https://x/sse"


def test_render_is_deterministic_sorted_by_name():
    servers = {
        "zeta": {"command": "/z"},
        "alpha": {"command": "/a"},
    }
    toml = codex_config.render_mcp_tables(servers)
    assert toml.index("mcp_servers.alpha") < toml.index("mcp_servers.zeta")
    # Reordering the input dict must not churn the output.
    assert codex_config.render_mcp_tables(
        {"alpha": {"command": "/a"}, "zeta": {"command": "/z"}}) == toml


def test_render_quotes_non_bare_names_and_escapes_strings():
    toml = codex_config.render_mcp_tables({
        "my.server": {"command": 'a "quoted" \\ path'},
    })
    # Dotted name must be quoted so it's a single table segment, and the value's
    # quote/backslash escaped — real TOML round-trips it.
    data = tomllib.loads(toml)
    assert data["mcp_servers"]["my.server"]["command"] == 'a "quoted" \\ path'


# --- managed-block merge -----------------------------------------------------


def test_render_config_preserves_foreign_content():
    existing = 'model = "gpt-5"\n\n[history]\npersistence = "none"\n'
    out = codex_config.render_config(existing, {"x": {"command": "/x"}})
    data = tomllib.loads(out)
    assert data["model"] == "gpt-5"
    assert data["history"] == {"persistence": "none"}
    assert data["mcp_servers"]["x"]["command"] == "/x"
    assert codex_config.MANAGED_BEGIN in out


def test_render_config_is_idempotent():
    existing = 'model = "gpt-5"\n'
    servers = {"x": {"command": "/x"}}
    once = codex_config.render_config(existing, servers)
    twice = codex_config.render_config(once, servers)
    assert once == twice


def test_render_config_replaces_stale_managed_block():
    existing = codex_config.render_config("", {"old": {"command": "/old"}})
    out = codex_config.render_config(existing, {"new": {"command": "/new"}})
    data = tomllib.loads(out)
    assert "old" not in data.get("mcp_servers", {})
    assert data["mcp_servers"]["new"]["command"] == "/new"


def test_render_config_empty_servers_removes_block_keeps_foreign():
    existing = codex_config.render_config('model = "gpt-5"\n', {"x": {"command": "/x"}})
    out = codex_config.render_config(existing, {})
    assert codex_config.MANAGED_BEGIN not in out
    assert tomllib.loads(out)["model"] == "gpt-5"


def test_render_config_empty_from_scratch_is_empty():
    assert codex_config.render_config("", {}) == ""


def test_render_config_strips_stray_unmanaged_mcp_table():
    """A manual `[mcp_servers.x]` outside a managed block must not survive into a
    re-render (it would duplicate the table and break codex TOML parsing). bobi
    owns the whole mcp_servers namespace."""
    existing = 'model = "gpt-5"\n\n[mcp_servers.x]\ncommand = "/manual"\n'
    out = codex_config.render_config(existing, {"x": {"command": "/rendered"}})
    data = tomllib.loads(out)  # must parse — no duplicate table
    assert data["mcp_servers"]["x"]["command"] == "/rendered"
    assert data["model"] == "gpt-5"
    # Exactly one [mcp_servers.x] header.
    assert out.count("[mcp_servers.x]") == 1


def test_render_config_empty_removes_stray_mcp_but_keeps_foreign():
    existing = '[history]\npersistence = "none"\n\n[mcp_servers.x]\ncommand = "/x"\n'
    out = codex_config.render_config(existing, {})
    data = tomllib.loads(out)
    assert "mcp_servers" not in data
    assert data["history"] == {"persistence": "none"}


def test_is_mcp_header_does_not_match_lookalikes():
    assert codex_config._is_mcp_header("[mcp_servers.x]")
    assert codex_config._is_mcp_header("[mcp_servers]")
    assert codex_config._is_mcp_header("[mcp_servers.x.env]")
    assert not codex_config._is_mcp_header("[mcp_servers_other]")
    assert not codex_config._is_mcp_header("[model_providers.x]")


def test_config_has_managed_block(tmp_path):
    assert codex_config.config_has_managed_block(tmp_path) is False
    codex_config.write_codex_config({"x": {"command": "/x"}}, home=tmp_path)
    assert codex_config.config_has_managed_block(tmp_path) is True
    codex_config.write_codex_config({}, home=tmp_path)
    assert codex_config.config_has_managed_block(tmp_path) is False


# --- disk writer -------------------------------------------------------------


def test_write_codex_config_writes_and_is_no_op_when_unchanged(tmp_path):
    servers = {"x": {"command": "/x"}}
    path = codex_config.write_codex_config(servers, home=tmp_path)
    assert path == tmp_path / "config.toml"
    first = path.read_text()
    mtime = path.stat().st_mtime_ns
    # Re-writing the identical set must not touch the file (durable-volume churn).
    codex_config.write_codex_config(servers, home=tmp_path)
    assert path.read_text() == first
    assert path.stat().st_mtime_ns == mtime


def test_write_codex_config_honors_codex_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
    path = codex_config.write_codex_config({"x": {"command": "/x"}})
    assert path == tmp_path / "ch" / "config.toml"


# --- real codex round-trip (the authoritative bar) ---------------------------


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
def test_rendered_config_parses_in_real_codex(tmp_path, monkeypatch):
    """`codex mcp list --json` reports back exactly the servers we rendered —
    proves the TOML shape matches the codex CLI, not just our reading of it."""
    import json

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    codex_config.write_codex_config({
        "weather": {
            "type": "stdio",
            "command": "/usr/local/bin/weather-mcp",
            "args": ["--stdio"],
            "env": {"API_KEY": "abc"},
        },
        "remote": {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer xyz"},
        },
    })
    out = subprocess.run(
        ["codex", "mcp", "list", "--json"],
        capture_output=True, text=True, timeout=30,
        env={**subprocess.os.environ, "CODEX_HOME": str(tmp_path)},
    )
    assert out.returncode == 0, out.stderr
    by_name = {s["name"]: s for s in json.loads(out.stdout)}
    assert by_name["weather"]["transport"]["type"] == "stdio"
    assert by_name["weather"]["transport"]["command"] == "/usr/local/bin/weather-mcp"
    assert by_name["weather"]["transport"]["env"] == {"API_KEY": "abc"}
    assert by_name["remote"]["transport"]["type"] == "streamable_http"
    assert by_name["remote"]["transport"]["http_headers"] == {"Authorization": "Bearer xyz"}
