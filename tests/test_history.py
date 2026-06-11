"""Unit tests for the conversation history indexer — extraction, indexing,
querying, and incremental state tracking."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from modastack import history
from modastack.history import (
    _extract_text,
    _extract_tool_calls,
    _fts_query,
    _index_file,
    _init_db,
    _project_from_path,
    context_for_events,
    conversations,
    index,
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
