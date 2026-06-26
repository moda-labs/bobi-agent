"""Connection-test probe: transport dispatch + env resolution.

The real handshake spawns a subprocess, so the live path is validated manually;
here we cover the pure logic (which transport runs, env assembly) without
launching anything.
"""

import anyio
import pytest

from bobi.setup import mcp_probe


class TestDispatch:
    def _run(self, entry, project, monkeypatch):
        calls = {}

        async def fake_stdio(e, p, t, call_name):
            calls["stdio"] = True
            return {"ok": True, "via": "stdio"}

        async def fake_http(e, p, t, call_name):
            calls["http"] = True
            return {"ok": True, "via": "http"}

        monkeypatch.setattr(mcp_probe, "_probe_stdio", fake_stdio)
        monkeypatch.setattr(mcp_probe, "_probe_http", fake_http)
        return anyio.run(mcp_probe.probe, entry, project), calls

    def test_stdio_when_command(self, tmp_path, monkeypatch):
        res, calls = self._run({"type": "stdio", "command": "uv"}, tmp_path, monkeypatch)
        assert res["via"] == "stdio" and "http" not in calls

    def test_http_when_url(self, tmp_path, monkeypatch):
        res, calls = self._run({"url": "https://mcp.x.com/mcp"}, tmp_path, monkeypatch)
        assert res["via"] == "http" and "stdio" not in calls

    def test_neither_is_an_error(self, tmp_path, monkeypatch):
        res, _ = self._run({"label": "x"}, tmp_path, monkeypatch)
        assert res["ok"] is False


class TestMatchConnectionTest:
    SERVERS = {"substack_mcp": {"type": "stdio", "command": "uv",
                                "label": "substack-mcp"}}

    @pytest.mark.parametrize("msg", [
        "are we connected?",
        "can you test the substack connection?",
        "does it work?",
        "pull a note to check it works",
        "test the connection",
    ])
    def test_detects_intent(self, msg):
        m = mcp_probe.match_connection_test(msg, self.SERVERS)
        assert m["intent"] is True and m["key"] == "substack_mcp"

    @pytest.mark.parametrize("msg", [
        "add a project lead role",
        "the team should connect Slack and post updates",
        "what services do we need?",
    ])
    def test_ignores_ordinary_chat(self, msg):
        assert mcp_probe.match_connection_test(msg, self.SERVERS)["intent"] is False

    def test_named_connection_resolves_among_several(self):
        servers = {"substack_mcp": {"command": "uv", "label": "substack-mcp"},
                   "notion_mcp": {"command": "uv", "label": "notion-mcp"}}
        m = mcp_probe.match_connection_test("test the notion connection", servers)
        assert m["key"] == "notion_mcp"

    def test_ambiguous_when_several_and_none_named(self):
        servers = {"a_mcp": {"command": "x", "label": "a"},
                   "b_mcp": {"command": "y", "label": "b"}}
        m = mcp_probe.match_connection_test("are we connected?", servers)
        assert m["intent"] is True and m.get("ambiguous") is True
        assert set(m["candidates"]) == {"a", "b"}

    def test_none_configured(self):
        m = mcp_probe.match_connection_test("test the connection", {})
        assert m["intent"] is True and m.get("none") is True


class TestIsReadOnly:
    @pytest.mark.parametrize("name", [
        "get_profile", "substack_get_notes_feed", "list_articles",
        "search_notes", "whoami", "github_get_repo"])
    def test_safe_read_tools(self, name):
        assert mcp_probe._is_read_only(name) is True

    @pytest.mark.parametrize("name", [
        "post_note", "substack_post_note", "delete_repo", "create_issue",
        "list_and_purge",        # read verb leads but a mutation word follows
        "get_or_delete",         # contains delete
        "deleteAll",             # camelCase write
        "github.delete_repo",    # dotted namespace write
        "send_message", "update_profile", "purge_cache", "revoke_token"])
    def test_write_or_destructive_tools_are_not_safe(self, name):
        assert mcp_probe._is_read_only(name) is False

    def test_read_verb_must_lead(self):
        # A read word buried after the namespace position doesn't make it safe.
        assert mcp_probe._is_read_only("a_b_c_get") is False


class TestMatchTestConfirmation:
    PENDING = {"key": "substack_mcp", "proposed": "substack_get_notes_feed",
               "tools": ["substack_get_notes_feed", "substack_get_profile",
                         "substack_post_note"]}

    @pytest.mark.parametrize("msg", ["yes", "yep", "go ahead", "run it", "ok"])
    def test_affirmation_runs_the_proposed_tool(self, msg):
        d = mcp_probe.match_test_confirmation(msg, self.PENDING)
        assert d["action"] == "run" and d["tool"] == "substack_get_notes_feed"

    def test_naming_a_read_only_tool_runs_it(self):
        d = mcp_probe.match_test_confirmation(
            "call substack_get_profile instead", self.PENDING)
        assert d["action"] == "run" and d["tool"] == "substack_get_profile"

    def test_naming_a_write_tool_is_refused_not_run(self):
        d = mcp_probe.match_test_confirmation(
            "call substack_post_note", self.PENDING)
        assert d["action"] == "refuse_write" and d["tool"] == "substack_post_note"

    def test_negation_of_a_write_tool_cancels_not_runs(self):
        # "don't call substack_post_note" must cancel, never match+run the name.
        d = mcp_probe.match_test_confirmation(
            "don't call substack_post_note", self.PENDING)
        assert d["action"] == "cancel"

    @pytest.mark.parametrize("msg", ["no", "no thanks", "cancel", "skip"])
    def test_decline(self, msg):
        assert mcp_probe.match_test_confirmation(msg, self.PENDING)["action"] == "cancel"

    @pytest.mark.parametrize("msg", [
        "actually add a notion connection too",
        "is the output ok?",        # bare 'ok' mid-sentence must NOT run
        "what does run mean here"])  # bare 'run' mid-sentence must NOT run
    def test_unrelated_message_is_none(self, msg):
        assert mcp_probe.match_test_confirmation(msg, self.PENDING)["action"] == "none"


class TestEnvResolution:
    def test_declared_vars_pulled_from_env_file(self, tmp_path, monkeypatch):
        # .env value is surfaced into the child env for declared vars only.
        from bobi.setup import actions
        monkeypatch.setattr(actions, "read_env",
                            lambda p: {"SUBSTACK_COOKIE": "sek", "UNRELATED": "x"})
        env = mcp_probe._resolved_env(
            {"env_vars": ["SUBSTACK_COOKIE"]}, tmp_path)
        assert env["SUBSTACK_COOKIE"] == "sek"
        assert "UNRELATED" not in {k: v for k, v in env.items()
                                   if k == "UNRELATED" and k not in __import__("os").environ}

    def test_no_declared_vars_just_process_env(self, tmp_path):
        env = mcp_probe._resolved_env({}, tmp_path)
        assert "PATH" in env   # inherits the process environment
