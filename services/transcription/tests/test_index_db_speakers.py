"""Per-chunk speaker tags and speaker-filtered reads (`search/index_db.py`).

Real SQLite files under tmp_path (the index is a managed dependency and is
never mocked); `FakeEmbedder` supplies model-free vectors. The vector-channel
case skips when sqlite-vec cannot load, as `test_index_db.py` does.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
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
        "meeting_dir": "ACME/260831 - Weekly sync",
        "meeting_title": "Weekly sync",
        "meeting_date": "2026-08-31",
        "speakers": "Иван Петров Anna",
        "mtime_ns": 1,
        "content_hash": "h1",
    }
    values.update(overrides)
    return DocRecord(**values)  # type: ignore[arg-type]


def _chunk(
    text: str,
    speakers: tuple[str, ...],
    *,
    start_sec: float = 0.0,
    end_sec: float = 4.0,
    embedding: list[float] | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        text=text,
        start_sec=start_sec,
        end_sec=end_sec,
        embedding=embedding,
        speakers=speakers,
    )


def _open(tmp_path: Path, **kwargs: object) -> IndexDb:
    return IndexDb(
        tmp_path / "index.sqlite3",
        embedding_model=MODEL,
        embedding_dim=DIM,
        **kwargs,  # type: ignore[arg-type]
    )


def _set_user_version(path: Path, version: int) -> None:
    """Hand-write `PRAGMA user_version` on a closed index file."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _raw_doc_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        (count,) = conn.execute("SELECT count(*) FROM docs").fetchone()
    finally:
        conn.close()
    return int(count)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[IndexDb]:
    handle = _open(tmp_path)
    yield handle
    handle.close()


# -- the filter finds the speaker's chunks ------------------------------


def test_a_chunk_is_found_by_a_speaker_who_has_a_line_in_it(db: IndexDb) -> None:
    doc_id = db.upsert_doc(_doc(), [_chunk("обсуждали дедлайн релиза", ("Иван Петров",))])

    assert db.fts_query("дедлайн", 10, speakers={"иван петров"}) == [doc_id]


def test_a_chunk_is_not_found_by_a_speaker_with_no_line_in_it(db: IndexDb) -> None:
    db.upsert_doc(_doc(), [_chunk("обсуждали дедлайн релиза", ("Иван Петров",))])

    assert db.fts_query("дедлайн", 10, speakers={"anna"}) == []


def test_the_speaker_filter_matches_a_cyrillic_name_regardless_of_case(db: IndexDb) -> None:
    doc_id = db.upsert_doc(_doc(), [_chunk("обсуждали дедлайн релиза", ("ИВАН ПЕТРОВ",))])

    assert db.fts_query("дедлайн", 10, speakers={"иван петров"}) == [doc_id]


def test_best_chunk_for_returns_a_chunk_the_filtered_speaker_took_part_in(db: IndexDb) -> None:
    # The Anna chunk outranks the Иван chunk on this MATCH (three hits in a
    # much shorter text), so an unfiltered snippet would come from Anna.
    ivan_line = "Иван Петров: обсуждали дедлайн релиза и всё, что к нему прилагается сверх меры"
    doc_id = db.upsert_doc(
        _doc(),
        [
            _chunk("Anna: дедлайн дедлайн дедлайн", ("Anna",), start_sec=0.0, end_sec=4.0),
            _chunk(ivan_line, ("Иван Петров",), start_sec=4.0, end_sec=8.0),
        ],
    )

    assert db.best_chunk_for(doc_id, "дедлайн", speakers={"иван петров"}) == (ivan_line, 4.0)


def test_exact_title_docs_filtered_by_speaker_returns_only_docs_with_a_tagged_chunk(
    db: IndexDb,
) -> None:
    ivan_doc = db.upsert_doc(_doc(), [_chunk("его реплика про дедлайн", ("Иван Петров",))])
    db.upsert_doc(
        _doc(meeting_dir="ACME/260830 - Weekly sync", content_hash="h2"),
        [_chunk("её реплика про дедлайн", ("Anna",))],
    )

    assert db.exact_title_docs("weekly sync", speakers={"иван петров"}) == [ivan_doc]


def test_title_trigram_query_filtered_by_speaker_returns_only_docs_with_a_tagged_chunk(
    db: IndexDb,
) -> None:
    ivan_doc = db.upsert_doc(_doc(), [_chunk("его реплика про дедлайн", ("Иван Петров",))])
    db.upsert_doc(
        _doc(meeting_dir="ACME/260830 - Weekly sync", content_hash="h2"),
        [_chunk("её реплика про дедлайн", ("Anna",))],
    )

    assert db.title_trigram_query("weekl", 10, speakers={"иван петров"}) == [ivan_doc]


