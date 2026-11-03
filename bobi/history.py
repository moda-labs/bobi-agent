"""Conversation history indexer — SQLite + FTS5 over Claude Code JSONL logs.

Reads conversation files from <projects-root>/<project>/<session>.jsonl,
indexes them into SQLite with full-text search, and provides a query API.
Incremental: only processes new lines since last index.

The projects roots are ``chat_history.claude_projects_dirs`` — a
``CLAUDE_CONFIG_DIR`` root when one is set, then ``~/.claude/projects`` —
so the index covers the same transcripts the replay path reads.
"""

import json
import sqlite3
from pathlib import Path

from bobi.chat_history import claude_projects_dirs, project_slug
from bobi.fsutil import atomic_write_text, file_lock
from bobi.timeutil import now_iso


def _projects_dirs() -> list[Path]:
    """The transcript roots to index, most specific first.

    Delegates to ``chat_history.claude_projects_dirs`` — the house rule — so
    the FTS index and the sleep-cycle delta see exactly the roots the replay
    path does. This module used to hardcode ``~/.claude/projects``, which
    silently indexed nothing for an agent whose brain runs under a
    ``CLAUDE_CONFIG_DIR`` (bobi renders per-team CLAUDE.md there, #779).
    """
    return claude_projects_dirs()


def _db_path() -> Path:
    from bobi import paths
    return paths.state_dir() / "history.db"


def _owned_path() -> Path:
    return _db_path().with_name("owned_transcripts.txt")


def _render_owned(session_ids: set[str]) -> str:
    return "".join(f"{session_id}\n" for session_id in sorted(session_ids))


def _read_owned() -> set[str] | None:
    try:
        return set(_owned_path().read_text().split())
    except FileNotFoundError:
        return None


def _owned_sessions() -> set[str]:
    """The Claude session ids this agent ran itself (#1142).

    The projects tree belongs to the OS user, not the agent, so agents sharing a
    user share it. A path cannot separate them either: a worker runs in the repo
    it works on, which another agent or a person may also be working in. Only
    the id the brain reports to the session that launched it is exact.

    Reading never writes. Only the agent's own claim creates the file, so a read
    command run as another OS user cannot leave a lock the agent cannot open.
    """
    owned = _read_owned()
    return _seed_owned_sessions() if owned is None else owned


def _owned_json() -> str:
    """The owned ids as a ``json_each`` parameter: a set can exceed SQLite's
    bound-variable limit."""
    return json.dumps(list(_owned_sessions()))


def _transcript_cwd(path: Path) -> str:
    """The first working directory a transcript records, or ""."""
    try:
        with path.open() as f:
            for line in f:
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if cwd:
                    return cwd
    except (OSError, ValueError):
        pass
    return ""


def _seed_owned_sessions() -> set[str]:
    """Ownership for sessions that predate the record.

    The session registry's ids are exact, so they are always claimed, as are
    transcripts started in the runtime root: nobody else works there. A
    ``history.db`` that already exists was indexed before the record did, and
    past ids were overwritten on rotation, so its rows are also claimed by
    working directory: every cwd the registry remembers. That is coarser than
    the record: a session another agent or a person ran in one of those
    directories is claimed too, and stays claimed if it keeps running. An agent
    with no database claims no such rows, or a new agent in a shared repo would
    inherit everyone's sessions.

    Directories compare exactly. Claude's project-directory name cannot be used:
    it maps ``eng_team`` and ``eng-team`` to the same name.
    """
    from bobi.sdk import SessionRegistry

    state = _db_path().parent
    root = str(state.parent)
    owned: set[str] = set()
    cwds = {root}
    for id_file in state.glob("sessions/*.id"):
        try:
            owned.update(id_file.read_text().split())
        except (OSError, ValueError):
            continue
    for entry in SessionRegistry(state.parent).list_all():
        if entry.session_id and isinstance(entry.session_id, str):
            owned.add(entry.session_id)
        if entry.cwd and isinstance(entry.cwd, str):
            # Claude records the resolved path; the registry may hold a symlink.
            cwds.update((entry.cwd, str(Path(entry.cwd).resolve())))

    for projects in _projects_dirs():
        owned.update(f.stem for f in (projects / project_slug(root)).glob("*.jsonl")
                     if _transcript_cwd(f) == root)

    if _db_path().exists():
        conn = sqlite3.connect(str(_db_path()))
        _init_db(conn)
        rows = conn.execute("SELECT session_id, cwd FROM conversations").fetchall()
        conn.close()
        owned.update(sid for sid, cwd in rows if cwd in cwds)
    return owned


def record_owned_session(session_id: str) -> None:
    """Claim *session_id* for this agent, so its transcript gets indexed.

    The first claim also saves the seed. The sync matters because a file that
    comes back empty after a crash would read as "this agent owns nothing".
    """
    if not session_id or session_id in (_read_owned() or ()):
        return
    path = _owned_path()
    with file_lock(path):
        atomic_write_text(
            path, _render_owned(_owned_sessions() | {session_id}), fsync=True)


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

        DROP TRIGGER IF EXISTS messages_ad;
        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.id;
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
    """Best-effort project path from the transcript's directory name.

    Claude encodes a cwd as its directory name by replacing every '/' with
    '-', which is LOSSY: '-home-z-dev-bobi-agent' is equally a decode of
    '/home/z/dev/bobi-agent' and of '/home/z/dev/bobi/agent'. Nothing here
    can tell them apart, so this is only the fallback — `_index_file` prefers
    the real cwd the transcript itself carries (D069).
    """
    return file_path.parent.name.replace("-", "/")


