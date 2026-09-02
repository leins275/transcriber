"""Tests for the incremental vault indexer (`search/indexer.py`).

A synthetic vault under tmp_path; FakeEmbedder keeps everything model-free.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fakes import FakeEmbedder

from transcription.errors import ErrorKind, ServiceError
from transcription.providers.base import CancelToken
from transcription.search.index_db import IndexDb
from transcription.search.indexer import index_vault

LONG_LINE = "Обсуждали дедлайн по проекту и планы на следующую неделю в подробностях. "


def _write_transcript(
    meeting_dir: Path, *, texts: list[str], speakers: list[str] | None = None
) -> None:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    segments = []
    for index, text in enumerate(texts):
        segment: dict[str, object] = {
            "id": index,
            "start": float(index),
            "end": float(index) + 1.0,
            "text": text,
        }
        if speakers is not None:
            segment["speaker"] = speakers[index % len(speakers)]
        segments.append(segment)
    (meeting_dir / "transcript.json").write_text(
        json.dumps({"schema_version": 1, "text": " ".join(texts), "segments": segments}),
        encoding="utf-8",
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    meeting = root / "ACME" / "260831 - Weekly sync"
    _write_transcript(meeting, texts=[LONG_LINE] * 4, speakers=["Speaker 1", "Speaker 2"])
    (meeting / "summary.md").write_text(
        "# Summary\n\nThe deadline moved to Friday after a long discussion.", encoding="utf-8"
    )
    (meeting / "note.md").write_text(
        "Remember to send the follow-up materials to the whole team.", encoding="utf-8"
    )
    unsorted = root / "unsorted" / "260830 - voice memo"
    _write_transcript(unsorted, texts=[LONG_LINE] * 2)
    # Legacy trees the walk must never enter.
    (root / "reports" / "260801").mkdir(parents=True)
    (root / "ACME" / "260831 - Weekly sync" / "exports").mkdir()
    return root


@pytest.fixture
def db(tmp_path: Path) -> IndexDb:
    handle = IndexDb(
        tmp_path / "index.sqlite3",
        embedding_model=FakeEmbedder.name,
        embedding_dim=FakeEmbedder.DIM,
    )
    yield handle
    handle.close()


def test_first_run_indexes_all_three_kinds(vault: Path, db: IndexDb) -> None:
    stats = index_vault(vault, db, FakeEmbedder())

    # ACME: transcript+summary+note; unsorted: transcript.
    assert stats.indexed == 4
    assert stats.skipped == 0
    assert db.doc_count() == 4
    assert len(db.fts_query("дедлайн", 10)) >= 1
    assert len(db.fts_query("Friday", 10)) == 1


def test_dot_dirs_at_the_vault_root_are_never_projects(vault: Path, db: IndexDb) -> None:
    """`.transcriber/` (the index's own home) and friends must not index."""
    hidden = vault / ".transcriber" / "260901 - looks like a meeting"
    hidden.mkdir(parents=True)
    (hidden / "summary.md").write_text("Should never be indexed.", encoding="utf-8")

    stats = index_vault(vault, db, FakeEmbedder())

    assert stats.indexed == 4  # the fixture's docs only
    assert db.fts_query("never be indexed", 10) == []


def test_second_run_over_an_unchanged_vault_skips_everything(vault: Path, db: IndexDb) -> None:
    index_vault(vault, db, FakeEmbedder())
    embedder = FakeEmbedder()

    stats = index_vault(vault, db, embedder)

    assert stats.indexed == 0
    assert stats.skipped == 4
    assert embedder.calls == []  # nothing re-embedded


def test_docs_indexed_without_a_model_reembed_once_one_appears(vault: Path, db: IndexDb) -> None:
    """The model-downloaded-later case: text-only rows must not stay frozen
    behind the mtime/hash skip forever."""
    first = index_vault(vault, db, None)  # no embedding model yet
    assert first.indexed == 4

    embedder = FakeEmbedder()
    second = index_vault(vault, db, embedder)

    assert second.indexed == 4  # every text-only doc re-embedded
    assert second.skipped == 0
    assert embedder.calls, "the re-run must actually embed"
    assert db.docs_missing_embeddings() == set()

    third = index_vault(vault, db, FakeEmbedder())
    assert third.indexed == 0  # and the skip discipline is back
    assert third.skipped == 4


def test_a_touched_but_identical_file_updates_mtime_only(vault: Path, db: IndexDb) -> None:
    index_vault(vault, db, FakeEmbedder())
    note = vault / "ACME" / "260831 - Weekly sync" / "note.md"
    later = time.time() + 60
    os.utime(note, (later, later))
    embedder = FakeEmbedder()

    stats = index_vault(vault, db, embedder)

    assert stats.indexed == 0
    assert stats.skipped == 4
    assert embedder.calls == []


def test_an_edited_file_is_reindexed(vault: Path, db: IndexDb) -> None:
    index_vault(vault, db, FakeEmbedder())
    note = vault / "ACME" / "260831 - Weekly sync" / "note.md"
    note.write_text("A completely different note about kittens and roadmaps.", encoding="utf-8")

    stats = index_vault(vault, db, FakeEmbedder())

    assert stats.indexed == 1
    assert len(db.fts_query("kittens", 10)) == 1
    assert db.fts_query("materials", 10) == []


def test_a_deleted_meeting_is_swept(vault: Path, db: IndexDb) -> None:
    index_vault(vault, db, FakeEmbedder())
    note = vault / "ACME" / "260831 - Weekly sync" / "note.md"
    note.unlink()

    stats = index_vault(vault, db, FakeEmbedder())

    assert stats.removed == 1
    assert db.fts_query("materials", 10) == []


def test_speaker_overrides_reach_chunk_text_and_the_speakers_column(
    vault: Path, db: IndexDb
) -> None:
    meeting = vault / "ACME" / "260831 - Weekly sync"
    (meeting / "speakers.json").write_text(
        json.dumps({"schema_version": 1, "assignments": {"0": "Даниил"}}), encoding="utf-8"
    )

    index_vault(vault, db, FakeEmbedder())

    assert len(db.fts_query("Даниил", 10)) == 1
    assert len(db.title_trigram_query("Даниил", 10)) == 1


def test_a_tiny_note_is_dropped_as_noise(vault: Path, db: IndexDb) -> None:
    (vault / "ACME" / "260831 - Weekly sync" / "note.md").write_text("ok", encoding="utf-8")

    stats = index_vault(vault, db, FakeEmbedder())

    # The note doc exists (its file does) but carries no chunks.
    assert stats.indexed == 4
    assert db.fts_query("ok", 10) == []


def test_a_failing_embedder_degrades_to_text_only_with_a_warning(vault: Path, db: IndexDb) -> None:
    stats = index_vault(vault, db, FakeEmbedder(raise_kind=ErrorKind.MODEL_LOAD))

    assert stats.indexed == 4
    assert any("embedding" in warning for warning in stats.warnings)
    assert len(db.fts_query("дедлайн", 10)) >= 1


def test_cancellation_propagates_between_batches(vault: Path, db: IndexDb) -> None:
    cancel = CancelToken()
    cancel.set()

    with pytest.raises(ServiceError) as exc_info:
        index_vault(vault, db, FakeEmbedder(), cancel=cancel)

    assert exc_info.value.kind is ErrorKind.CANCELLED


def test_an_unparseable_transcript_warns_and_continues(vault: Path, db: IndexDb) -> None:
    broken = vault / "ACME" / "260829 - Broken"
    broken.mkdir()
    (broken / "transcript.json").write_text("{not json", encoding="utf-8")

    stats = index_vault(vault, db, FakeEmbedder())

    assert stats.indexed == 4
    assert any("Broken" in warning for warning in stats.warnings)
