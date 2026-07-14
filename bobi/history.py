"""Conversation history indexer — SQLite + FTS5 over Claude Code JSONL logs.

Reads conversation files from ~/.claude/projects/<project>/<session>.jsonl,
indexes them into SQLite with full-text search, and provides a query API.
Incremental: only processes new lines since last index.
"""

import json
import sqlite3
import time
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
def _db_path() -> Path:
    from bobi import paths
    return paths.state_dir() / "history.db"


def _init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            session_id   TEXT PRIMARY KEY,
            project      TEXT,
            cwd          TEXT,
            git_branch   TEXT,
            started_at   TEXT,
            message_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            type         TEXT NOT NULL,
            role         TEXT,
            content      TEXT,
            tool_name    TEXT,
            tool_input   TEXT,
            timestamp    TEXT,
            line_number  INTEGER,
            FOREIGN KEY (session_id) REFERENCES conversations(session_id)
        );

        CREATE TABLE IF NOT EXISTS index_state (
            file_path    TEXT PRIMARY KEY,
            lines_read   INTEGER DEFAULT 0,
            last_indexed TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            tool_name,
            session_id UNINDEXED,
            msg_id UNINDEXED
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, tool_name, session_id, msg_id)
            VALUES (new.id, new.content, new.tool_name, new.session_id, new.id);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, session_id, msg_id)
            VALUES ('delete', old.id, old.content, old.tool_name, old.session_id, old.id);
        END;
    """)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                parts.append(block.get("thinking", ""))
        return "\n".join(parts)
    return ""


def _extract_tool_calls(content) -> list[dict]:
    if not isinstance(content, list):
        return []
    calls = []
    for block in content:
        if block.get("type") == "tool_use":
            calls.append({
                "name": block.get("name", ""),
                "input": json.dumps(block.get("input", {})),
            })
    return calls


def _is_sleep_cycle_task(text: str) -> bool:
    """True for the out-of-band sleep-cycle task prompt.

    These sessions are distillation machinery. Indexing them feeds the full
    sleep-cycle prompt, rendered transcript delta, and current memory back into
    later sleep-cycle runs.
    """
    stripped = text.lstrip()
    return (
        stripped.startswith("You are the **sleep cycle** for this agent team.")
        or (
            "=== CURRENT long_term_memory.md (rewrite this in full via Write) ===" in stripped
            and "=== NEW TRANSCRIPT DELTA (since your last run) ===" in stripped
        )
    )


def _is_role_startup_prompt(text: str) -> bool:
    """True for framework startup/base prompt injections.

    Persistent agent starts and rotation reconnects send the composed base prompt
    as a giant first user message. It is framework boilerplate already present in
    every prompt, not team activity worth indexing.
    """
    stripped = text.lstrip()
    return (
        stripped.startswith("You are a bobi ")
        and "Act directly using your tools.\n\n# Bobi Agent" in stripped
        and "## Available workflows" in stripped
    )


def _is_sleep_cycle_session_id(session_id: str) -> bool:
    return session_id.startswith("curator-") and session_id.endswith("-curator")


def _sleep_cycle_transcript_ids() -> set[str]:
    """Claude transcript ids for Bobi's sleep-cycle/curator sessions."""
    try:
        from bobi import paths
        sessions = paths.sessions_path()
    except Exception:
        return set()
    try:
        id_files = sessions.glob("curator-*-curator.id")
    except OSError:
        return set()
    ids: set[str] = set()
    for id_file in id_files:
        if not _is_sleep_cycle_session_id(id_file.stem):
            continue
        try:
            session_id = id_file.read_text().strip()
        except OSError:
            continue
        if session_id:
            ids.add(session_id)
    return ids


def _is_sleep_cycle_session(session_id: str, lines: list[str]) -> bool:
    if (
        _is_sleep_cycle_session_id(session_id)
        or session_id in _sleep_cycle_transcript_ids()
    ):
        return True
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") != "user":
            continue
        text = _extract_text(msg.get("message", {}).get("content", ""))
        if _is_sleep_cycle_task(text):
            return True
    return False


def _sleep_cycle_echo_session_sql() -> str:
    return """
        SELECT DISTINCT session_id FROM messages
        WHERE content LIKE 'You are the **sleep cycle** for this agent team.%'
           OR (
             content LIKE '%=== CURRENT long_term_memory.md (rewrite this in full via Write) ===%'
             AND content LIKE '%=== NEW TRANSCRIPT DELTA (since your last run) ===%'
           )
    """


