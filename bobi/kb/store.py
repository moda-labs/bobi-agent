"""Knowledge base storage — SQLite + FTS5 + sqlite-vec per named KB.

Each KB is a separate database at <run>/state/kb/<name>.db.
Uses APSW for SQLite extension loading (sqlite-vec).
The store accepts an optional embed_fn for vector operations, making it
independently testable without the embedding sidecar.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

import apsw

log = logging.getLogger(__name__)

EMBEDDING_DIM = 384
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
MAX_CHUNK_CHARS = 2000
MIN_CHUNK_CHARS = 100


def _kb_dir() -> Path:
    from bobi import paths
    return paths.state_dir() / "kb"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# APSW helpers
# ---------------------------------------------------------------------------

def _fetchone(conn: apsw.Connection, sql: str,
              bindings=None) -> dict | None:
    cur = conn.execute(sql, bindings or ())
    row = next(cur, None)
    if row is None:
        return None
    desc = cur.getdescription()
    return {d[0]: v for d, v in zip(desc, row)}


def _fetchall(conn: apsw.Connection, sql: str,
              bindings=None) -> list[dict]:
    cur = conn.execute(sql, bindings or ())
    desc = None
    results = []
    for row in cur:
        if desc is None:
            desc = cur.getdescription()
        results.append({d[0]: v for d, v in zip(desc, row)})
    return results


def _fetchval(conn: apsw.Connection, sql: str, bindings=None):
    """Fetch a single scalar value."""
    row = next(conn.execute(sql, bindings or ()), None)
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')


def _chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS,
                min_chars: int = MIN_CHUNK_CHARS) -> list[str]:
    """Split text into chunks suitable for embedding.

    Strategy: split on double-newlines (paragraphs), then on sentence
    boundaries if a paragraph is too long. Merge tiny paragraphs.
    """
    text = text.strip()
    if not text:
        return []

    paragraphs = re.split(r'\n\s*\n', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    if not paragraphs:
        return []

    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            chunks.append(para)
        else:
            sentences = _SENTENCE_RE.split(para)
            current = ""
            for sent in sentences:
                if current and len(current) + len(sent) + 1 > max_chars:
                    chunks.append(current.strip())
                    current = sent
                else:
                    current = f"{current} {sent}" if current else sent
            if current.strip():
                chunks.append(current.strip())

    merged: list[str] = []
    for chunk in chunks:
        if merged and len(merged[-1]) < min_chars and \
                len(merged[-1]) + len(chunk) + 1 <= max_chars:
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        else:
            merged.append(chunk)

    return merged


# ---------------------------------------------------------------------------
# FTS5 query builder
# ---------------------------------------------------------------------------

def _fts_query(query: str) -> str:
    tokens = query.split()
    quoted = [f'"{t}"' for t in tokens if t]
    return " OR ".join(quoted)


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def _rrf_merge(fts_results: list[dict], vec_results: list[dict],
               limit: int, k: int = 60) -> list[dict]:
    """Merge two ranked result lists using Reciprocal Rank Fusion."""
    scores: dict[int, float] = {}
    by_id: dict[int, dict] = {}

    for rank, r in enumerate(fts_results):
        entry_id = r["id"]
        scores[entry_id] = scores.get(entry_id, 0) + 1.0 / (k + rank + 1)
        by_id[entry_id] = r

    for rank, r in enumerate(vec_results):
        entry_id = r["id"]
        scores[entry_id] = scores.get(entry_id, 0) + 1.0 / (k + rank + 1)
        by_id[entry_id] = r

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    results = []
    for entry_id, score in ranked:
        row = dict(by_id[entry_id])
        row["score"] = score
        results.append(row)
    return results


# ---------------------------------------------------------------------------
# KBStore
# ---------------------------------------------------------------------------

class KBStore:
    """Manages a single named knowledge base."""

    def __init__(self, name: str, db_path: Path | None = None):
        self.name = name
        self._db_path = db_path or (_kb_dir() / f"{name}.db")
        if not self._db_path.exists():
            raise FileNotFoundError(f"KB '{name}' does not exist at {self._db_path}")
        self._conn: apsw.Connection | None = None

    @staticmethod
    def kb_dir() -> Path:
        return _kb_dir()

    @staticmethod
    def db_path_for(name: str) -> Path:
        return _kb_dir() / f"{name}.db"

    def _connect(self) -> apsw.Connection:
        """Open the connection lazily and reuse it for the store's lifetime."""
        if self._conn is None:
            conn = apsw.Connection(str(self._db_path))
            self._load_vec(conn)
            self._migrate_vec_metric(conn)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "KBStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def _load_vec(conn: apsw.Connection) -> None:
        try:
            import sqlite_vec
        except ImportError:
            raise ImportError(
                "Knowledge base requires sqlite-vec. "
                "Install with: pip install 'bobi[kb]'"
            ) from None
        conn.enableloadextension(True)
        conn.loadextension(sqlite_vec.loadable_path())
        conn.enableloadextension(False)

    @staticmethod
    def _init_schema(conn: apsw.Connection) -> None:
        stmts = [
            """CREATE TABLE IF NOT EXISTS kb_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT NOT NULL,
                source      TEXT,
                source_hash TEXT,
                chunk_index INTEGER DEFAULT 0,
                metadata    TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                content,
                content=entries,
                content_rowid=id
            )""",
            """CREATE TRIGGER IF NOT EXISTS entries_fts_ai AFTER INSERT ON entries BEGIN
                INSERT INTO entries_fts(rowid, content)
                VALUES (new.id, new.content);
            END""",
            """CREATE TRIGGER IF NOT EXISTS entries_fts_ad AFTER DELETE ON entries BEGIN
                INSERT INTO entries_fts(entries_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END""",
            """CREATE TRIGGER IF NOT EXISTS entries_fts_au AFTER UPDATE OF content ON entries BEGIN
                INSERT INTO entries_fts(entries_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO entries_fts(rowid, content)
                VALUES (new.id, new.content);
            END""",
            # cosine distance is magnitude-invariant: it ranks by direction
            # only, so vectors from different embedders (fastembed normalizes
            # to unit length; the old sentence-transformers path did not) rank
            # consistently. vec0 defaults to L2, under which a unit-length
            # query vector mis-ranks against raw-magnitude stored vectors —
            # see _migrate_vec_metric for the one-time fix on existing KBs.
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS entries_vec
                USING vec0(entry_id INTEGER PRIMARY KEY,
                           embedding FLOAT[{EMBEDDING_DIM}] distance_metric=cosine)""",
        ]
        for sql in stmts:
            conn.execute(sql)

    def _migrate_vec_metric(self, conn: apsw.Connection) -> None:
        """Rebuild a pre-cosine vector index to cosine distance (one-time).

        KBs created before the embedder swap have an L2-distance vec0 table.
        Under L2, querying with a unit-length (fastembed) vector against the
        old raw-magnitude stored vectors silently mis-ranks. Cosine is
        magnitude-invariant, so re-inserting the existing vectors into a
        cosine table fixes ranking without re-embedding. Guarded by a
        kb_meta marker so it runs at most once per KB.
        """
        marker = _fetchone(
            conn, "SELECT value FROM kb_meta WHERE key = 'vec_metric'")
        if marker and marker.get("value") == "cosine":
            return
        table = _fetchone(
            conn, "SELECT name FROM sqlite_master WHERE name = 'entries_vec'")
        if not table:
            return
        try:
            vecs = _fetchall(
                conn,
                "SELECT entry_id, vec_to_json(embedding) AS emb FROM entries_vec")
            with conn:
                conn.execute("DROP TABLE entries_vec")
                conn.execute(
                    f"""CREATE VIRTUAL TABLE entries_vec
                        USING vec0(entry_id INTEGER PRIMARY KEY,
                                   embedding FLOAT[{EMBEDDING_DIM}] distance_metric=cosine)""")
                for v in vecs:
                    conn.execute(
                        "INSERT INTO entries_vec (entry_id, embedding) VALUES (?, ?)",
                        (v["entry_id"], v["emb"]))
                conn.execute(
                    "INSERT OR REPLACE INTO kb_meta VALUES ('vec_metric', 'cosine')")
            log.info("KB %s: migrated vector index to cosine distance "
                     "(%d vectors)", self.name, len(vecs))
        except Exception:
            log.warning("KB %s: cosine vec-metric migration failed; vector "
                        "search may be degraded until reindex",
                        self.name, exc_info=True)

    # --- Core operations ---

    def add_text(self, text: str, source: str = "inline",
                 metadata: dict | None = None,
                 embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
                 ) -> list[int]:
        """Chunk text, insert entries, compute & store embeddings."""
        chunks = _chunk_text(text)
        if not chunks:
            return []

        now = _now()
        meta_json = json.dumps(metadata) if metadata else None
        source_hash = hashlib.sha256(text.encode()).hexdigest()

        conn = self._connect()
        ids = []
        with conn:
            for i, chunk in enumerate(chunks):
                conn.execute(
                    """INSERT INTO entries
                       (content, source, source_hash, chunk_index, metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (chunk, source, source_hash, i, meta_json, now, now),
                )
                ids.append(conn.last_insert_rowid())

            if embed_fn and ids:
                embeddings = embed_fn(chunks)
                self._store_embeddings(conn, ids, embeddings)

        return ids

    def add_file(self, path: Path,
                 embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
                 ) -> list[int]:
        """Read file, dedup by hash, chunk, insert."""
        content = path.read_text()
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        source = str(path)

        conn = self._connect()
        existing = _fetchone(
            conn,
            "SELECT source_hash FROM entries WHERE source = ? LIMIT 1",
            (source,),
        )

        if existing and existing["source_hash"] == file_hash:
            return []

        if existing:
            with conn:
                self._remove_source_entries(conn, source)

        return self.add_text(content, source=source, embed_fn=embed_fn)

    def search(self, query: str, limit: int = 10,
               embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
               ) -> list[dict]:
        """Hybrid search: FTS5 + vector, merged via RRF."""
        conn = self._connect()
        fts_results = self._fts_search(conn, query, limit)

        if embed_fn:
            query_embedding = embed_fn([query])[0]
            vec_results = self._vec_search(conn, query_embedding, limit)
            return _rrf_merge(fts_results, vec_results, limit)

        for i, r in enumerate(fts_results):
            r["score"] = 1.0 / (60 + i + 1)
        return fts_results[:limit]

    def near_duplicates(self, entry_id: int, limit: int = 10,
                        min_similarity: float = 0.85) -> list[dict]:
        """Return vector-near entries for an existing entry.

        sqlite-vec's cosine metric reports distance, so similarity is
        ``1 - distance``. The entry itself is excluded.
        """
        conn = self._connect()
        row = _fetchone(
            conn,
            "SELECT vec_to_json(embedding) AS embedding FROM entries_vec WHERE entry_id = ?",
            (entry_id,),
        )
        if not row:
            return []

        try:
            embedding = json.loads(row["embedding"])
        except (TypeError, json.JSONDecodeError):
            return []

        candidates = self._vec_search(conn, embedding, limit + 1)
        results: list[dict] = []
        for candidate in candidates:
            if candidate["id"] == entry_id:
                continue
            similarity = 1.0 - float(candidate.get("distance", 1.0) or 0.0)
            if similarity < min_similarity:
                continue
            item = dict(candidate)
            item["similarity"] = similarity
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def dedup_exact_by_source_hash(self) -> dict:
        """Remove older duplicate chunks that share source_hash and chunk_index."""
        conn = self._connect()
        rows = _fetchall(
            conn,
            """SELECT id, source_hash, chunk_index
               FROM entries
               WHERE source_hash IS NOT NULL
               ORDER BY id DESC""",
        )
        seen: set[tuple[str, int]] = set()
        delete_ids: list[int] = []
        for row in rows:
            key = (row["source_hash"], int(row["chunk_index"] or 0))
            if key in seen:
                delete_ids.append(row["id"])
            else:
                seen.add(key)
        if delete_ids:
            with conn:
                self._delete_entry_ids(conn, delete_ids)
        return {"deduped": len(delete_ids)}

    def dedup_semantic(self, *, auto_merge_threshold: float = 0.95,
                       flag_threshold: float = 0.85,
                       limit_per_entry: int = 5) -> dict:
        """Merge high-confidence vector duplicates and count gray-zone pairs."""
        conn = self._connect()
        rows = _fetchall(
            conn,
            "SELECT id, source, source_hash FROM entries ORDER BY id DESC",
        )
        live_ids = {r["id"] for r in rows}
        by_id = {r["id"]: r for r in rows}
        delete_ids: set[int] = set()
        flagged: set[tuple[int, int]] = set()

        for row in rows:
            entry_id = row["id"]
            if entry_id not in live_ids:
                continue
            for candidate in self.near_duplicates(
                entry_id, limit=limit_per_entry, min_similarity=flag_threshold
            ):
                other_id = candidate["id"]
                if other_id not in live_ids or other_id == entry_id:
                    continue
                current = by_id.get(entry_id) or {}
                other = by_id.get(other_id) or {}
                if (
                    current.get("source") == other.get("source")
                    and current.get("source_hash") == other.get("source_hash")
                ):
                    continue
                similarity = float(candidate.get("similarity", 0.0) or 0.0)
                older_id, newer_id = sorted((entry_id, other_id))
                if similarity >= auto_merge_threshold:
                    self._record_merged_provenance(conn, newer_id, older_id, similarity)
                    delete_ids.add(older_id)
                    live_ids.discard(older_id)
                else:
                    flagged.add((older_id, newer_id))

        if delete_ids:
            with conn:
                self._delete_entry_ids(conn, sorted(delete_ids))
        return {"merged": len(delete_ids), "flagged": len(flagged)}

    def info(self) -> dict:
        conn = self._connect()
        entry_count = _fetchval(conn, "SELECT COUNT(*) FROM entries")

        sources = _fetchall(
            conn,
            """SELECT source, COUNT(*) as count
               FROM entries GROUP BY source ORDER BY count DESC""",
        )

        meta = {}
        for row in _fetchall(conn, "SELECT key, value FROM kb_meta"):
            meta[row["key"]] = row["value"]

        return {
            "name": self.name,
            "entry_count": entry_count,
            "source_count": len(sources),
            "sources": sources,
            "embedding_model": meta.get("embedding_model", EMBEDDING_MODEL),
            "created_at": meta.get("created_at", ""),
        }

    def remove_source(self, source: str) -> int:
        conn = self._connect()
        with conn:
            return self._remove_source_entries(conn, source)

    def delete(self) -> None:
        self.close()
        if self._db_path.exists():
            self._db_path.unlink()

    # --- Private helpers ---

    def _fts_search(self, conn: apsw.Connection, query: str,
                    limit: int) -> list[dict]:
        fts = _fts_query(query)
        rows = _fetchall(
            conn,
            """SELECT e.id, e.content, e.source, e.source_hash, e.chunk_index,
                      e.metadata, e.created_at, e.updated_at, entries_fts.rank
               FROM entries_fts
               JOIN entries e ON e.id = entries_fts.rowid
               WHERE entries_fts MATCH ?
               ORDER BY entries_fts.rank
               LIMIT ?""",
            (fts, limit),
        )
        return [self._decode_metadata(r) for r in rows]

    def _vec_search(self, conn: apsw.Connection,
                    query_embedding: list[float], limit: int) -> list[dict]:
        try:
            rows = _fetchall(
                conn,
                """SELECT entry_id, distance
                   FROM entries_vec
                   WHERE embedding MATCH ?
                   ORDER BY distance
                   LIMIT ?""",
                (json.dumps(query_embedding), limit),
            )
        except Exception:
            return []

        if not rows:
            return []

        entry_ids = [r["entry_id"] for r in rows]
        placeholders = ",".join("?" * len(entry_ids))
        entries = _fetchall(
            conn,
            f"""SELECT id, content, source, source_hash, chunk_index, metadata,
                       created_at, updated_at
                FROM entries WHERE id IN ({placeholders})""",
            entry_ids,
        )

        entries_by_id = {r["id"]: dict(r) for r in entries}
        results = []
        for r in rows:
            entry = entries_by_id.get(r["entry_id"])
            if entry:
                entry["distance"] = r["distance"]
                results.append(self._decode_metadata(entry))
        return results

    @staticmethod
    def _decode_metadata(row: dict) -> dict:
        value = row.get("metadata")
        if value:
            try:
                row["metadata"] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                row["metadata"] = {}
        else:
            row["metadata"] = {}
        return row

    def _record_merged_provenance(self, conn: apsw.Connection, keep_id: int,
                                  merged_id: int, similarity: float) -> None:
        keep = _fetchone(
            conn,
            """SELECT id, metadata FROM entries WHERE id = ?""",
            (keep_id,),
        )
        merged = _fetchone(
            conn,
            """SELECT id, source, source_hash, chunk_index, metadata, created_at
               FROM entries WHERE id = ?""",
            (merged_id,),
        )
        if not keep or not merged:
            return
        keep_meta = self._decode_metadata(keep).get("metadata") or {}
        merged_meta = self._decode_metadata(merged).get("metadata") or {}
        provenance = list(keep_meta.get("merged_from") or [])
        provenance.append({
            "id": merged["id"],
            "source": merged.get("source"),
            "source_hash": merged.get("source_hash"),
            "chunk_index": merged.get("chunk_index"),
            "created_at": merged.get("created_at"),
            "metadata": merged_meta,
            "similarity": similarity,
        })
        keep_meta["merged_from"] = provenance
        conn.execute(
            "UPDATE entries SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(keep_meta), _now(), keep_id),
        )

    def _store_embeddings(self, conn: apsw.Connection,
                          ids: list[int],
                          embeddings: list[list[float]]) -> None:
        for entry_id, emb in zip(ids, embeddings):
            conn.execute(
                "INSERT INTO entries_vec (entry_id, embedding) VALUES (?, ?)",
                (entry_id, json.dumps(emb)),
            )

    def _remove_source_entries(self, conn: apsw.Connection,
                               source: str) -> int:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM entries WHERE source = ?", (source,)
        )]

        if ids:
            self._delete_entry_ids(conn, ids)
        return len(ids)

    def _delete_entry_ids(self, conn: apsw.Connection, ids: list[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM entries_vec WHERE entry_id IN ({placeholders})",
            ids,
        )
        conn.execute(
            f"DELETE FROM entries WHERE id IN ({placeholders})",
            ids,
        )
        return len(ids)

    # --- Class methods for KB management ---

    @classmethod
    def create(cls, name: str, db_path: Path | None = None) -> "KBStore":
        path = db_path or (_kb_dir() / f"{name}.db")
        if path.exists():
            raise FileExistsError(f"KB '{name}' already exists at {path}")
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = apsw.Connection(str(path))
        cls._load_vec(conn)
        cls._init_schema(conn)
        now = _now()
        with conn:
            conn.execute("INSERT OR REPLACE INTO kb_meta VALUES ('name', ?)", (name,))
            conn.execute("INSERT OR REPLACE INTO kb_meta VALUES ('created_at', ?)", (now,))
            conn.execute(
                "INSERT OR REPLACE INTO kb_meta VALUES ('embedding_model', ?)",
                (EMBEDDING_MODEL,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO kb_meta VALUES ('embedding_dim', ?)",
                (str(EMBEDDING_DIM),),
            )
            # Fresh KBs are born cosine (see _init_schema); stamp the marker so
            # the migration on next open is a cheap no-op.
            conn.execute(
                "INSERT OR REPLACE INTO kb_meta VALUES ('vec_metric', 'cosine')")
        conn.close()

        return cls(name, db_path=path)

    @classmethod
    def remove(cls, name: str) -> None:
        path = _kb_dir() / f"{name}.db"
        if not path.exists():
            raise FileNotFoundError(f"KB '{name}' does not exist")
        path.unlink()

    @classmethod
    def list_kbs(cls) -> list[dict]:
        kb_d = _kb_dir()
        if not kb_d.exists():
            return []

        results = []
        for db_file in sorted(kb_d.glob("*.db")):
            name = db_file.stem
            try:
                conn = apsw.Connection(str(db_file))
                entry_count = _fetchval(conn, "SELECT COUNT(*) FROM entries")
                created = _fetchone(
                    conn,
                    "SELECT value FROM kb_meta WHERE key = 'created_at'",
                )
                conn.close()
                results.append({
                    "name": name,
                    "entry_count": entry_count,
                    "created_at": created["value"] if created else "",
                    "path": str(db_file),
                })
            except Exception:
                results.append({
                    "name": name,
                    "entry_count": 0,
                    "created_at": "",
                    "path": str(db_file),
                })
        return results
