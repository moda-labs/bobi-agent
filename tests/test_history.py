"""Unit tests for the conversation history indexer — extraction, indexing,
querying, and incremental state tracking."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from bobi import history, paths
from bobi.history import (
    _extract_text,
    _extract_tool_calls,
    _fts_query,
    _index_file,
    _init_db,
    _project_from_path,
    context_for_events,
    conversations,
    index,
    messages_since,
    search,
    session_messages,
)


@pytest.fixture
def db(tmp_path):
    """In-memory SQLite connection with the history schema initialized."""
    conn = sqlite3.connect(":memory:")
    _init_db(conn)
    return conn


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """Redirect PROJECTS_DIR and DB_PATH for test isolation."""
    pdir = tmp_path / "projects"
    pdir.mkdir()
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(history, "PROJECTS_DIR", pdir)
    monkeypatch.setattr(history, "_db_path", lambda: db_path)
    return pdir


def _write_jsonl(path: Path, lines: list[dict]):
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def _user_msg(text: str, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def _assistant_msg(text: str, **extra) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": text}, **extra}


def _tool_msg(blocks: list[dict], **extra) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}, **extra}


def _role_startup_prompt(role: str = "gtm-director") -> str:
    return (
        f"You are a bobi {role} for gtm-team. Act directly using your tools.\n\n"
        "# Bobi Agent\n\n"
        "Shared framework instructions that are already present in every prompt.\n\n"
        "## Long-Term Memory\n\n"
        "Injected durable memory.\n\n"
        "## Available workflows\n\n"
        "- adhoc"
    )


def _sleep_cycle_rendered_task(prompt: str = "Custom team memory distillation prompt.") -> str:
    return (
        f"{prompt}\n\n"
        "=== CURRENT long_term_memory.md (rewrite this in full via Write) ===\n"
        "existing memory\n\n"
        "=== NEW TRANSCRIPT DELTA (since your last run) ===\n"
        "copied team transcript"
    )


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_plain_string(self):
        assert _extract_text("hello world") == "hello world"

    def test_text_blocks(self):
        content = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
        assert _extract_text(content) == "first\nsecond"

    def test_thinking_blocks(self):
        content = [
            {"type": "thinking", "thinking": "I should check"},
            {"type": "text", "text": "Here's the answer"},
        ]
        assert _extract_text(content) == "I should check\nHere's the answer"

    def test_ignores_unknown_block_types(self):
        content = [
            {"type": "text", "text": "keep"},
            {"type": "tool_use", "name": "read"},
        ]
        assert _extract_text(content) == "keep"

    def test_empty_list(self):
        assert _extract_text([]) == ""

    def test_none_returns_empty(self):
        assert _extract_text(None) == ""

    def test_int_returns_empty(self):
        assert _extract_text(42) == ""

    def test_missing_text_key(self):
        content = [{"type": "text"}]
        assert _extract_text(content) == ""


# ---------------------------------------------------------------------------
# _extract_tool_calls
# ---------------------------------------------------------------------------

class TestExtractToolCalls:
    def test_extracts_tool_use(self):
        content = [
            {"type": "tool_use", "name": "Read", "input": {"path": "/a.py"}},
            {"type": "text", "text": "ignored"},
            {"type": "tool_use", "name": "Edit", "input": {"file": "/b.py"}},
        ]
        calls = _extract_tool_calls(content)
        assert len(calls) == 2
        assert calls[0]["name"] == "Read"
        assert json.loads(calls[0]["input"]) == {"path": "/a.py"}
        assert calls[1]["name"] == "Edit"

    def test_no_tool_calls(self):
        content = [{"type": "text", "text": "no tools"}]
        assert _extract_tool_calls(content) == []

    def test_not_a_list(self):
        assert _extract_tool_calls("just text") == []
        assert _extract_tool_calls(None) == []

    def test_missing_name_and_input(self):
        content = [{"type": "tool_use"}]
        calls = _extract_tool_calls(content)
        assert len(calls) == 1
        assert calls[0]["name"] == ""
        assert json.loads(calls[0]["input"]) == {}


# ---------------------------------------------------------------------------
# _project_from_path
# ---------------------------------------------------------------------------

class TestProjectFromPath:
    def test_dashed_path(self):
        p = Path("/home/user/.claude/projects/-Users-alice-dev-myproject/session.jsonl")
        assert _project_from_path(p) == "/Users/alice/dev/myproject"

    def test_single_segment(self):
        p = Path("/a/b/simple/file.jsonl")
        assert _project_from_path(p) == "simple"


# ---------------------------------------------------------------------------
# _fts_query
# ---------------------------------------------------------------------------

class TestFtsQuery:
    def test_single_word(self):
        assert _fts_query("hello") == '"hello"'

    def test_multiple_words(self):
        result = _fts_query("find the bug")
        assert result == '"find" OR "the" OR "bug"'

    def test_empty_string(self):
        assert _fts_query("") == ""

    def test_extra_whitespace(self):
        result = _fts_query("  a   b  ")
        assert '"a"' in result
        assert '"b"' in result


# ---------------------------------------------------------------------------
# _init_db — schema creation
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_tables(self, db):
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "conversations" in tables
        assert "messages" in tables
        assert "index_state" in tables
        assert "messages_fts" in tables

    def test_creates_triggers(self, db):
        triggers = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()}
        assert "messages_ai" in triggers
        assert "messages_ad" in triggers

    def test_idempotent(self, db):
        _init_db(db)
        _init_db(db)
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len([t for t in tables if t[0] == "conversations"]) == 1


# ---------------------------------------------------------------------------
# _index_file — incremental indexing
# ---------------------------------------------------------------------------

class TestIndexFile:
    def test_indexes_messages(self, db, tmp_path):
        f = tmp_path / "projects" / "proj" / "sess1.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg("hello", timestamp="2024-01-01T00:00:00", cwd="/dev", gitBranch="main"),
            _assistant_msg("hi there", timestamp="2024-01-01T00:00:01"),
        ])
        count = _index_file(db, f)
        assert count == 2

        msgs = db.execute("SELECT * FROM messages ORDER BY id").fetchall()
        assert len(msgs) == 2

    def test_creates_conversation_record(self, db, tmp_path):
        f = tmp_path / "proj" / "sess2.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg("hi", timestamp="2024-01-01T00:00:00", cwd="/code", gitBranch="feature"),
        ])
        _index_file(db, f)
        conv = db.execute("SELECT * FROM conversations WHERE session_id = 'sess2'").fetchone()
        assert conv is not None

    def test_incremental_skips_already_indexed(self, db, tmp_path):
        f = tmp_path / "proj" / "sess3.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [_user_msg("first", timestamp="2024-01-01T00:00:00")])

        count1 = _index_file(db, f)
        assert count1 == 1

        count2 = _index_file(db, f)
        assert count2 == 0

    def test_incremental_picks_up_new_lines(self, db, tmp_path):
        f = tmp_path / "proj" / "sess4.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [_user_msg("first", timestamp="2024-01-01T00:00:00")])
        _index_file(db, f)

        lines = [
            json.dumps(_user_msg("first", timestamp="2024-01-01T00:00:00")),
            json.dumps(_assistant_msg("second", timestamp="2024-01-01T00:00:01")),
        ]
        f.write_text("\n".join(lines) + "\n")

        count = _index_file(db, f)
        assert count == 1

    def test_skips_invalid_json(self, db, tmp_path):
        f = tmp_path / "proj" / "sess5.jsonl"
        f.parent.mkdir(parents=True)
        content = json.dumps(_user_msg("good", timestamp="2024-01-01T00:00:00"))
        f.write_text(content + "\n{bad json}\n")

        count = _index_file(db, f)
        assert count == 1

    def test_skips_non_message_types(self, db, tmp_path):
        f = tmp_path / "proj" / "sess6.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            {"type": "init", "message": {}},
            _user_msg("real message", timestamp="2024-01-01T00:00:00"),
            {"type": "result", "message": {}},
        ])
        count = _index_file(db, f)
        assert count == 1

    def test_indexes_tool_calls(self, db, tmp_path):
        f = tmp_path / "proj" / "sess7.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg("check this", timestamp="2024-01-01T00:00:00"),
            _tool_msg([
                {"type": "tool_use", "name": "Read", "input": {"path": "/a"}},
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}},
            ], timestamp="2024-01-01T00:00:01"),
        ])
        count = _index_file(db, f)
        assert count == 3  # 1 user text + 2 tool calls

        tools = db.execute(
            "SELECT tool_name FROM messages WHERE tool_name IS NOT NULL ORDER BY id"
        ).fetchall()
        assert [t[0] for t in tools] == ["Read", "Grep"]

    def test_updates_message_count(self, db, tmp_path):
        f = tmp_path / "proj" / "sess8.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg("a", timestamp="2024-01-01T00:00:00"),
            _assistant_msg("b", timestamp="2024-01-01T00:00:01"),
        ])
        _index_file(db, f)
        count = db.execute(
            "SELECT message_count FROM conversations WHERE session_id = 'sess8'"
        ).fetchone()[0]
        assert count == 2

    def test_skips_empty_content(self, db, tmp_path):
        f = tmp_path / "proj" / "sess9.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            {"type": "user", "message": {"role": "user", "content": ""}, "timestamp": "2024-01-01T00:00:00"},
            {"type": "assistant", "message": {"role": "assistant", "content": "   "}, "timestamp": "2024-01-01T00:00:01"},
        ])
        count = _index_file(db, f)
        assert count == 0

    def test_skips_sleep_cycle_harness_session(self, db, tmp_path):
        f = tmp_path / "proj" / "curator-sleep-cycle-curator.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg(
                "You are the **sleep cycle** for this agent team.\n\n"
                "## CURRENT `long_term_memory.md`\n\n"
                "existing memory\n\n"
                "## NEW TRANSCRIPT DELTA\n\n"
                "real team transcript copied into the task",
                timestamp="2026-07-14T00:00:00",
                cwd="/run",
                gitBranch="main",
            ),
            _assistant_msg(
                '{"success": true, "updated": false, "summary": "no durable changes"}',
                timestamp="2026-07-14T00:00:01",
            ),
        ])

        count = _index_file(db, f)

        assert count == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert db.execute(
            "SELECT lines_read FROM index_state WHERE file_path = ?", (str(f),)
        ).fetchone()[0] == 2

    def test_sleep_cycle_harness_session_stays_skipped_incrementally(self, db, tmp_path):
        f = tmp_path / "proj" / "curator-sleep-cycle-curator.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg("You are the **sleep cycle** for this agent team.", timestamp="2026-07-14T00:00:00"),
        ])
        assert _index_file(db, f) == 0

        with f.open("a") as fh:
            fh.write(json.dumps(_assistant_msg("later summary", timestamp="2026-07-14T00:00:01")) + "\n")

        assert _index_file(db, f) == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute(
            "SELECT lines_read FROM index_state WHERE file_path = ?", (str(f),)
        ).fetchone()[0] == 2

    def test_skips_curator_session_with_custom_sleep_cycle_prompt(self, db, tmp_path):
        f = tmp_path / "proj" / "curator-curator-deadbeef-curator.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg(
                _sleep_cycle_rendered_task(),
                timestamp="2026-07-14T00:00:00",
            ),
            _assistant_msg("custom summary", timestamp="2026-07-14T00:00:01"),
        ])

        assert _index_file(db, f) == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute(
            "SELECT lines_read FROM index_state WHERE file_path = ?", (str(f),)
        ).fetchone()[0] == 2

    def test_skips_mapped_claude_transcript_for_custom_sleep_cycle_prompt(
        self, db, tmp_path, monkeypatch
    ):
        state = tmp_path / "state"
        sessions = state / "sessions"
        sessions.mkdir(parents=True)
        monkeypatch.setattr(paths, "state_path", lambda *a, **k: state)
        (sessions / "curator-curator-deadbeef-curator.id").write_text(
            "claude-session-uuid"
        )

        f = tmp_path / "proj" / "claude-session-uuid.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg(
                _sleep_cycle_rendered_task(),
                timestamp="2026-07-14T00:00:00",
            ),
            _assistant_msg("custom summary", timestamp="2026-07-14T00:00:01"),
        ])

        assert _index_file(db, f) == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute(
            "SELECT lines_read FROM index_state WHERE file_path = ?", (str(f),)
        ).fetchone()[0] == 2

    def test_skips_sleep_cycle_session_when_first_user_line_is_preamble(
        self, db, tmp_path
    ):
        f = tmp_path / "proj" / "claude-session-with-preamble.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg("transcript preamble", timestamp="2026-07-14T00:00:00"),
            _assistant_msg("prelude", timestamp="2026-07-14T00:00:01"),
            _user_msg(
                "You are the **sleep cycle** for this agent team.\n\n"
                "=== NEW TRANSCRIPT DELTA (since your last run) ===\n"
                "copied team transcript",
                timestamp="2026-07-14T00:00:02",
            ),
            _assistant_msg("custom summary", timestamp="2026-07-14T00:00:03"),
        ])

        assert _index_file(db, f) == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute(
            "SELECT lines_read FROM index_state WHERE file_path = ?", (str(f),)
        ).fetchone()[0] == 4

    def test_skips_rotation_base_prompt_reinjection(self, db, tmp_path):
        f = tmp_path / "proj" / "director-rotation.jsonl"
        f.parent.mkdir(parents=True)
        _write_jsonl(f, [
            _user_msg(
                _role_startup_prompt(),
                timestamp="2026-07-14T00:00:00",
                cwd="/run",
                gitBranch="main",
            ),
            _assistant_msg("ready", timestamp="2026-07-14T00:00:01"),
            _user_msg("Human asked the team to remember the deploy command.", timestamp="2026-07-14T00:00:02"),
        ])

        count = _index_file(db, f)

        assert count == 2
        contents = [
            r[0] for r in db.execute(
                "SELECT content FROM messages ORDER BY id"
            ).fetchall()
        ]
        assert contents == ["ready", "Human asked the team to remember the deploy command."]
        assert _role_startup_prompt() not in contents


# ---------------------------------------------------------------------------
# FTS5 trigger integration
# ---------------------------------------------------------------------------

class TestFtsTriggers:
    def test_insert_populates_fts(self, db):
        db.execute("""
            INSERT INTO messages (session_id, type, role, content, timestamp, line_number)
            VALUES ('s1', 'user', 'user', 'hello world', '2024-01-01', 1)
        """)
        fts = db.execute(
            "SELECT * FROM messages_fts WHERE messages_fts MATCH '\"hello\"'"
        ).fetchall()
        assert len(fts) == 1

    def test_fts_search_by_tool_name(self, db):
        db.execute("""
            INSERT INTO messages (session_id, type, role, content, tool_name, timestamp, line_number)
            VALUES ('s1', 'assistant', 'assistant', '', 'ReadFile', '2024-01-01', 1)
        """)
        fts = db.execute(
            "SELECT * FROM messages_fts WHERE messages_fts MATCH '\"ReadFile\"'"
        ).fetchall()
        assert len(fts) == 1


# ---------------------------------------------------------------------------
# index() — top-level orchestrator
# ---------------------------------------------------------------------------

class TestIndex:
    def test_indexes_project_files(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev-repo"
        proj.mkdir()
        _write_jsonl(proj / "session1.jsonl", [
            _user_msg("hello", timestamp="2024-01-01T00:00:00"),
        ])
        stats = index()
        assert stats["files_scanned"] == 1
        assert stats["new_messages"] == 1
        assert stats["total_conversations"] == 1

    def test_project_filter(self, projects_dir):
        p1 = projects_dir / "-Users-alice-dev-repo-a"
        p1.mkdir()
        _write_jsonl(p1 / "s1.jsonl", [_user_msg("a", timestamp="2024-01-01T00:00:00")])

        p2 = projects_dir / "-Users-alice-dev-repo-b"
        p2.mkdir()
        _write_jsonl(p2 / "s2.jsonl", [_user_msg("b", timestamp="2024-01-01T00:00:00")])

        stats = index(project_filter="repo-a")
        assert stats["files_scanned"] == 1
        assert stats["new_messages"] == 1

    def test_incremental(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [_user_msg("first", timestamp="2024-01-01T00:00:00")])

        stats1 = index()
        assert stats1["new_messages"] == 1

        stats2 = index()
        assert stats2["new_messages"] == 0
        assert stats2["files_with_new"] == 0

    def test_empty_projects_dir(self, projects_dir):
        stats = index()
        assert stats["files_scanned"] == 0
        assert stats["new_messages"] == 0

    def test_missing_projects_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "PROJECTS_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr(history, "_db_path", lambda: tmp_path / "history.db")
        stats = index()
        assert stats["files_scanned"] == 0


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------

class TestSearch:
    def test_finds_indexed_content(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [
            _user_msg("fix the authentication bug", timestamp="2024-01-01T00:00:00"),
            _assistant_msg("I found the issue in auth.py", timestamp="2024-01-01T00:00:01"),
        ])
        index()

        results = search("authentication")
        assert len(results) >= 1
        assert any("authentication" in r.get("snippet", "").lower()
                    or "authentication" in (r.get("content", "") or "").lower()
                    for r in results)

    def test_returns_empty_when_no_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "_db_path", lambda: tmp_path / "nonexistent.db")
        assert search("anything") == []

    def test_project_filter(self, projects_dir):
        p1 = projects_dir / "-Users-alice-dev-proj-a"
        p1.mkdir()
        _write_jsonl(p1 / "s1.jsonl", [
            _user_msg("unique_target_word", timestamp="2024-01-01T00:00:00"),
        ])

        p2 = projects_dir / "-Users-alice-dev-proj-b"
        p2.mkdir()
        _write_jsonl(p2 / "s2.jsonl", [
            _user_msg("unique_target_word", timestamp="2024-01-01T00:00:00"),
        ])
        index()

        all_results = search("unique_target_word")
        assert len(all_results) == 2

        filtered = search("unique_target_word", project="proj/a")
        assert len(filtered) == 1

    def test_limit(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        messages = [_user_msg(f"searchable message {i}", timestamp=f"2024-01-01T00:00:{i:02d}")
                    for i in range(10)]
        _write_jsonl(proj / "s1.jsonl", messages)
        index()

        results = search("searchable", limit=3)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# conversations()
# ---------------------------------------------------------------------------

class TestConversations:
    def test_lists_conversations(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [
            _user_msg("a", timestamp="2024-01-01T00:00:00", cwd="/dev", gitBranch="main"),
        ])
        _write_jsonl(proj / "s2.jsonl", [
            _user_msg("b", timestamp="2024-01-02T00:00:00", cwd="/dev", gitBranch="feature"),
        ])
        index()

        convos = conversations()
        assert len(convos) == 2

    def test_project_filter(self, projects_dir):
        p1 = projects_dir / "-Users-alice-proj-x"
        p1.mkdir()
        _write_jsonl(p1 / "s1.jsonl", [_user_msg("a", timestamp="2024-01-01T00:00:00")])

        p2 = projects_dir / "-Users-alice-proj-y"
        p2.mkdir()
        _write_jsonl(p2 / "s2.jsonl", [_user_msg("b", timestamp="2024-01-01T00:00:00")])
        index()

        filtered = conversations(project="proj/x")
        assert len(filtered) == 1

    def test_returns_empty_when_no_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "_db_path", lambda: tmp_path / "nonexistent.db")
        assert conversations() == []

    def test_limit(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        for i in range(5):
            _write_jsonl(proj / f"s{i}.jsonl", [
                _user_msg("msg", timestamp=f"2024-01-0{i+1}T00:00:00"),
            ])
        index()
        assert len(conversations(limit=2)) == 2


# ---------------------------------------------------------------------------
# session_messages()
# ---------------------------------------------------------------------------

class TestSessionMessages:
    def test_retrieves_messages(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "target.jsonl", [
            _user_msg("question", timestamp="2024-01-01T00:00:00"),
            _assistant_msg("answer", timestamp="2024-01-01T00:00:01"),
        ])
        index()

        msgs = session_messages("target")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_no_db_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "_db_path", lambda: tmp_path / "nonexistent.db")
        assert session_messages("anything") == []

    def test_unknown_session(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [_user_msg("a", timestamp="2024-01-01T00:00:00")])
        index()
        assert session_messages("nonexistent") == []


# ---------------------------------------------------------------------------
# context_for_events()
# ---------------------------------------------------------------------------

class TestContextForEvents:
    def test_returns_context_for_matching_events(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [
            _user_msg("fix the login authentication flow", timestamp="2024-01-01T00:00:00"),
        ])
        index()

        events = [{"data": {"title": "login authentication"}}]
        ctx = context_for_events(events)
        assert "Prior conversation context" in ctx
        assert "login" in ctx.lower() or "authentication" in ctx.lower()

    def test_returns_empty_for_no_matches(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [
            _user_msg("hello world", timestamp="2024-01-01T00:00:00"),
        ])
        index()

        events = [{"data": {"title": "zzz_nonexistent_term_zzz"}}]
        ctx = context_for_events(events)
        assert ctx == ""

    def test_empty_events(self, projects_dir):
        assert context_for_events([]) == ""

    def test_no_db_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "_db_path", lambda: tmp_path / "nonexistent.db")
        assert context_for_events([{"data": {"title": "test"}}]) == ""

    def test_extracts_issue_id(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [
            _user_msg("working on BET-42 ticket", timestamp="2024-01-01T00:00:00"),
        ])
        index()

        events = [{"data": {"run_key": "BET-42"}}]
        ctx = context_for_events(events)
        assert "BET-42" in ctx

    def test_truncates_long_text(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [
            _user_msg("unique_marker " + "x" * 300, timestamp="2024-01-01T00:00:00"),
        ])
        index()

        events = [{"data": {"text": "unique_marker " + "x" * 300}}]
        ctx = context_for_events(events)
        assert "unique_marker" in ctx

    def test_deduplicates_results(self, projects_dir):
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        _write_jsonl(proj / "s1.jsonl", [
            _user_msg("dedupe_target_word", timestamp="2024-01-01T00:00:00"),
        ])
        index()

        events = [
            {"data": {"title": "dedupe_target_word"}},
            {"data": {"text": "dedupe_target_word"}},
        ]
        ctx = context_for_events(events)
        assert ctx.count("dedupe_target_word") == 1


# ---------------------------------------------------------------------------
# messages_since — the sleep-cycle delta cursor (#456)
# ---------------------------------------------------------------------------

class TestMessagesSince:
    """Window on messages.id, not conversations.started_at and not timestamp.

    These are the R2/R3/R4 regressions: the sleep_cycle must re-select a long-lived
    session's new messages, and an id cursor must be skip-free where a timestamp
    cursor was not (tool-call tie-rows + empty-timestamp rows)."""

    def test_empty_when_no_db(self, projects_dir):
        # No index() run yet → no db file → empty, never raises.
        assert messages_since(0) == []

    def test_includes_new_messages_of_a_long_lived_session(self, projects_dir):
        # A persistent session started long ago, plus a fresh ephemeral one.
        proj = projects_dir / "-Users-alice-dev"
        proj.mkdir()
        persistent = proj / "director-persistent.jsonl"
        _write_jsonl(persistent, [
            _user_msg("old kickoff", timestamp="2020-01-01T00:00:00"),
        ])
        index()

        # Cursor = everything indexed so far (the director's old started_at row).
        cursor = max(m["id"] for m in session_messages("director-persistent"))

        # The persistent session accrues NEW messages; a fresh session appears.
        with persistent.open("a") as f:
            f.write(json.dumps(_assistant_msg(
                "fresh durable learning", timestamp="2026-06-24T10:00:00")) + "\n")
        eph = proj / "ephemeral.jsonl"
        _write_jsonl(eph, [
            _user_msg("ephemeral task", timestamp="2026-06-24T11:00:00"),
        ])
        index()

        delta = messages_since(cursor)
        sids = {m["session_id"] for m in delta}
        # Windowing on messages.id picks up the persistent session's new rows —
        # a started_at>cursor window would exclude it forever (write-once).
        assert "director-persistent" in sids
        assert "ephemeral" in sids
        assert any("fresh durable learning" in (m["content"] or "") for m in delta)
        # The old kickoff row (<= cursor) is NOT re-read.
        assert all(m["id"] > cursor for m in delta)

    def test_id_cursor_selects_tie_rows_and_empty_timestamp(self, projects_dir):
        # A tool-using turn writes a text row + tool-call sibling rows ALL at the
        # identical timestamp, plus a row with an empty/absent timestamp. Under a
        # `timestamp > cursor` window a tie-sibling and the empty-ts row would be
        # skipped forever; the id cursor must select every one.
        proj = projects_dir / "-Users-bob-dev"
        proj.mkdir()
        T = "2026-06-24T00:00:00"
        _write_jsonl(proj / "tooluse.jsonl", [
            _user_msg("kick off", timestamp=T),
            _tool_msg([
                {"type": "text", "text": "calling tools"},
                {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
                {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
            ], timestamp=T),
            # No timestamp key at all → stored as "" (sorts below any real ts).
            _assistant_msg("no-timestamp tail"),
        ])
        index()

        rows = session_messages("tooluse")
        # The tie group: the text row + its two tool siblings share timestamp T.
        tie_ts = [r for r in rows if r["timestamp"] == T and r["type"] == "assistant"]
        assert len(tie_ts) >= 3  # text + 2 tool-call rows, all at T
        empty_ts = [r for r in rows if r["timestamp"] == ""]
        assert empty_ts and empty_ts[0]["content"] == "no-timestamp tail"

        # Cursor just below the first tie-row id: a timestamp cursor (= T) would
        # drop the sibling tool rows (== T, not > T) and the ""-ts row (never > T).
        first_tie_id = min(r["id"] for r in tie_ts)
        cursor = first_tie_id - 1

        delta_ids = {m["id"] for m in messages_since(cursor)}
        # Every tie-sibling AND the empty-timestamp row is selected by id > cursor.
        for r in tie_ts:
            assert r["id"] in delta_ids
        assert empty_ts[0]["id"] in delta_ids

    def test_oldest_first_and_limit(self, projects_dir):
        proj = projects_dir / "-Users-c-dev"
        proj.mkdir()
        _write_jsonl(proj / "s.jsonl", [
            _user_msg(f"m{i}", timestamp=f"2026-06-24T00:0{i}:00") for i in range(5)
        ])
        index()
        delta = messages_since(0)
        ids = [m["id"] for m in delta]
        assert ids == sorted(ids)  # oldest-first by id
        assert len(messages_since(0, limit=2)) == 2

    def test_excludes_previously_indexed_sleep_cycle_and_startup_echoes(
        self, projects_dir, tmp_path, monkeypatch
    ):
        state = tmp_path / "state"
        sessions = state / "sessions"
        sessions.mkdir(parents=True)
        monkeypatch.setattr(paths, "state_path", lambda *a, **k: state)
        (sessions / "curator-curator-oldhash-curator.id").write_text(
            "claude-old-sleep-cycle"
        )

        proj = projects_dir / "-Users-d-dev"
        proj.mkdir()
        _write_jsonl(proj / "normal.jsonl", [
            _user_msg("real team signal", timestamp="2026-07-14T00:00:00"),
        ])
        index()

        db_path = history._db_path()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO messages (session_id, type, role, content, timestamp, line_number)
            VALUES ('claude-old-sleep-cycle', 'user', 'user',
                    'Custom old sleep-cycle prompt with transcript delta',
                    '2026-07-14T00:00:01', 1)
        """)
        conn.execute("""
            INSERT INTO messages (session_id, type, role, content, timestamp, line_number)
            VALUES ('claude-unmapped-default-sleep-cycle', 'user', 'user',
                    'You are the **sleep cycle** for this agent team. old prompt',
                    '2026-07-14T00:00:02', 1)
        """)
        conn.execute("""
            INSERT INTO messages (session_id, type, role, content, timestamp, line_number)
            VALUES ('claude-unmapped-default-sleep-cycle', 'assistant', 'assistant',
                    'sleep-cycle assistant summary',
                    '2026-07-14T00:00:03', 2)
        """)
        conn.execute("""
            INSERT INTO messages (session_id, type, role, content, tool_name,
                                  tool_input, timestamp, line_number)
            VALUES ('claude-unmapped-default-sleep-cycle', 'assistant', 'assistant',
                    '', 'Write', '{"file_path": "long_term_memory.md"}',
                    '2026-07-14T00:00:03', 2)
        """)
        conn.execute("""
            INSERT INTO messages (session_id, type, role, content, timestamp, line_number)
            VALUES ('claude-unmapped-custom-sleep-cycle', 'user', 'user', ?,
                    '2026-07-14T00:00:04', 1)
        """, (_sleep_cycle_rendered_task(),))
        conn.execute("""
            INSERT INTO messages (session_id, type, role, content, timestamp, line_number)
            VALUES ('claude-unmapped-custom-sleep-cycle', 'assistant', 'assistant',
                    'custom sleep-cycle assistant summary',
                    '2026-07-14T00:00:05', 2)
        """)
        conn.execute("""
            INSERT INTO messages (session_id, type, role, content, timestamp, line_number)
            VALUES ('director-rotated', 'user', 'user', ?,
                    '2026-07-14T00:00:06', 1)
        """, (_role_startup_prompt(),))
        conn.commit()
        conn.close()

        delta = messages_since(0)

        assert [m["content"] for m in delta] == ["real team signal"]
