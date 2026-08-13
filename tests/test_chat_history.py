"""Unit tests for the chat-panel transcript reader — brain-aware dispatch and
the Codex rollout parser (fixes the fleet UI showing no history for codex agents).

The Codex rollout shapes here are copied from a real
``$CODEX_HOME/sessions/.../rollout-*.jsonl`` produced by codex-cli 0.142.x: a
``session_meta`` header, ``response_item`` messages with ``role``
developer/user/assistant and ``input_text``/``output_text`` content blocks, plus
``event_msg``/``reasoning``/``function_call`` bookkeeping the reader must skip.
"""

import json
from pathlib import Path

import pytest

from bobi import chat_history
from bobi.chat_history import (
    _codex_rollout_path,
    _codex_text,
    read_codex_transcript_messages,
    read_transcript_messages,
)


# --- fixtures ---------------------------------------------------------------

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _msg(role: str, *texts: str, block: str = "input_text") -> dict:
    """A codex ``response_item`` message row (developer/user/assistant)."""
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": block, "text": t} for t in texts],
        },
    }


CODEX_ROWS = [
    {"type": "session_meta", "payload": {"session_id": "SID"}},
    _msg("developer", "<permissions instructions> ... </permissions instructions>"),
    _msg("user", "You are a manager agent. Complete the task."),
    {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
    {"type": "response_item", "payload": {"type": "reasoning",
                                          "summary": [{"text": "thinking..."}]}},
    {"type": "response_item", "payload": {"type": "function_call",
                                          "name": "shell", "arguments": "{}"}},
    _msg("assistant", "Startup checks are complete.", block="output_text"),
    {"type": "event_msg", "payload": {"type": "token_count", "total": 1234}},
    # Codex injects an environment-context block as a user-role message before a
    # turn — non-conversational, must not render as a chat bubble.
    _msg("user", "<environment_context>\n  <cwd>/run</cwd>\n</environment_context>"),
    _msg("user", "are you alive?"),
    _msg("assistant", "Yes. Manager is alive and standing by.", block="output_text"),
    {"type": "event_msg", "payload": {"type": "task_complete",
                                      "last_agent_message": "Yes. ..."}},
]


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """Point ``$CODEX_HOME`` at a tmp dir and return its ``sessions`` root."""
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home / "sessions"


def _write_rollout(sessions_root: Path, session_id: str,
                   rows: list[dict], *, day: str = "2026/07/09",
                   ts: str = "2026-07-09T05-12-25") -> Path:
    """Write a rollout at the real date-partitioned path, id in the filename."""
    path = sessions_root / day / f"rollout-{ts}-{session_id}.jsonl"
    _write_jsonl(path, rows)
    return path


# --- _codex_text ------------------------------------------------------------

class TestCodexText:
    def test_joins_text_blocks(self):
        content = [{"type": "input_text", "text": "a"},
                   {"type": "output_text", "text": "b"},
                   {"type": "text", "text": "c"}]
        assert _codex_text(content) == "a\nb\nc"

    def test_ignores_unknown_blocks(self):
        content = [{"type": "input_text", "text": "keep"},
                   {"type": "input_image", "image_url": "x"}]
        assert _codex_text(content) == "keep"

    def test_non_list_returns_empty(self):
        assert _codex_text(None) == ""
        assert _codex_text("just a string") == ""

    def test_missing_text_key(self):
        assert _codex_text([{"type": "input_text"}]) == ""


# --- _codex_rollout_path ----------------------------------------------------

class TestCodexRolloutPath:
    def test_finds_by_id_suffix_across_date_dirs(self, codex_home):
        _write_rollout(codex_home, "abc-123", CODEX_ROWS, day="2026/06/01")
        target = _write_rollout(codex_home, "xyz-789", CODEX_ROWS, day="2026/07/09")
        assert _codex_rollout_path("xyz-789") == target

    def test_missing_id_returns_none(self, codex_home):
        _write_rollout(codex_home, "abc-123", CODEX_ROWS)
        assert _codex_rollout_path("nope") is None

    def test_empty_session_id_returns_none(self, codex_home):
        assert _codex_rollout_path("") is None

    def test_no_sessions_dir_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
        assert _codex_rollout_path("anything") is None


# --- read_codex_transcript_messages -----------------------------------------

class TestReadCodexTranscript:
    def test_extracts_only_user_and_assistant_turns(self, codex_home):
        _write_rollout(codex_home, "SID", CODEX_ROWS)
        msgs = read_codex_transcript_messages("SID")
        # developer/reasoning/function_call/event_msg rows are all dropped.
        assert msgs == [
            {"role": "user", "text": "You are a manager agent. Complete the task."},
            {"role": "agent", "text": "Startup checks are complete."},
            {"role": "user", "text": "are you alive?"},
            {"role": "agent", "text": "Yes. Manager is alive and standing by."},
        ]

    def test_unknown_session_returns_empty(self, codex_home):
        _write_rollout(codex_home, "SID", CODEX_ROWS)
        assert read_codex_transcript_messages("OTHER") == []

    def test_skips_injected_context_blocks(self, codex_home):
        rows = [
            _msg("user", "<environment_context><cwd>/x</cwd></environment_context>"),
            _msg("user", "<user_instructions>be nice</user_instructions>"),
            _msg("user", "real question"),
            _msg("assistant", "real answer", block="output_text"),
        ]
        _write_rollout(codex_home, "SID", rows)
        assert read_codex_transcript_messages("SID") == [
            {"role": "user", "text": "real question"},
            {"role": "agent", "text": "real answer"},
        ]

    def test_skips_invalid_json_lines(self, codex_home):
        path = _write_rollout(codex_home, "SID", CODEX_ROWS)
        path.write_text(path.read_text() + "{not valid json}\n")
        msgs = read_codex_transcript_messages("SID")
        assert len(msgs) == 4

    def test_skips_empty_text(self, codex_home):
        rows = [_msg("user", ""), _msg("assistant", "   ", block="output_text"),
                _msg("user", "real")]
        _write_rollout(codex_home, "SID", rows)
        assert read_codex_transcript_messages("SID") == [
            {"role": "user", "text": "real"}]

    def test_limit_keeps_newest(self, codex_home):
        rows = [_msg("user", f"m{i}") for i in range(10)]
        _write_rollout(codex_home, "SID", rows)
        msgs = read_codex_transcript_messages("SID", limit=3)
        assert [m["text"] for m in msgs] == ["m7", "m8", "m9"]


# --- read_transcript_messages: brain dispatch -------------------------------

class TestBrainDispatch:
    def test_codex_brain_reads_rollout(self, codex_home):
        _write_rollout(codex_home, "SID", CODEX_ROWS)
        msgs = read_transcript_messages("SID", brain="codex")
        assert [m["text"] for m in msgs][-1] == "Yes. Manager is alive and standing by."

    def test_claude_brain_never_reads_codex_rollout(self, codex_home, monkeypatch):
        # A codex rollout exists, but brain=claude must not fall through to it.
        _write_rollout(codex_home, "SID", CODEX_ROWS)
        monkeypatch.setattr(chat_history, "find_claude_transcript", lambda sid: None)
        assert read_transcript_messages("SID", brain="claude") == []

    def test_unknown_brain_falls_back_to_codex(self, codex_home, monkeypatch):
        # No claude transcript resolves; an unrecorded brain still finds codex.
        _write_rollout(codex_home, "SID", CODEX_ROWS)
        monkeypatch.setattr(chat_history, "find_claude_transcript", lambda sid: None)
        for brain in (None, ""):
            msgs = read_transcript_messages("SID", brain=brain)
            assert [m["text"] for m in msgs][-1] == \
                "Yes. Manager is alive and standing by."

    def test_explicit_non_codex_brain_never_walks_codex(self, codex_home,
                                                        monkeypatch):
        # gateway/stub write Claude-format transcripts; an explicit non-codex
        # brain must return [] without ever scanning the codex rollout tree.
        _write_rollout(codex_home, "SID", CODEX_ROWS)
        monkeypatch.setattr(chat_history, "find_claude_transcript", lambda sid: None)

        def _boom(*a, **k):
            raise AssertionError("codex rollout tree must not be walked")

        monkeypatch.setattr(chat_history, "_codex_rollout_path", _boom)
        for brain in ("claude", "gateway", "stub"):
            assert read_transcript_messages("SID", brain=brain) == []

    def test_gateway_openai_reads_codex_rollout(self, codex_home):
        _write_rollout(codex_home, "SID", CODEX_ROWS)

        msgs = read_transcript_messages("SID", brain="gateway-openai")

        assert [m["text"] for m in msgs][-1] == \
            "Yes. Manager is alive and standing by."

    def test_gateway_openai_session_records_brain_kind(self, tmp_path,
                                                       monkeypatch):
        """Gateway sessions keep recording the alias-style provenance label
        (#789 session_brain_label): pre-upgrade records match, and this
        module's format dispatch keeps working unchanged."""
        from bobi import paths
        from bobi.sdk import load_session_brain, save_session_id

        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)
        monkeypatch.setenv("BOBI_BRAIN", "gateway-openai")
        monkeypatch.setenv("BOBI_GATEWAY_BASE_URL", "http://localhost:9000/v1")

        save_session_id("s", "codex-thread-id", root=tmp_path)

        assert load_session_brain("s", root=tmp_path) == "gateway-openai"

    def test_claude_transcript_wins_when_present(self, codex_home, tmp_path,
                                                 monkeypatch):
        # When a claude transcript resolves, codex is never consulted.
        claude = tmp_path / "claude-SID.jsonl"
        _write_jsonl(claude, [
            {"type": "user", "message": {"role": "user", "content": "hi claude"}},
            {"type": "assistant",
             "message": {"role": "assistant", "content": "hello from claude"}},
        ])
        monkeypatch.setattr(chat_history, "find_claude_transcript", lambda sid: claude)
        _write_rollout(codex_home, "SID", CODEX_ROWS)
        msgs = read_transcript_messages("SID")  # brain unspecified
        assert msgs == [
            {"role": "user", "text": "hi claude"},
            {"role": "agent", "text": "hello from claude"},
        ]