def _project_from_path(file_path: Path) -> str:
    return file_path.parent.name.replace("-", "/", 1).replace("-", "/")


def _index_file(conn: sqlite3.Connection, file_path: Path) -> int:
    session_id = file_path.stem
    state = conn.execute(
        "SELECT lines_read FROM index_state WHERE file_path = ?",
        (str(file_path),),
    ).fetchone()
    skip = state[0] if state else 0

    lines = file_path.read_text().splitlines()
    if _is_sleep_cycle_session(session_id, lines):
        conn.execute("""
            INSERT OR REPLACE INTO index_state (file_path, lines_read, last_indexed)
            VALUES (?, ?, ?)
        """, (str(file_path), len(lines), time.strftime("%Y-%m-%dT%H:%M:%S")))
        return 0

    if len(lines) <= skip:
        return 0

    new_lines = lines[skip:]
    project = _project_from_path(file_path)
    inserted = 0

    conv_exists = conn.execute(
        "SELECT 1 FROM conversations WHERE session_id = ?", (session_id,)
    ).fetchone()

    for i, line in enumerate(new_lines):
        line_num = skip + i + 1
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type", "")
        message = msg.get("message", {})
        role = message.get("role", "")
        raw_content = message.get("content", "")
        timestamp = msg.get("timestamp", "")

        if msg_type == "user" and not conv_exists:
            git_branch = msg.get("gitBranch", "")
            cwd = msg.get("cwd", "")
            conn.execute("""
                INSERT OR REPLACE INTO conversations (session_id, project, cwd, git_branch, started_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, project, cwd, git_branch, timestamp))
            conv_exists = True

        if msg_type not in ("user", "assistant", "system"):
            continue

        text = _extract_text(raw_content)
        tool_calls = _extract_tool_calls(raw_content)

        if text.strip() and not (
            msg_type == "user" and role == "user" and _is_role_startup_prompt(text)
        ):
            conn.execute("""
                INSERT INTO messages (session_id, type, role, content, timestamp, line_number)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, msg_type, role, text, timestamp, line_num))
            inserted += 1

        for tc in tool_calls:
            conn.execute("""
                INSERT INTO messages (session_id, type, role, content, tool_name, tool_input, timestamp, line_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, msg_type, role, "", tc["name"], tc["input"], timestamp, line_num))
            inserted += 1

    conn.execute("""
        INSERT OR REPLACE INTO index_state (file_path, lines_read, last_indexed)
        VALUES (?, ?, ?)
    """, (str(file_path), len(lines), time.strftime("%Y-%m-%dT%H:%M:%S")))

    conn.execute("""
        UPDATE conversations SET message_count = (
            SELECT COUNT(*) FROM messages WHERE session_id = ?
        ) WHERE session_id = ?
    """, (session_id, session_id))

    return inserted


def index(project_filter: str | None = None) -> dict:
    """Index all conversation JSONL files. Returns stats."""
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path()))
    _init_db(conn)

    files = []
    if PROJECTS_DIR.exists():
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            if project_filter and project_filter not in project_dir.name:
                continue
            for f in project_dir.glob("*.jsonl"):
                files.append(f)

    total_new = 0
    files_processed = 0
    for f in files:
        new = _index_file(conn, f)
        if new > 0:
            files_processed += 1
            total_new += new

    conn.commit()

    stats = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    return {
        "files_scanned": len(files),
        "files_with_new": files_processed,
        "new_messages": total_new,
        "total_conversations": stats,
        "total_messages": msg_count,
    }


def _fts_query(query: str) -> str:
    """Convert natural language query to FTS5 syntax. Quotes each token."""
    tokens = query.split()
    quoted = [f'"{t}"' for t in tokens if t]
    return " OR ".join(quoted)


def search(query: str, limit: int = 20, project: str | None = None) -> list[dict]:
    """Full-text search across conversation history."""
    if not _db_path().exists():
        return []

    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row

    fts_query = _fts_query(query)

    sql = """
        SELECT
            m.id, m.session_id, m.type, m.role, m.tool_name, m.timestamp,
            highlight(messages_fts, 0, '>>>', '<<<') AS snippet,
            c.project, c.cwd, c.git_branch,
            rank
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.msg_id
        LEFT JOIN conversations c ON c.session_id = m.session_id
        WHERE messages_fts MATCH ?
    """
    params: list = [fts_query]

    if project:
        sql += " AND c.project LIKE ?"
        params.append(f"%{project}%")

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results


def conversations(limit: int = 20, project: str | None = None) -> list[dict]:
    """List recent conversations."""
    if not _db_path().exists():
        return []

    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row

    sql = "SELECT * FROM conversations"
    params: list = []
    if project:
        sql += " WHERE project LIKE ?"
        params.append(f"%{project}%")
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results


def session_messages(session_id: str) -> list[dict]:
    """Get all messages from a specific session."""
    if not _db_path().exists():
        return []

    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results


def messages_since(cursor: int, limit: int | None = None) -> list[dict]:
    """Return indexed messages with ``id > cursor``, oldest-first by row id.

    The delta the sleep cycle (#456) ingests each run, across *all*
    sessions. Keyed on the table's unique autoincrement ``messages.id`` — NOT a
    message ``timestamp`` (non-unique across tool-call tie-rows, sometimes
    empty) and NOT ``conversations.started_at`` (write-once, so a long-lived
    director/manager would never be re-selected). ``id`` is a true consumption
    watermark: assigned to every row regardless of timestamp value, monotonic,
    and unique — so a budget cut between rows, an empty-timestamp row, or a
    late-indexed old row is still picked up on a later run by ``id > cursor``.

    The caller groups the returned rows by ``session_id`` to reconstruct
    per-session context. ``limit`` bounds the row count (an over-fetch guard);
    the sleep cycle's char/message budget is applied on top of this.
    """
    if not _db_path().exists():
        return []

    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    excluded_session_ids = sorted(_sleep_cycle_transcript_ids())
    sql = """
        SELECT * FROM messages
        WHERE id > ?
          AND session_id NOT LIKE 'curator-%-curator'
          AND session_id NOT IN (""" + _sleep_cycle_echo_session_sql() + """)
          AND content NOT LIKE 'You are the **sleep cycle** for this agent team.%'
          AND NOT (
            type = 'user'
            AND role = 'user'
            AND content LIKE 'You are a bobi %'
            AND content LIKE '%Act directly using your tools.%# Bobi Agent%'
            AND content LIKE '%## Available workflows%'
          )
    """
    params: list = [int(cursor)]
    if excluded_session_ids:
        placeholders = ",".join("?" for _ in excluded_session_ids)
        sql += f" AND session_id NOT IN ({placeholders})"
        params.extend(excluded_session_ids)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results


def context_for_events(events: list[dict], max_results: int = 5) -> str:
    """Search history for context relevant to a batch of events.

    Extracts search terms from event data (issue titles, messages, details)
    and returns formatted context.
    """
    if not _db_path().exists():
        return ""

    queries = set()
    for e in events:
        data = e.get("data", {})
        if data.get("title"):
            queries.add(data["title"])
        if data.get("run_key"):
            queries.add(data["run_key"])
        if data.get("text"):
            text = data["text"]
            if len(text) > 200:
                text = text[:200]
            queries.add(text)

    if not queries:
        return ""

    seen_ids = set()
    results = []
    for q in queries:
        for r in search(q, limit=3):
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                results.append(r)

    if not results:
        return ""

    results = results[:max_results]
    lines = ["## Prior conversation context (from history search)", ""]
    for r in results:
        role = r.get("role") or r.get("type") or ""
        tool = f" [{r['tool_name']}]" if r.get("tool_name") else ""
        snippet = (r.get("snippet") or "")[:300].replace("\n", " ")
        branch = r.get("git_branch") or ""
        cwd = r.get("cwd") or ""
        lines.append(f"- **{role}{tool}** ({r['timestamp'][:19]}, {branch}, {cwd})")
        lines.append(f"  {snippet}")
        lines.append("")

    return "\n".join(lines)


def start_background_indexer(interval: int = 120):
    """Start a background thread that re-indexes every `interval` seconds."""
    import logging
    import threading

    log = logging.getLogger(__name__)

    def _loop():
        while True:
            try:
                stats = index()
                if stats["new_messages"] > 0:
                    log.debug(f"History indexer: +{stats['new_messages']} messages")
            except Exception as e:
                log.warning(f"History indexer error: {e}")
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="history-indexer")
    t.start()
    return t
