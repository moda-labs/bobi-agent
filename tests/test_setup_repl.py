"""Tests for the setup REPL loop, driven by a fake SDK client."""

import asyncio

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from modastack.setup.repl import run_repl, _make_write_guard
from modastack.setup.state import SetupState, Stage


def _result_msg(session_id="sess-1"):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id=session_id, total_cost_usd=0.0,
    )


def _assistant_msg(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="fake")


class FakeClient:
    """Scripted stand-in for ClaudeSDKClient.

    `script` is a list of turns; each turn is a list of messages yielded
    by receive_response. `on_turn` optionally mutates shared state before
    a turn's messages are yielded (simulating tool side effects).
    """

    def __init__(self, options):
        self.options = options
        self.queries = []
        self.connected = False
        self.turn = 0

    script = []
    on_turn = None

    async def connect(self):
        self.connected = True

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        idx = min(self.turn, len(self.script) - 1)
        self.turn += 1
        if FakeClient.on_turn is not None:
            FakeClient.on_turn(idx)
        for msg in self.script[idx]:
            yield msg

    async def interrupt(self):
        pass

    async def disconnect(self):
        self.connected = False


@pytest.fixture
def fake_client_factory():
    created = []

    def factory(options):
        client = FakeClient(options)
        created.append(client)
        return client

    factory.created = created
    return factory


def _run(project, factory, inputs, resume=False):
    queue = list(inputs)

    def input_fn():
        return queue.pop(0) if queue else None

    return asyncio.run(run_repl(project, resume=resume,
                                client_factory=factory, input_fn=input_fn))


class TestReplFlow:
    def test_finish_exits_zero_and_clears_state(self, tmp_path, fake_client_factory):
        state_holder = {}

        def on_turn(idx):
            # Simulate finish_setup on the second turn.
            if idx == 1 and "state" in state_holder:
                state_holder["state"].finished = True

        FakeClient.script = [
            [_assistant_msg("Welcome! What do you want to build?"), _result_msg()],
            [_assistant_msg("Done — run modastack start."), _result_msg()],
        ]
        FakeClient.on_turn = on_turn

        # Capture the live state object via the server factory.
        import modastack.setup.repl as repl_mod
        orig = repl_mod.create_setup_server

        def capture_server(state, project, prompt_fn=None):
            state_holder["state"] = state
            return orig(state, project, prompt_fn)

        repl_mod.create_setup_server = capture_server
        try:
            code = _run(tmp_path, fake_client_factory, ["build me an agent"])
        finally:
            repl_mod.create_setup_server = orig
            FakeClient.on_turn = None

        assert code == 0
        assert SetupState.load(tmp_path) is None  # cleared on success
        client = fake_client_factory.created[0]
        assert len(client.queries) == 2  # kickoff + one user message

    def test_eof_pauses_with_resumable_state(self, tmp_path, fake_client_factory):
        FakeClient.script = [
            [_assistant_msg("Which team?"), _result_msg("sess-abc")],
        ]
        code = _run(tmp_path, fake_client_factory, [])  # immediate EOF
        assert code == 1
        saved = SetupState.load(tmp_path)
        assert saved is not None
        assert saved.session_id == "sess-abc"

    def test_resume_requires_prior_state(self, tmp_path, fake_client_factory):
        code = _run(tmp_path, fake_client_factory, [], resume=True)
        assert code == 1
        assert not fake_client_factory.created  # never connected

    def test_resume_passes_session_id_and_context(self, tmp_path, fake_client_factory):
        prior = SetupState(stage=Stage.INTERVIEW, branch="build",
                           team_name="my-team", session_id="sess-old",
                           answers={"purpose": "demo"})
        prior.save(tmp_path)
        FakeClient.script = [[_assistant_msg("Picking up."), _result_msg()]]
        _run(tmp_path, fake_client_factory, [], resume=True)
        client = fake_client_factory.created[0]
        assert client.options.resume == "sess-old"
        kickoff = client.queries[0]
        assert "RESUMES" in kickoff
        assert "interview" in kickoff
        assert "my-team" in kickoff

    def test_resume_of_finished_setup_refused(self, tmp_path, fake_client_factory):
        done = SetupState(finished=True)
        done.save(tmp_path)
        code = _run(tmp_path, fake_client_factory, [], resume=True)
        assert code == 1
        assert not fake_client_factory.created

    def test_blank_input_not_sent(self, tmp_path, fake_client_factory):
        FakeClient.script = [[_assistant_msg("hi"), _result_msg()]]
        _run(tmp_path, fake_client_factory, ["", "   ", "real message"])
        client = fake_client_factory.created[0]
        assert len(client.queries) == 2  # kickoff + "real message" only
        assert client.queries[1] == "real message"

    def test_fresh_run_clears_stale_state(self, tmp_path, fake_client_factory):
        SetupState(stage=Stage.GENERATE, branch="build", team_name="old").save(tmp_path)
        FakeClient.script = [[_assistant_msg("hi"), _result_msg()]]
        _run(tmp_path, fake_client_factory, [])
        client = fake_client_factory.created[0]
        assert client.options.resume is None  # fresh session, not resumed


class TestWriteGuard:
    def _guard(self, tmp_path, team="my-team"):
        state = SetupState(team_name=team)
        hooks = _make_write_guard(tmp_path, state)
        return hooks["PreToolUse"][0].hooks[0]

    def test_denies_env_reads(self, tmp_path):
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Read",
             "tool_input": {"file_path": str(tmp_path / ".modastack" / ".env")}},
            "id", None))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_denies_bash_env_access(self, tmp_path):
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "cat .modastack/.env"}},
            "id", None))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_denies_writes_outside_team_source(self, tmp_path):
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(tmp_path / "README.md")}},
            "id", None))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_allows_writes_to_team_source(self, tmp_path):
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(
                 tmp_path / "agents" / "my-team" / "agent.yaml")}},
            "id", None))
        assert result == {}

    def test_allows_ordinary_reads(self, tmp_path):
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Read",
             "tool_input": {"file_path": str(tmp_path / "docs" / "x.md")}},
            "id", None))
        assert result == {}

    def test_denies_traversal_escape(self, tmp_path):
        guard = self._guard(tmp_path)
        sneaky = tmp_path / "agents" / "my-team" / ".." / ".." / ".modastack" / "agent.yaml"
        result = asyncio.run(guard(
            {"tool_name": "Write", "tool_input": {"file_path": str(sneaky)}},
            "id", None))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_denies_sibling_prefix_directory(self, tmp_path):
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(
                 tmp_path / "agents" / "my-team-archive" / "x.yaml")}},
            "id", None))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_allows_relative_path_inside_team_source(self, tmp_path):
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": "agents/my-team/roles/x/ROLE.md"}},
            "id", None))
        assert result == {}

    def test_allows_team_content_mentioning_env(self, tmp_path):
        # File *content* may legitimately document where secrets live —
        # only the write target matters.
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Write",
             "tool_input": {
                 "file_path": str(tmp_path / "agents" / "my-team" / "agent.md"),
                 "content": "Secrets live in .modastack/.env as ${VAR} refs."}},
            "id", None))
        assert result == {}

    def test_denies_env_read_via_relative_path(self, tmp_path):
        guard = self._guard(tmp_path)
        result = asyncio.run(guard(
            {"tool_name": "Read",
             "tool_input": {"file_path": ".modastack/.env"}},
            "id", None))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