# --- load_session_brain -----------------------------------------------------

class TestLoadSessionBrain:
    def test_reads_recorded_brain(self, tmp_path):
        from bobi import paths
        from bobi.sdk import load_session_brain

        (paths.sessions_dir(tmp_path) / "s.brain").write_text("codex\n")
        assert load_session_brain("s", root=tmp_path) == "codex"

    def test_unrecorded_returns_empty(self, tmp_path):
        from bobi import paths
        from bobi.sdk import load_session_brain

        paths.sessions_dir(tmp_path)  # dir exists, no .brain file
        assert load_session_brain("s", root=tmp_path) == ""

    def test_roundtrips_with_save_session_id(self, tmp_path, monkeypatch):
        from bobi.sdk import load_session_brain, save_session_id

        monkeypatch.setenv("BOBI_BRAIN", "codex")
        save_session_id("s", "thread-1", root=tmp_path)
        assert load_session_brain("s", root=tmp_path) == "codex"


# --- find_claude_transcript: the one locator (Q027/Q104) --------------------

class TestFindClaudeTranscript:
    """One locator, called by chat_history, brain.claude and cli.

    Four hand-rolled copies of "where is session <id>.jsonl" collapsed into
    this function; these pin the behaviour every caller now inherits.
    """

    def _seed(self, root: str, session_id: str, tmp_path) -> Path:
        projects = tmp_path / root / "projects" / "some-project"
        projects.mkdir(parents=True)
        target = projects / f"{session_id}.jsonl"
        target.write_text("{}\n")
        return target

    def test_honors_claude_config_dir(self, tmp_path, monkeypatch):
        from bobi.chat_history import find_claude_transcript

        target = self._seed("cfg", "sess-1", tmp_path)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

        assert find_claude_transcript("sess-1") == target

    def test_falls_back_to_home(self, tmp_path, monkeypatch):
        from bobi.chat_history import find_claude_transcript

        target = self._seed("home/.claude", "sess-2", tmp_path)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

        assert find_claude_transcript("sess-2") == target

    def test_config_dir_wins_over_home(self, tmp_path, monkeypatch):
        from bobi.chat_history import find_claude_transcript

        pinned = self._seed("cfg", "dup", tmp_path)
        self._seed("home/.claude", "dup", tmp_path)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

        assert find_claude_transcript("dup") == pinned

    def test_empty_session_id_is_none(self, tmp_path, monkeypatch):
        from bobi.chat_history import find_claude_transcript

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert find_claude_transcript("") is None

    def test_unreadable_root_is_skipped_not_raised(self, tmp_path, monkeypatch):
        """The guard the brain.claude copy carried, kept by the survivor.

        The scan races Claude Code writing the tree; an unreadable root means
        "no transcript here", never a traceback out of a history read.
        """
        from bobi import chat_history

        good = self._seed("home/.claude", "sess-3", tmp_path)
        bad = tmp_path / "cfg" / "projects"
        bad.mkdir(parents=True)

        real_iterdir = Path.iterdir

        def exploding(self):
            if self == bad:
                raise PermissionError(str(self))
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", exploding)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

        assert chat_history.find_claude_transcript("sess-3") == good
