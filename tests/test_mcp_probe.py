"""Connection-test probe: transport dispatch, env resolution, and the
test-a-connection dialogue.

The real handshake spawns a subprocess, so the live path is validated manually;
here we cover the pure logic (which transport runs, env assembly, the two
conversation turns) without launching anything.
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


class TestMcp20FieldRenames:
    """mcp 2.0 renames ``Tool.inputSchema`` → ``input_schema`` and
    ``CallToolResult.isError`` → ``is_error`` (camelCase survives only as the
    wire alias). The probe must read both eras — and fail LOUD, never
    silently-green, when neither spelling exists."""

    @staticmethod
    def _tool(name, schema, era):
        attr = {"1x": "inputSchema", "20": "input_schema"}[era]
        return type("T", (), {"name": name, attr: schema})

    @pytest.mark.parametrize("era", ["1x", "20"])
    def test_pick_safe_tool_reads_both_spellings(self, era):
        t = self._tool("get_profile", {"type": "object"}, era)
        assert mcp_probe._pick_safe_tool([t]) is t

    @pytest.mark.parametrize("era", ["1x", "20"])
    def test_required_args_disqualify_in_either_era(self, era):
        t = self._tool("get_profile", {"required": ["id"]}, era)
        assert mcp_probe._pick_safe_tool([t]) is None

    def test_unreadable_schema_is_unknown_never_no_required_args(self):
        # Neither spelling: the tool must not be proposed for a live call.
        t = type("T", (), {"name": "get_profile"})
        assert mcp_probe._pick_safe_tool([t]) is None

    def test_call_errored_reads_both_spellings(self):
        assert mcp_probe._call_errored(type("R", (), {"is_error": True})) is True
        assert mcp_probe._call_errored(type("R", (), {"isError": True})) is True
        assert mcp_probe._call_errored(type("R", (), {"is_error": False})) is False
        assert mcp_probe._call_errored(type("R", (), {"isError": False})) is False

    def test_call_errored_fails_loud_when_neither_spelling_exists(self):
        with pytest.raises(AttributeError):
            mcp_probe._call_errored(type("R", (), {}))

    def test_shadowed_extra_field_never_wins_on_1x_shapes(self):
        # mcp 1.x models are extra="allow": a server-supplied wire key with
        # the 2.0 spelling lands as an attribute BESIDE the declared
        # (camelCase) field. The declared field must win.
        t = type("T", (), {"name": "get_profile",
                           "inputSchema": {"required": ["id"]},
                           "input_schema": {}})
        assert mcp_probe._pick_safe_tool([t]) is None
        out = type("R", (), {"isError": True, "is_error": False})
        assert mcp_probe._call_errored(out) is True

    def test_http_probe_touches_only_the_first_two_stream_slots(
            self, tmp_path, monkeypatch):
        # mcp 2.0's transport yields (read, write) — no session-id slot; a
        # 3-unpack regression in _probe_http fails here.
        import contextlib
        from bobi import mcp_handshake

        @contextlib.asynccontextmanager
        async def _fake_open(url, headers=None):
            yield ("r", "w")

        seen = {}

        async def _fake_handshake(read, write, call_name):
            seen["streams"] = (read, write)
            return {"ok": True}

        monkeypatch.setattr(mcp_handshake, "open_streamable_http", _fake_open)
        monkeypatch.setattr(mcp_probe, "_handshake", _fake_handshake)
        res = anyio.run(mcp_probe._probe_http,
                        {"url": "https://x/mcp"}, tmp_path, 5.0, None)
        assert res == {"ok": True}
        assert seen["streams"] == ("r", "w")

    def test_errored_call_reports_live_failure_in_the_mcp20_shape(
            self, monkeypatch):
        # Through _handshake: a 2.0-shaped errored result must land as
        # live_ok=False — not render the error text as successful output.
        class _Content:
            text = "boom: unauthorized"

        class _Out:
            is_error = True
            content = [_Content()]

        class _Tools:
            tools = []

        class _Session:
            def __init__(self, r, w):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def initialize(self):
                pass

            async def list_tools(self):
                return _Tools()

            async def call_tool(self, name, args):
                return _Out()

        monkeypatch.setattr("mcp.ClientSession", _Session)
        res = anyio.run(mcp_probe._handshake, "r", "w", "get_profile")
        assert res["live_ok"] is False
        assert "unauthorized" in res["live_error"]


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

    def test_exported_var_beats_env_file_like_the_running_agent(
            self, tmp_path, monkeypatch):
        # The probe must hand the child the credential `bobi agent start`
        # would run with. config.load_dotenv never overwrites an existing
        # os.environ entry, so an exported value wins over run/.env.
        from bobi.setup import actions
        monkeypatch.setattr(actions, "read_env", lambda p: {"TOK": "from-dotenv"})
        monkeypatch.setenv("TOK", "from-shell")
        env = mcp_probe._resolved_env({"env_vars": ["TOK"]}, tmp_path)
        assert env["TOK"] == "from-shell"


class TestScrubResult:
    def test_redacts_both_copies_when_the_two_sources_disagree(
            self, tmp_path, monkeypatch):
        # The losing copy is still a live secret on this machine, and the
        # probe's own output is the thing most likely to echo it.
        from bobi.setup import actions
        monkeypatch.setattr(actions, "read_env",
                            lambda p: {"TOK": "dotenv-secret-value"})
        monkeypatch.setenv("TOK", "shell-secret-value")
        out = mcp_probe._scrub_result(
            {"output": "saw dotenv-secret-value and shell-secret-value"},
            {"env_vars": ["TOK"]}, tmp_path)
        assert "dotenv-secret-value" not in out["output"]
        assert "shell-secret-value" not in out["output"]

    def test_short_values_are_not_redacted(self, tmp_path, monkeypatch):
        # Below the 8-char floor a "secret" is too generic to blank safely.
        from bobi.setup import actions
        monkeypatch.setattr(actions, "read_env", lambda p: {"TOK": "abc"})
        monkeypatch.delenv("TOK", raising=False)
        out = mcp_probe._scrub_result({"output": "abc"}, {"env_vars": ["TOK"]},
                                      tmp_path)
        assert out["output"] == "abc"


class TestTestConnectionDialogue:
    """The two dialogue turns, driven directly.

    These used to be closures inside the /api/message route, reachable only
    through a TestClient. As module-level generators they can be run on a bare
    SetupState — the route's only remaining job is SSE-wrapping the chunks.
    """

    @staticmethod
    def _drain(gen):
        chunks = []

        async def run():
            async for c in gen:
                chunks.append(c)
        anyio.run(run)
        return "".join(chunks)

    @staticmethod
    def _state():
        from bobi.setup.state import SetupState
        return SetupState()

    def test_no_connections_configured_says_so_and_records_the_turn(self, tmp_path):
        s = self._state()
        out = self._drain(mcp_probe.propose_test(
            s, tmp_path, "test the connection", {"intent": True, "none": True}))
        assert "no MCP connections" in out
        # The exchange lands in history exactly once, user turn then reply.
        assert [m["role"] for m in s.messages] == ["user", "assistant"]
        assert s.messages[0]["content"] == "test the connection"

    def test_ambiguous_lists_the_candidates(self, tmp_path):
        s = self._state()
        out = self._drain(mcp_probe.propose_test(
            s, tmp_path, "test it",
            {"intent": True, "ambiguous": True, "candidates": ["a", "b"]}))
        assert "a, b" in out

    def test_cancel_clears_the_pending_proposal(self, tmp_path):
        s = self._state()
        s.pending_test = {"key": "k", "proposed": "t", "tools": ["t"]}
        out = self._drain(mcp_probe.resolve_pending(
            s, tmp_path, "no thanks", {"action": "cancel"}))
        assert "skipped" in out.lower()
        assert s.pending_test == {}

    def test_write_tool_is_refused_and_the_proposal_survives(self, tmp_path):
        # Refusing must not clear pending_test — the user still gets to pick a
        # read-only tool without re-running the listing probe.
        s = self._state()
        s.pending_test = {"key": "k", "proposed": "get_x", "tools": ["get_x"]}
        out = self._drain(mcp_probe.resolve_pending(
            s, tmp_path, "call delete_all",
            {"action": "refuse_write", "tool": "delete_all"}))
        assert "delete_all" in out and "won’t" in out
        assert s.pending_test["proposed"] == "get_x"

    def test_removed_connection_is_not_tested(self, tmp_path, monkeypatch):
        # The entry vanished between proposal and confirmation.
        s = self._state()
        s.pending_test = {"key": "gone", "proposed": "get_x", "tools": ["get_x"]}
        called = []
        monkeypatch.setattr(mcp_probe, "probe",
                            lambda *a, **k: called.append(a) or {})
        out = self._drain(mcp_probe.resolve_pending(
            s, tmp_path, "yes", {"action": "run", "tool": "get_x"}))
        assert "isn’t there anymore" in out
        assert called == []          # nothing was launched

    def test_probe_identity_ignores_only_the_recorded_outcome(self):
        entry = {"command": "uv", "args": "run x", "last_test": {"ok": True}}
        assert mcp_probe._probe_identity(entry) == {"command": "uv",
                                                    "args": "run x"}
        # A command edit is a different connection; a new verdict is not.
        assert (mcp_probe._probe_identity({**entry, "last_test": {"ok": False}})
                == mcp_probe._probe_identity(entry))
        assert (mcp_probe._probe_identity({**entry, "command": "npx"})
                != mcp_probe._probe_identity(entry))
