"""The rebuildable search-index database (SQLite: FTS5 + sqlite-vec).

Follows ``ledger.py``'s connection discipline (WAL, one ``threading.Lock``
per instance, ``check_same_thread=False``) but the opposite migration
story: the index is **derived data**. Any mismatch -- schema version,
embedding model, embedding dimension -- deletes the file and starts empty;
the next index job repopulates it. No ``ALTER TABLE``, ever.

Two FTS5 tables, deliberately: ``chunks_fts`` (unicode61) carries real BM25
scoring over chunk text; ``titles_fts`` (trigram) exists only for
typo-tolerant matching over short identity fields (meeting title, speaker
names, project code). Both are external-content tables kept in sync by
triggers, so a rebuild of either never re-embeds anything.

sqlite-vec is optional at runtime: the baked relocatable CPython may lack
``enable_load_extension``. Embeddings are therefore ALWAYS stored as BLOBs
on ``chunks``; ``chunks_vec`` (the vec0 kNN table) is populated only when
the extension loads, and can be rebuilt from the BLOBs on a later run
without re-embedding.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("transcription")

INDEX_SCHEMA_VERSION = 1

DOC_KINDS = ("transcript", "summary", "note")

_SCHEMA = """
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE docs(
  doc_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('transcript','summary','note')),
  project TEXT NOT NULL,
  meeting_dir TEXT NOT NULL,
  meeting_title TEXT NOT NULL,
  meeting_date TEXT,
  speakers TEXT NOT NULL DEFAULT '',
  mtime_ns INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  UNIQUE(meeting_dir, kind)
);

CREATE TABLE chunks(
  chunk_id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES docs(doc_id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  text TEXT NOT NULL,
  start_sec REAL,
  end_sec REAL,
  embedding BLOB
);
CREATE INDEX chunks_by_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text,
  content='chunks', content_rowid='chunk_id',
  tokenize = "unicode61 remove_diacritics 2 tokenchars '-_'"
);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
  INSERT INTO chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;

CREATE VIRTUAL TABLE titles_fts USING fts5(
  meeting_title, speakers, project,
  content='docs', content_rowid='doc_id',
  tokenize = 'trigram'
);
CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO titles_fts(rowid, meeting_title, speakers, project)
  VALUES (new.doc_id, new.meeting_title, new.speakers, new.project);
END;
CREATE TRIGGER docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO titles_fts(titles_fts, rowid, meeting_title, speakers, project)
  VALUES ('delete', old.doc_id, old.meeting_title, old.speakers, old.project);
END;
CREATE TRIGGER docs_au AFTER UPDATE ON docs BEGIN
  INSERT INTO titles_fts(titles_fts, rowid, meeting_title, speakers, project)
  VALUES ('delete', old.doc_id, old.meeting_title, old.speakers, old.project);
  INSERT INTO titles_fts(rowid, meeting_title, speakers, project)
  VALUES (new.doc_id, new.meeting_title, new.speakers, new.project);