def test_vec_query_filtered_by_speaker_returns_only_that_speakers_chunks(db: IndexDb) -> None:
    if not db.vec_available:
        pytest.skip("sqlite-vec not loadable in this environment")
    (ivan_vector, anna_vector) = FakeEmbedder().embed(["его реплика", "её реплика"])
    ivan_doc = db.upsert_doc(
        _doc(), [_chunk("его реплика", ("Иван Петров",), embedding=ivan_vector)]
    )
    db.upsert_doc(
        _doc(meeting_dir="ACME/260830 - Weekly sync", content_hash="h2"),
        [_chunk("её реплика", ("Anna",), embedding=anna_vector)],
    )

    hits = db.vec_query(anna_vector, k=2, speakers={"иван петров"})

    assert [doc_id for doc_id, _ in hits] == [ivan_doc]


def test_a_filter_naming_an_unknown_speaker_finds_nothing_in_any_text_channel(
    db: IndexDb,
) -> None:
    doc_id = db.upsert_doc(_doc(), [_chunk("обсуждали дедлайн релиза", ("Иван Петров",))])
    nobody = {"пётр сидоров"}

    assert db.fts_query("дедлайн", 10, speakers=nobody) == []
    assert db.title_trigram_query("weekl", 10, speakers=nobody) == []
    assert db.exact_title_docs("weekly sync", speakers=nobody) == []
    assert db.best_chunk_for(doc_id, "дедлайн", speakers=nobody) is None


def test_an_unfiltered_read_still_returns_every_doc(db: IndexDb) -> None:
    ivan_doc = db.upsert_doc(_doc(), [_chunk("его реплика про дедлайн", ("Иван Петров",))])
    anna_doc = db.upsert_doc(
        _doc(meeting_dir="ACME/260830 - Weekly sync", content_hash="h2"),
        [_chunk("её реплика про дедлайн", ("Anna",))],
    )

    assert sorted(db.fts_query("дедлайн", 10)) == sorted([ivan_doc, anna_doc])


# -- the known-speaker roster -------------------------------------------


def test_known_speakers_lists_the_distinct_casefolded_names_of_the_index(db: IndexDb) -> None:
    db.upsert_doc(
        _doc(),
        [
            _chunk("первая реплика", ("Иван Петров", "Anna")),
            _chunk("вторая реплика", ("Anna",)),
        ],
    )

    assert sorted(db.known_speakers()) == ["anna", "иван петров"]


def test_known_speakers_is_scoped_to_one_project(db: IndexDb) -> None:
    db.upsert_doc(_doc(), [_chunk("его реплика", ("Иван Петров",))])
    db.upsert_doc(
        _doc(project="OTHER", meeting_dir="OTHER/260830 - Standup", content_hash="h2"),
        [_chunk("её реплика", ("Anna",))],
    )

    assert db.known_speakers("ACME") == ["иван петров"]


def test_replacing_a_doc_forgets_the_speakers_of_its_old_chunks(db: IndexDb) -> None:
    db.upsert_doc(_doc(), [_chunk("старая реплика", ("Иван Петров",))])

    db.upsert_doc(_doc(mtime_ns=2, content_hash="h2"), [_chunk("новая реплика", ("Anna",))])

    assert db.known_speakers() == ["anna"]


def test_sweeping_a_doc_gone_from_disk_forgets_its_speakers(db: IndexDb) -> None:
    db.upsert_doc(_doc(), [_chunk("его реплика", ("Иван Петров",))])
    db.upsert_doc(
        _doc(meeting_dir="ACME/260830 - Weekly sync", content_hash="h2"),
        [_chunk("её реплика", ("Anna",))],
    )

    db.delete_docs_not_in({("ACME/260830 - Weekly sync", "transcript")})

    assert db.known_speakers() == ["anna"]


def test_a_chunk_stored_without_speakers_round_trips_and_carries_no_tags(db: IndexDb) -> None:
    doc_id = db.upsert_doc(_doc(), [ChunkRecord(text="реплика без спикера")])

    assert db.fts_query("реплика", 10) == [doc_id]
    assert db.known_speakers() == []


# -- schema version 2 ----------------------------------------------------


def test_an_index_from_an_older_schema_version_is_recreated_empty(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("его реплика", ("Иван Петров",))])
    db.close()
    _set_user_version(tmp_path / "index.sqlite3", 1)

    reopened = _open(tmp_path)

    assert reopened.doc_count() == 0
    reopened.close()


def test_an_older_schema_version_opened_read_only_is_reported_stale(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("его реплика", ("Иван Петров",))])
    db.close()
    _set_user_version(tmp_path / "index.sqlite3", 1)

    reader = _open(tmp_path, read_only=True)

    assert reader.schema_stale is True
    reader.close()
    assert _raw_doc_count(tmp_path / "index.sqlite3") == 1


def test_a_current_schema_version_opened_read_only_is_not_stale(tmp_path: Path) -> None:
    db = _open(tmp_path)
    db.upsert_doc(_doc(), [_chunk("его реплика", ("Иван Петров",))])
    db.close()

    reader = _open(tmp_path, read_only=True)

    assert reader.schema_stale is False
    reader.close()