def _lines_consumed(lines: list[str]) -> int:
    """How many of *lines* are safe to record as read.

    The indexer runs while Claude Code is mid-append, so the final line can be
    half-written. Counting it as consumed means the writer's completion of that
    same line is never re-read and the message is lost from the index — and
    from the sleep cycle's transcript delta — permanently (D022). A trailing
    line that does not parse is therefore held back; a complete one that is
    genuinely garbage costs only a re-parse on the next pass, never a
    duplicate insert.
    """
    if lines and not _parses_as_json(lines[-1]):
        return len(lines) - 1
    return len(lines)


def _parses_as_json(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


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
        """, (str(file_path), len(lines), now_iso()))
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
            # The transcript's own cwd is exact; the directory name is a lossy
            # encoding of it, so it is only the fallback (D069). Without this,
            # `--project bobi-agent` matches none of that repo's own sessions.
            conn.execute("""
                INSERT OR REPLACE INTO conversations (session_id, project, cwd, git_branch, started_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, cwd or project, cwd, git_branch, timestamp))
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
    """, (str(file_path), _lines_consumed(lines),
          now_iso()))

    conn.execute("""
        UPDATE conversations SET message_count = (
            SELECT COUNT(*) FROM messages WHERE session_id = ?
        ) WHERE session_id = ?
    """, (session_id, session_id))

    return inserted


def index(project_filter: str | None = None) -> dict:
    """Index this agent's conversation JSONL files. Returns stats.

    A file nobody has claimed yet is left unread, not indexed and filtered
    later. The sleep cycle's cursor is a row id, and a session's first lines
    reach disk before its id reaches bobi: indexing them early would give them
    ids below a cursor that has already moved on, and they would never be read.
    """
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    owned = _owned_sessions()
    conn = sqlite3.connect(str(_db_path()))
    _init_db(conn)

    files = []
    # `claude_projects_dirs` dedupes by spelling, which misses a
    # CLAUDE_CONFIG_DIR symlinked at ~/.claude — two names, one tree. Resolve
    # each root once (not each file) so `files_scanned` counts files rather
    # than visits.
    seen_roots: set[str] = set()
    for projects in _projects_dirs():
        if not projects.exists():
            continue
        key = str(projects.resolve())
        if key in seen_roots:
            continue
        seen_roots.add(key)
        for project_dir in projects.iterdir():
            if not project_dir.is_dir():
                continue
            if project_filter and project_filter not in project_dir.name:
                continue
            for f in project_dir.glob("*.jsonl"):
                if f.stem in owned:
                    files.append(f)

    total_new = 0
    files_processed = 0
    for f in files:
        new = _index_file(conn, f)
        if new > 0:
            files_processed += 1
            total_new += new

    conn.commit()

    owned_json = json.dumps(list(owned))
    stats = conn.execute(
        "SELECT COUNT(*) FROM conversations "
        "WHERE session_id IN (SELECT value FROM json_each(?))", (owned_json,),
    ).fetchone()[0]
    msg_count = conn.execute(
        "SELECT COUNT(*) FROM messages "
        "WHERE session_id IN (SELECT value FROM json_each(?))", (owned_json,),
    ).fetchone()[0]
    conn.close()

    return {
        "files_scanned": len(files),
        "files_with_new": files_processed,
        "new_messages": total_new,
        "total_conversations": stats,
        "total_messages": msg_count,
    }


def _fts_query(query: str) -> str:
    """Convert natural language query to FTS5 syntax. Quotes each token.

    FTS5 escapes a double quote inside a phrase by DOUBLING it, so a token
    carrying an odd number of them (`5"`) has to be escaped rather than
    wrapped — wrapping yields `"5""`, which raises 'unterminated string'
    straight out of search() (D068).
    """
    quoted = ['"' + t.replace('"', '""') + '"' for t in query.split() if t]
    return " OR ".join(quoted)


def search(query: str, limit: int = 20, project: str | None = None) -> list[dict]:
    """Full-text search across conversation history."""
    if not _db_path().exists():
        return []

    fts_query = _fts_query(query)
    if not fts_query:
        # An empty MATCH expression is itself an FTS5 syntax error, so an
        # all-whitespace query has to stop here rather than reach the query
        # (D068). No tokens means nothing could have matched anyway.
        return []

    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row

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
          AND m.session_id IN (SELECT value FROM json_each(?))
    """
    params: list = [fts_query, _owned_json()]

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

    sql = "SELECT * FROM conversations WHERE session_id IN (SELECT value FROM json_each(?))"
    params: list = [_owned_json()]
    if project:
        sql += " AND project LIKE ?"
        params.append(f"%{project}%")
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results


def session_messages(session_id: str) -> list[dict]:
    """Get all messages from a specific session."""
    if not _db_path().exists() or session_id not in _owned_sessions():
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

    The delta the sleep cycle (#456) ingests each run, across all of this
    agent's sessions. Rows another agent's session left in a database indexed
    before #1142 are skipped; ids stay monotonic across them, so a cursor from
    before the fix stays valid and a foreign stretch cannot park it. Keyed on
    the table's unique autoincrement ``messages.id`` — NOT a
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
          AND session_id IN (SELECT value FROM json_each(?))
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
    params: list = [int(cursor), _owned_json()]
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