END;
"""

# Filtered kNN over-fetch: sqlite-vec validates `k` before any outer filter
# applies, so a filtered query silently under-returns unless it over-asks.
VEC_OVERFETCH_FACTOR = 4
VEC_MIN_K = 40


@dataclass(frozen=True, kw_only=True)
class DocRecord:
    """One indexable document (a meeting's transcript, summary or note)."""

    kind: str
    project: str
    meeting_dir: str  # vault-root-relative, forward slashes
    meeting_title: str
    meeting_date: str | None
    speakers: str  # distinct speaker names, space-joined
    mtime_ns: int
    content_hash: str


@dataclass(frozen=True, kw_only=True)
class ChunkRecord:
    """One chunk of a document, ready to store (embedding may be absent)."""

    text: str
    start_sec: float | None = None
    end_sec: float | None = None
    embedding: list[float] | None = None


@dataclass(frozen=True, kw_only=True)
class DocRow:
    doc_id: int
    kind: str
    project: str
    meeting_dir: str
    meeting_title: str
    meeting_date: str | None
    speakers: str


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class IndexDb:
    """One connection to the index database.

    Writes happen only on the job manager's single serial executor thread;
    reads go through the same instance under ``_lock``, so there is no
    cross-thread contention to speak of. ``read_only=True`` (the MCP
    server) opens the file via a ``mode=ro`` URI and never migrates it.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedding_model: str,
        embedding_dim: int,
        read_only: bool = False,
    ) -> None:
        self._path = Path(db_path)
        self._embedding_model = embedding_model
        self._embedding_dim = int(embedding_dim)
        self._read_only = read_only
        self._lock = threading.Lock()
        self._conn = self._open()
        self.vec_available = self._setup_vec()
        if not read_only:
            self._migrate_or_recreate()
            if self.vec_available:
                self._rebuild_vec_from_blobs()

    # -- connection / schema lifecycle ----------------------------------

    def _open(self) -> sqlite3.Connection:
        if self._read_only:
            uri = f"file:{self._path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _setup_vec(self) -> bool:
        """Load sqlite-vec into this connection; False disables the vector
        channel (text search still works, embeddings still stored)."""
        try:
            import sqlite_vec  # noqa: PLC0415

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            return True
        except Exception:
            logger.warning(
                "sqlite-vec unavailable; vector search disabled",
                exc_info=True,
                extra={"event": "sqlite_vec_unavailable"},
            )
            return False

    def _settings(self) -> dict[str, str]:
        try:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        except sqlite3.Error:
            return {}
        return {str(row["key"]): str(row["value"]) for row in rows}

    def _migrate_or_recreate(self) -> None:
        (user_version,) = self._conn.execute("PRAGMA user_version").fetchone()
        settings = self._settings()
        expected = {
            "embedding_model": self._embedding_model,
            "embedding_dim": str(self._embedding_dim),
        }
        fresh = user_version == 0 and not settings
        stale = not fresh and (
            user_version != INDEX_SCHEMA_VERSION
            or any(settings.get(key) != value for key, value in expected.items())
        )
        if stale:
            logger.info(
                "search index is stale (schema/model changed); rebuilding from scratch",
                extra={"event": "index_recreated"},
            )
            self._conn.close()
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self._path}{suffix}").unlink(missing_ok=True)
            self._conn = self._open()
            self.vec_available = self._setup_vec()
            fresh = True
        if fresh:
            with self._conn:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
                self._conn.executemany(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    list(expected.items()),
                )
        if self.vec_available:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
                f"  chunk_id INTEGER PRIMARY KEY, embedding float[{self._embedding_dim}] "
                "distance_metric=cosine)"
            )

    def _rebuild_vec_from_blobs(self) -> None:
        """Backfill ``chunks_vec`` from stored BLOBs -- the recovery path for
        an index built while the extension could not load."""
        with self._lock, self._conn:
            (vec_count,) = self._conn.execute("SELECT count(*) FROM chunks_vec").fetchone()
            (blob_count,) = self._conn.execute(
                "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"
            ).fetchone()
            if vec_count >= blob_count:
                return
            self._conn.execute("DELETE FROM chunks_vec")
            self._conn.execute(
                "INSERT INTO chunks_vec(chunk_id, embedding) "
                "SELECT chunk_id, embedding FROM chunks WHERE embedding IS NOT NULL"
            )
        logger.info(
            "chunks_vec rebuilt from stored embeddings",
            extra={"event": "index_vec_backfilled", "chunks": blob_count},
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes (indexer only; serial executor thread) -------------------

    def doc_fingerprint(self, meeting_dir: str, kind: str) -> tuple[int, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT mtime_ns, content_hash FROM docs WHERE meeting_dir = ? AND kind = ?",
                (meeting_dir, kind),
            ).fetchone()
        if row is None:
            return None
        return int(row["mtime_ns"]), str(row["content_hash"])

    def touch_mtime(self, meeting_dir: str, kind: str, mtime_ns: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE docs SET mtime_ns = ? WHERE meeting_dir = ? AND kind = ?",
                (mtime_ns, meeting_dir, kind),
            )

    def upsert_doc(self, doc: DocRecord, chunks: list[ChunkRecord]) -> int:
        """Replace the document (and all its chunks) wholesale; returns doc_id."""
        with self._lock, self._conn:
            old = self._conn.execute(
                "SELECT doc_id FROM docs WHERE meeting_dir = ? AND kind = ?",
                (doc.meeting_dir, doc.kind),
            ).fetchone()
            if old is not None:
                self._delete_doc_locked(int(old["doc_id"]))
            cursor = self._conn.execute(
                "INSERT INTO docs(kind, project, meeting_dir, meeting_title, meeting_date,"
                " speakers, mtime_ns, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.kind,
                    doc.project,
                    doc.meeting_dir,
                    doc.meeting_title,
                    doc.meeting_date,
                    doc.speakers,
                    doc.mtime_ns,
                    doc.content_hash,
                ),
            )
            doc_id = int(cursor.lastrowid or 0)
            for seq, chunk in enumerate(chunks):
                blob = _pack(chunk.embedding) if chunk.embedding is not None else None
                chunk_cursor = self._conn.execute(
                    "INSERT INTO chunks(doc_id, seq, text, start_sec, end_sec, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, seq, chunk.text, chunk.start_sec, chunk.end_sec, blob),
                )
                if self.vec_available and chunk.embedding is not None:
                    self._conn.execute(
                        "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                        (int(chunk_cursor.lastrowid or 0), blob),
                    )
            return doc_id

    def _delete_doc_locked(self, doc_id: int) -> None:
        """Delete one doc + chunks (+ vec rows -- vec0 sits outside the FK
        cascade). Caller holds the lock and an open transaction."""
        if self.vec_available:
            self._conn.execute(
                "DELETE FROM chunks_vec WHERE chunk_id IN"
                " (SELECT chunk_id FROM chunks WHERE doc_id = ?)",
                (doc_id,),
            )
        # Explicit chunk delete (not just the FK cascade) so the FTS delete
        # triggers fire per row.
        self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))

    def delete_docs_not_in(self, live: set[tuple[str, str]]) -> int:
        """Orphan sweep: drop docs whose (meeting_dir, kind) is gone from disk."""
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT doc_id, meeting_dir, kind FROM docs").fetchall()
            removed = 0
            for row in rows:
                if (str(row["meeting_dir"]), str(row["kind"])) not in live:
                    self._delete_doc_locked(int(row["doc_id"]))
                    removed += 1
            return removed

    # -- reads (search + MCP) --------------------------------------------

    def fts_query(self, match: str, limit: int, project: str | None = None) -> list[int]:
        """BM25-ranked doc ids for an FTS5 MATCH over chunk text -- collapsed
        per doc keeping each doc's best rank position."""
        sql = (
            "SELECT chunks.doc_id AS doc_id, min(chunks_fts.rank) AS best_rank"
            " FROM chunks_fts JOIN chunks ON chunks.chunk_id = chunks_fts.rowid"
            " JOIN docs ON docs.doc_id = chunks.doc_id"
            " WHERE chunks_fts MATCH ?"
        )
        params: list[object] = [match]
        if project is not None:
            sql += " AND docs.project = ?"
            params.append(project)
        sql += " GROUP BY chunks.doc_id ORDER BY best_rank LIMIT ?"
        params.append(limit)
        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # An unparseable MATCH (stray quotes and such) yields no
                # channel, not a 500.
                return []
        return [int(row["doc_id"]) for row in rows]

    def best_chunk_for(self, doc_id: int, match: str) -> tuple[str, float | None] | None:
        """The best-matching chunk's (text, start_sec) for snippeting."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT chunks.text AS text, chunks.start_sec AS start_sec"
                    " FROM chunks_fts JOIN chunks ON chunks.chunk_id = chunks_fts.rowid"
                    " WHERE chunks_fts MATCH ? AND chunks.doc_id = ?"
                    " ORDER BY chunks_fts.rank LIMIT 1",
                    (match, doc_id),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is None:
                row = self._conn.execute(
                    "SELECT text, start_sec FROM chunks WHERE doc_id = ? ORDER BY seq LIMIT 1",
                    (doc_id,),
                ).fetchone()
        if row is None:
            return None
        start = row["start_sec"]
        return str(row["text"]), (float(start) if start is not None else None)

    def title_trigram_query(self, match: str, limit: int, project: str | None = None) -> list[int]:
        sql = "SELECT rowid AS doc_id FROM titles_fts WHERE titles_fts MATCH ?"
        params: list[object] = [match]
        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return []
        return [int(row["doc_id"]) for row in rows]

    def exact_title_docs(self, query: str, project: str | None = None) -> list[int]:
        """Docs whose meeting title contains the query, case-insensitively."""
        needle = query.strip().casefold()
        if not needle:
            return []
        sql = "SELECT doc_id, meeting_title, project FROM docs"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            int(row["doc_id"])
            for row in rows
            if needle in str(row["meeting_title"]).casefold()
            and (project is None or str(row["project"]) == project)
        ]

    def vec_query(
        self, embedding: list[float], k: int, project: str | None = None
    ) -> list[tuple[int, int]]:
        """Nearest chunks by cosine distance: ``(doc_id, chunk_id)`` pairs in
        rank order, collapsed per doc keeping the best chunk."""
        if not self.vec_available:
            return []
        fetch_k = max(k * VEC_OVERFETCH_FACTOR, VEC_MIN_K)
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT chunk_id, distance FROM chunks_vec"
                    " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (_pack(embedding), fetch_k),
                ).fetchall()
            except sqlite3.OperationalError:
                # A read-only open of an index built without the extension
                # has no chunks_vec table at all; text channels still serve.
                return []
            pairs: list[tuple[int, int]] = []
            seen_docs: set[int] = set()
            for row in rows:
                chunk = self._conn.execute(
                    "SELECT chunks.doc_id AS doc_id, docs.project AS project"
                    " FROM chunks JOIN docs ON docs.doc_id = chunks.doc_id"
                    " WHERE chunks.chunk_id = ?",
                    (int(row["chunk_id"]),),
                ).fetchone()
                if chunk is None:
                    continue
                if project is not None and str(chunk["project"]) != project:
                    continue
                doc_id = int(chunk["doc_id"])
                if doc_id in seen_docs:
                    continue
                seen_docs.add(doc_id)
                pairs.append((doc_id, int(row["chunk_id"])))
                if len(pairs) >= k:
                    break
        return pairs

    def get_docs(self, doc_ids: list[int]) -> dict[int, DocRow]:
        if not doc_ids:
            return {}
        placeholders = ",".join("?" for _ in doc_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM docs WHERE doc_id IN ({placeholders})",  # noqa: S608
                doc_ids,
            ).fetchall()
        return {
            int(row["doc_id"]): DocRow(
                doc_id=int(row["doc_id"]),
                kind=str(row["kind"]),
                project=str(row["project"]),
                meeting_dir=str(row["meeting_dir"]),
                meeting_title=str(row["meeting_title"]),
                meeting_date=(
                    str(row["meeting_date"]) if row["meeting_date"] is not None else None
                ),
                speakers=str(row["speakers"]),
            )
            for row in rows
        }

    def get_chunk(self, chunk_id: int) -> tuple[str, float | None] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT text, start_sec FROM chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
        if row is None:
            return None
        start = row["start_sec"]
        return str(row["text"]), (float(start) if start is not None else None)

    def doc_count(self) -> int:
        with self._lock:
            (count,) = self._conn.execute("SELECT count(*) FROM docs").fetchone()
        return int(count)
