"""Vector-index distance-metric migration (containerized-4 follow-up).

The embedder swap (sentence-transformers → fastembed) changed stored-vector
magnitude: fastembed L2-normalizes to unit length, the old path did not. The
vec0 index ranks by L2 distance by default, under which a unit-length query
vector mis-ranks against the old raw-magnitude vectors. The fix is a cosine
(magnitude-invariant) index, applied to existing KBs via a one-time migration
on open. These tests prove the migration fixes ranking without re-embedding.
"""

import json

import apsw
import pytest

from bobi.kb.store import KBStore, EMBEDDING_DIM, _fetchone

sqlite_vec = pytest.importorskip("sqlite_vec")


def _axis_vec(axis: int, magnitude: float) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[axis] = magnitude
    return v


def _build_legacy_l2_kb(path, rows):
    """Write a KB db with the *pre-cosine* (default-L2) vec0 table.

    `rows` is a list of (entry_id, content, vector). No `vec_metric` marker is
    written — exactly the shape of a KB created before the migration existed.
    """
    conn = apsw.Connection(str(path))
    conn.enableloadextension(True)
    conn.loadextension(sqlite_vec.loadable_path())
    conn.enableloadextension(False)
    with conn:
        conn.execute("CREATE TABLE kb_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            """CREATE TABLE entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
                source TEXT, source_hash TEXT, chunk_index INTEGER DEFAULT 0,
                metadata TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute(
            "CREATE VIRTUAL TABLE entries_fts USING fts5(content, content=entries, content_rowid=id)")
        # Legacy index: NO distance_metric => L2.
        conn.execute(
            f"""CREATE VIRTUAL TABLE entries_vec
                USING vec0(entry_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBEDDING_DIM}])""")
        conn.execute("INSERT OR REPLACE INTO kb_meta VALUES ('name', 'legacy')")
        for eid, content, vec in rows:
            conn.execute(
                """INSERT INTO entries (id, content, source, source_hash, created_at, updated_at)
                   VALUES (?, ?, 't', 'h', 'now', 'now')""", (eid, content))
            conn.execute(
                "INSERT INTO entries_vec (entry_id, embedding) VALUES (?, ?)",
                (eid, json.dumps(vec)))
    conn.close()


def test_new_kb_is_born_cosine(tmp_path):
    store = KBStore.create("fresh", db_path=tmp_path / "fresh.db")
    conn = store._connect()
    marker = _fetchone(conn, "SELECT value FROM kb_meta WHERE key='vec_metric'")
    assert marker and marker["value"] == "cosine"
    store.close()


def test_migration_fixes_legacy_l2_ranking(tmp_path):
    """A KB indexed under L2 with raw-magnitude vectors mis-ranks a unit query;
    after the on-open migration to cosine it ranks by direction (correctly),
    with the original vectors re-used (no re-embedding)."""
    db = tmp_path / "legacy.db"
    # Entry A: query's direction but large magnitude (cosine-near, L2-far).
    # Entry B: orthogonal direction, unit magnitude (cosine-far, L2-near).
    _build_legacy_l2_kb(db, [
        (1, "alpha", _axis_vec(0, 10.0)),
        (2, "beta", _axis_vec(1, 1.0)),
    ])

    store = KBStore("legacy", db_path=db)
    conn = store._connect()  # triggers migration

    marker = _fetchone(conn, "SELECT value FROM kb_meta WHERE key='vec_metric'")
    assert marker and marker["value"] == "cosine"

    query = _axis_vec(0, 1.0)  # unit vector in entry A's direction
    results = store._vec_search(conn, query, limit=2)
    assert [r["id"] for r in results][0] == 1, "entry A (same direction) must rank first under cosine"
    store.close()


def test_migration_is_idempotent(tmp_path):
    """Re-opening an already-migrated KB is a cheap no-op (marker set)."""
    db = tmp_path / "legacy.db"
    _build_legacy_l2_kb(db, [(1, "alpha", _axis_vec(0, 3.0))])

    KBStore("legacy", db_path=db)._connect()  # first open migrates
    store = KBStore("legacy", db_path=db)
    conn = store._connect()  # second open: marker already cosine
    marker = _fetchone(conn, "SELECT value FROM kb_meta WHERE key='vec_metric'")
    assert marker["value"] == "cosine"
    # Data survived the migration.
    assert _fetchone(conn, "SELECT COUNT(*) AS n FROM entries_vec")["n"] == 1
    store.close()
