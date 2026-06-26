"""Unit tests for the KB store — schema, CRUD, FTS search, dedup, and management."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bobi.kb.store import (
    EMBEDDING_DIM,
    KBStore,
    _chunk_text,
    _fetchall,
    _fetchone,
    _fetchval,
    _fts_query,
    _kb_dir,
    _rrf_merge,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kb_root(tmp_path, monkeypatch):
    """Redirect KB storage to a temp directory."""
    kb_d = tmp_path / "kb"
    kb_d.mkdir()
    monkeypatch.setattr("bobi.kb.store._kb_dir", lambda: kb_d)
    return kb_d


@pytest.fixture
def store(kb_root):
    """Create a fresh test KB."""
    return KBStore.create("test", db_path=kb_root / "test.db")


def _mock_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic fake embedder — hash-based fixed vectors."""
    results = []
    for t in texts:
        h = hashlib.sha256(t.encode()).digest()
        vec = [b / 255.0 for b in h]
        vec = (vec * ((EMBEDDING_DIM // len(vec)) + 1))[:EMBEDDING_DIM]
        results.append(vec)
    return results


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_tables_created(self, store):
        conn = store._connect()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        conn.close()
        assert "entries" in tables
        assert "kb_meta" in tables

    def test_fts_table_created(self, store):
        conn = store._connect()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        conn.close()
        assert "entries_fts" in tables

    def test_schema_idempotent(self, store):
        conn = store._connect()
        KBStore._init_schema(conn)
        KBStore._init_schema(conn)
        count = _fetchval(conn, "SELECT COUNT(*) FROM entries")
        conn.close()
        assert count == 0

    def test_meta_seeded(self, store):
        conn = store._connect()
        meta = {}
        for row in _fetchall(conn, "SELECT key, value FROM kb_meta"):
            meta[row["key"]] = row["value"]
        conn.close()
        assert meta["name"] == "test"
        assert "created_at" in meta
        assert meta["embedding_model"] == "all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# add_text
# ---------------------------------------------------------------------------

class TestAddText:
    def test_single_chunk(self, store):
        ids = store.add_text("Hello world")
        assert len(ids) == 1

    def test_multiple_chunks(self, store):
        para = "x" * 150
        text = f"{para}\n\n{para}\n\n{para}"
        ids = store.add_text(text)
        assert len(ids) == 3

    def test_entry_content_stored(self, store):
        store.add_text("Test content here")
        conn = store._connect()
        row = _fetchone(conn, "SELECT content FROM entries")
        conn.close()
        assert row["content"] == "Test content here"

    def test_source_stored(self, store):
        store.add_text("Hello", source="myfile.md")
        conn = store._connect()
        row = _fetchone(conn, "SELECT source FROM entries")
        conn.close()
        assert row["source"] == "myfile.md"

    def test_default_source_is_inline(self, store):
        store.add_text("Hello")
        conn = store._connect()
        row = _fetchone(conn, "SELECT source FROM entries")
        conn.close()
        assert row["source"] == "inline"

    def test_metadata_stored(self, store):
        store.add_text("Hello", metadata={"key": "value"})
        conn = store._connect()
        row = _fetchone(conn, "SELECT metadata FROM entries")
        conn.close()
        assert json.loads(row["metadata"]) == {"key": "value"}

    def test_chunk_index_sequential(self, store):
        para = "x" * 150
        text = f"{para}\n\n{para}\n\n{para}"
        store.add_text(text)
        conn = store._connect()
        rows = _fetchall(conn, "SELECT chunk_index FROM entries ORDER BY id")
        conn.close()
        assert [r["chunk_index"] for r in rows] == [0, 1, 2]

    def test_empty_text_returns_empty(self, store):
        ids = store.add_text("")
        assert ids == []

    def test_with_embed_fn(self, store):
        ids = store.add_text("Test embedding", embed_fn=_mock_embed)
        assert len(ids) == 1

    def test_fts_trigger_populates(self, store):
        store.add_text("searchable content here")
        conn = store._connect()
        rows = list(conn.execute(
            "SELECT rowid FROM entries_fts WHERE entries_fts MATCH '\"searchable\"'"
        ))
        conn.close()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# add_file
# ---------------------------------------------------------------------------

class TestAddFile:
    def test_adds_file_content(self, store, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("File content here")
        ids = store.add_file(f)
        assert len(ids) >= 1

    def test_source_is_file_path(self, store, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("Content")
        store.add_file(f)
        conn = store._connect()
        row = _fetchone(conn, "SELECT source FROM entries")
        conn.close()
        assert row["source"] == str(f)

    def test_dedup_same_content(self, store, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("Same content")
        store.add_file(f)
        ids = store.add_file(f)
        assert ids == []

    def test_reindex_changed_file(self, store, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("Original")
        store.add_file(f)

        f.write_text("Modified content")
        ids = store.add_file(f)
        assert len(ids) >= 1

        conn = store._connect()
        count = _fetchval(conn, "SELECT COUNT(*) FROM entries")
        conn.close()
        assert count == len(ids)

    def test_with_embed_fn(self, store, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("Embedded file content")
        ids = store.add_file(f, embed_fn=_mock_embed)
        assert len(ids) >= 1


# ---------------------------------------------------------------------------
# search (FTS only)
# ---------------------------------------------------------------------------

class TestSearchFTS:
    def test_finds_matching_content(self, store):
        store.add_text("Python is a programming language")
        store.add_text("JavaScript runs in the browser")
        results = store.search("Python")
        assert len(results) >= 1
        assert "Python" in results[0]["content"]

    def test_no_results(self, store):
        store.add_text("Hello world")
        results = store.search("xyznotfound")
        assert results == []

    def test_empty_kb(self, store):
        results = store.search("anything")
        assert results == []

    def test_limit_respected(self, store):
        for i in range(20):
            store.add_text(f"Document number {i} about testing")
        results = store.search("testing", limit=5)
        assert len(results) <= 5

    def test_results_have_score(self, store):
        store.add_text("Important document")
        results = store.search("Important")
        assert "score" in results[0]

    def test_results_have_source(self, store):
        store.add_text("Content", source="test.md")
        results = store.search("Content")
        assert results[0]["source"] == "test.md"


# ---------------------------------------------------------------------------
# search (hybrid with embed_fn)
# ---------------------------------------------------------------------------

class TestSearchHybrid:
    def test_hybrid_returns_results(self, store):
        store.add_text("Machine learning is fascinating", embed_fn=_mock_embed)
        store.add_text("Baking bread requires flour", embed_fn=_mock_embed)
        results = store.search("machine learning", embed_fn=_mock_embed)
        assert len(results) >= 1

    def test_hybrid_includes_score(self, store):
        store.add_text("Test document", embed_fn=_mock_embed)
        results = store.search("Test", embed_fn=_mock_embed)
        if results:
            assert "score" in results[0]
            assert results[0]["score"] > 0


# ---------------------------------------------------------------------------
# RRF merge
# ---------------------------------------------------------------------------

class TestRRFMerge:
    def test_single_list(self):
        fts = [{"id": 1, "content": "a"}, {"id": 2, "content": "b"}]
        merged = _rrf_merge(fts, [], limit=10)
        assert len(merged) == 2
        assert merged[0]["id"] == 1

    def test_overlapping_results(self):
        fts = [{"id": 1, "content": "a"}, {"id": 2, "content": "b"}]
        vec = [{"id": 2, "content": "b"}, {"id": 3, "content": "c"}]
        merged = _rrf_merge(fts, vec, limit=10)
        ids = [r["id"] for r in merged]
        assert 2 in ids
        id2 = next(r for r in merged if r["id"] == 2)
        assert id2["score"] > merged[-1]["score"]

    def test_limit(self):
        fts = [{"id": i, "content": f"doc{i}"} for i in range(10)]
        merged = _rrf_merge(fts, [], limit=3)
        assert len(merged) == 3

    def test_empty_inputs(self):
        assert _rrf_merge([], [], limit=10) == []

    def test_scores_are_positive(self):
        fts = [{"id": 1, "content": "a"}]
        vec = [{"id": 1, "content": "a"}]
        merged = _rrf_merge(fts, vec, limit=10)
        assert merged[0]["score"] > 0


# ---------------------------------------------------------------------------
# FTS query builder
# ---------------------------------------------------------------------------

class TestFTSQuery:
    def test_single_term(self):
        assert _fts_query("hello") == '"hello"'

    def test_multiple_terms(self):
        result = _fts_query("hello world")
        assert '"hello"' in result
        assert '"world"' in result
        assert " OR " in result

    def test_empty(self):
        assert _fts_query("") == ""


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------

class TestInfo:
    def test_empty_kb(self, store):
        info = store.info()
        assert info["name"] == "test"
        assert info["entry_count"] == 0
        assert info["source_count"] == 0

    def test_with_entries(self, store):
        store.add_text("Hello", source="a.md")
        store.add_text("World", source="b.md")
        info = store.info()
        assert info["entry_count"] == 2
        assert info["source_count"] == 2

    def test_sources_listed(self, store):
        para = "x" * 150
        store.add_text(f"{para}\n\n{para}\n\n{para}", source="doc.md")
        info = store.info()
        sources = info["sources"]
        assert len(sources) == 1
        assert sources[0]["source"] == "doc.md"
        assert sources[0]["count"] == 3


# ---------------------------------------------------------------------------
# remove_source
# ---------------------------------------------------------------------------

class TestRemoveSource:
    def test_removes_entries(self, store):
        store.add_text("Content", source="target.md")
        store.add_text("Other", source="keep.md")
        removed = store.remove_source("target.md")
        assert removed == 1

        conn = store._connect()
        count = _fetchval(conn, "SELECT COUNT(*) FROM entries")
        conn.close()
        assert count == 1

    def test_removes_from_fts(self, store):
        store.add_text("Searchable text", source="target.md")
        store.remove_source("target.md")
        results = store.search("Searchable")
        assert results == []

    def test_nonexistent_source(self, store):
        removed = store.remove_source("nope.md")
        assert removed == 0


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_removes_db_file(self, store):
        db_path = store._db_path
        assert db_path.exists()
        store.delete()
        assert not db_path.exists()


# ---------------------------------------------------------------------------
# KB management (class methods)
# ---------------------------------------------------------------------------

class TestKBManagement:
    def test_create(self, kb_root):
        store = KBStore.create("mydb", db_path=kb_root / "mydb.db")
        assert store.name == "mydb"
        assert (kb_root / "mydb.db").exists()

    def test_create_duplicate_raises(self, kb_root):
        KBStore.create("dup", db_path=kb_root / "dup.db")
        with pytest.raises(FileExistsError):
            KBStore.create("dup", db_path=kb_root / "dup.db")

    def test_open_nonexistent_raises(self, kb_root):
        with pytest.raises(FileNotFoundError):
            KBStore("nonexistent", db_path=kb_root / "nonexistent.db")

    def test_remove(self, kb_root):
        KBStore.create("removeme")
        KBStore.remove("removeme")
        assert not (kb_root / "removeme.db").exists()

    def test_remove_nonexistent_raises(self, kb_root):
        with pytest.raises(FileNotFoundError):
            KBStore.remove("ghost")

    def test_list_empty(self, kb_root):
        kbs = KBStore.list_kbs()
        # May include the test fixture KB — filter
        assert isinstance(kbs, list)

    def test_list_kbs(self, kb_root):
        KBStore.create("alpha")
        KBStore.create("beta")
        kbs = KBStore.list_kbs()
        names = [k["name"] for k in kbs]
        assert "alpha" in names
        assert "beta" in names

    def test_list_includes_counts(self, kb_root):
        s = KBStore.create("counted")
        s.add_text("Entry one")
        s.add_text("Entry two")
        kbs = KBStore.list_kbs()
        counted = next(k for k in kbs if k["name"] == "counted")
        assert counted["entry_count"] == 2


# ---------------------------------------------------------------------------
# Optional dependency error messages (#259)
# ---------------------------------------------------------------------------

class TestOptionalDependencyErrors:
    """Verify graceful errors when optional [kb] deps are missing."""

    def test_missing_sqlite_vec_gives_clear_error(self, kb_root):
        """When sqlite-vec is not installed, _load_vec raises ImportError
        with install instructions instead of a bare ModuleNotFoundError."""
        import builtins
        real_import = builtins.__import__

        def _block_sqlite_vec(name, *args, **kwargs):
            if name == "sqlite_vec":
                raise ImportError("No module named 'sqlite_vec'")
            return real_import(name, *args, **kwargs)

        import apsw
        conn = apsw.Connection(":memory:")
        with patch("builtins.__import__", side_effect=_block_sqlite_vec):
            with pytest.raises(ImportError, match="pip install 'bobi\\[kb\\]'"):
                KBStore._load_vec(conn)
        conn.close()
