"""Tests for the rebuildable search-index database (`search/index_db.py`).

Real SQLite files under tmp_path; sqlite-vec loads if installed and the
vec-unavailable degradation is exercised by monkeypatching the loader.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeEmbedder

from transcription.search.index_db import ChunkRecord, DocRecord, IndexDb

MODEL = "fake-embedder"
DIM = FakeEmbedder.DIM


def _doc(**overrides: object) -> DocRecord:
    values: dict[str, object] = {
        "kind": "transcript",
        "project": "ACME",
        "meeting_dir": "ACME/260831 - Sync",
        "meeting_title": "Sync",
        "meeting_date": "2026-08-31",
        "speakers": "Даниил Anna",
        "mtime_ns": 1,
        "content_hash": "h1",
    }
    values.update(overrides)
    return DocRecord(**values)  # type: ignore[arg-type]


def _chunk(text: str, embedding: list[float] | None = None) -> ChunkRecord:
    return ChunkRecord(text=text, start_sec=0.0, end_sec=1.0, embedding=embedding)


def _open(tmp_path: Path, **kwargs: object) -> IndexDb:
    return IndexDb(
        tmp_path / "index.sqlite3",
        embedding_model=MODEL,
        embedding_dim=DIM,
        **kwargs,  # type: ignore[arg-type]
    )


def test_upsert_and_fts_roundtrip_including_cyrillic(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("обсуждали дедлайн по проекту"), _chunk("shipping the beta")])

    assert db.fts_query("дедлайн", 10) == db.fts_query("дедлайн", 10)
    assert len(db.fts_query("дедлайн", 10)) == 1
    assert len(db.fts_query("beta", 10)) == 1
    assert db.fts_query("nonexistent", 10) == []
    db.close()


def test_replacing_a_doc_removes_its_old_chunks_from_fts(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("old unique wording")])
    db.upsert_doc(_doc(mtime_ns=2, content_hash="h2"), [_chunk("new unique wording")])

    assert db.fts_query("old", 10) == []
    assert len(db.fts_query("new", 10)) == 1
    assert db.doc_count() == 1
    db.close()


def test_orphan_sweep_removes_docs_gone_from_disk(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("alpha text one")])
    db.upsert_doc(
        _doc(meeting_dir="ACME/260830 - Old", content_hash="h2"), [_chunk("beta text two")]
    )

    removed = db.delete_docs_not_in({("ACME/260831 - Sync", "transcript")})

    assert removed == 1
    assert db.doc_count() == 1
    assert db.fts_query("beta", 10) == []
    db.close()


def test_an_embedding_model_change_recreates_the_file(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("some indexed text here")])
    db.close()

    reopened = IndexDb(tmp_path / "index.sqlite3", embedding_model="other-model", embedding_dim=DIM)
    assert reopened.doc_count() == 0
    reopened.close()


def test_a_dimension_change_recreates_the_file(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("some indexed text here")])
    db.close()

    reopened = IndexDb(tmp_path / "index.sqlite3", embedding_model=MODEL, embedding_dim=DIM * 2)
    assert reopened.doc_count() == 0
    reopened.close()


def test_a_matching_reopen_keeps_the_data(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("survives a clean reopen")])
    db.close()

    reopened = _open(tmp_path)
    assert reopened.doc_count() == 1
    assert len(reopened.fts_query("survives", 10)) == 1
    reopened.close()


def test_vector_query_returns_nearest_doc_when_vec_is_available(tmp_path: Path) -> None:
    db = _open(tmp_path)
    if not db.vec_available:
        db.close()
        pytest.skip("sqlite-vec not loadable in this environment")
    embedder = FakeEmbedder()
    (vec_a, vec_b) = embedder.embed(["первый текст", "второй текст"])
    db.upsert_doc(_doc(), [_chunk("первый текст", vec_a)])
    db.upsert_doc(
        _doc(meeting_dir="ACME/260830 - Other", content_hash="h2"), [_chunk("второй текст", vec_b)]
    )

    hits = db.vec_query(vec_a, k=2)

    assert len(hits) == 2
    top_doc = db.get_docs([hits[0][0]])[hits[0][0]]
    assert top_doc.meeting_dir == "ACME/260831 - Sync"
    db.close()


def test_vec_unavailable_degrades_to_empty_vector_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(IndexDb, "_setup_vec", lambda self: False)
    db = _open(tmp_path)
    embedder = FakeEmbedder()
    (vector,) = embedder.embed(["stored anyway"])
    db.upsert_doc(_doc(), [_chunk("stored anyway", vector)])

    assert db.vec_query(vector, k=5) == []
    # Text search still works over the same rows.
    assert len(db.fts_query("stored", 10)) == 1
    db.close()


def test_vec_table_is_rebuilt_from_blobs_on_a_healthy_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build the index while the extension "cannot load"...
    monkeypatch.setattr(IndexDb, "_setup_vec", lambda self: False)
    db = _open(tmp_path)
    embedder = FakeEmbedder()
    (vector,) = embedder.embed(["needs no re-embedding"])
    db.upsert_doc(_doc(), [_chunk("needs no re-embedding", vector)])
    db.close()
    monkeypatch.undo()

    # ...then reopen with it working: the BLOBs backfill chunks_vec.
    reopened = _open(tmp_path)
    if not reopened.vec_available:
        reopened.close()
        pytest.skip("sqlite-vec not loadable in this environment")
    hits = reopened.vec_query(vector, k=1)
    assert len(hits) == 1
    reopened.close()


def test_trigram_title_matching_tolerates_partial_names(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(
        _doc(meeting_title="Security retrospective"), [_chunk("body long enough to keep")]
    )

    assert len(db.title_trigram_query("retrospec", 10)) == 1
    assert db.title_trigram_query("zzz", 10) == []
    db.close()


def test_exact_title_matching_is_case_insensitive(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(meeting_title="Security retro"), [_chunk("body long enough to keep")])

    assert len(db.exact_title_docs("security RETRO")) == 1
    assert db.exact_title_docs("security", project="OTHER") == []
    db.close()


def test_a_malformed_fts_match_yields_no_channel_not_an_error(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("perfectly fine text")])

    assert db.fts_query('"unterminated', 10) == []
    db.close()


def test_read_only_open_serves_queries_without_migrating(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("visible to readers")])
    db.close()

    reader = _open(tmp_path, read_only=True)
    assert len(reader.fts_query("visible", 10)) == 1
    reader.close()
