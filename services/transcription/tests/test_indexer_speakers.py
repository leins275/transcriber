"""Speaker tags and speaker-naming breadcrumbs from the vault indexer (T2).

A synthetic vault under tmp_path, a real tmp-path SQLite index and
FakeEmbedder -- model-free, GPU-free, network-free. Every assertion is on
what a later query can observe (retrieval by speaker, stored chunk text,
the text handed to the embedder), never on the indexer's internals.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import FakeEmbedder

from transcription.search.index_db import IndexDb
from transcription.search.indexer import index_vault

LONG_LINE = "Обсуждали дедлайн по проекту и планы на следующую неделю в подробностях. "

ACME_DIR = "ACME/260831 - Weekly sync"
UNSORTED_DIR = "unsorted/260830 - voice memo"

TRANSCRIPT_BREADCRUMB = "[ACME / 260831 - Weekly sync / 0:00–0:04 / Speaker 1, Speaker 2]"
NOTE_BREADCRUMB = "[ACME / 260831 - Weekly sync / note]"
UNLABELLED_BREADCRUMB = "[unsorted / 260830 - voice memo / 0:00–0:02]"


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
    """ACME: a diarized transcript (Speaker 1 / Speaker 2 alternating) plus a
    summary and a note; `unsorted`: a transcript with the same words and no
    speaker labels at all."""
    root = tmp_path / "vault"
    meeting = root / "ACME" / "260831 - Weekly sync"
    _write_transcript(meeting, texts=[LONG_LINE] * 4, speakers=["Speaker 1", "Speaker 2"])
    (meeting / "summary.md").write_text(
        "# Summary\n\nThe deadline moved to Friday after a long discussion.", encoding="utf-8"
    )
    (meeting / "note.md").write_text(
        "Remember to send the follow-up materials to the whole team.", encoding="utf-8"
    )
    _write_transcript(root / "unsorted" / "260830 - voice memo", texts=[LONG_LINE] * 2)
    return root


@pytest.fixture
def db(tmp_path: Path) -> Iterator[IndexDb]:
    handle = IndexDb(
        tmp_path / "index.sqlite3",
        embedding_model=FakeEmbedder.name,
        embedding_dim=FakeEmbedder.DIM,
    )
    yield handle
    handle.close()


def _located(db: IndexDb, doc_ids: list[int]) -> set[tuple[str, str]]:
    """The (meeting_dir, kind) pairs the given doc ids stand for."""
    return {(row.meeting_dir, row.kind) for row in db.get_docs(doc_ids).values()}


def _chunk_text(db: IndexDb, match: str, *, meeting_dir: str, kind: str) -> str:
    """The stored text of the given document's chunk best matching `match`."""
    docs = db.get_docs(db.fts_query(match, 10))
    doc_id = next(
        found for found, row in docs.items() if row.meeting_dir == meeting_dir and row.kind == kind
    )
    chunk = db.best_chunk_for(doc_id, match)
    assert chunk is not None, f"no chunk for {meeting_dir}/{kind}"
    return chunk[0]


def test_a_speaker_filter_finds_only_the_meeting_where_that_speaker_speaks(
    vault: Path, db: IndexDb
) -> None:
    """Both transcripts contain "дедлайн"; only the diarized one is tagged."""
    index_vault(vault, db, FakeEmbedder())

    hits = db.fts_query("дедлайн", 10, speakers={"speaker 1"})

    assert _located(db, hits) == {(ACME_DIR, "transcript")}


def test_a_speaker_absent_from_the_vault_matches_nothing(vault: Path, db: IndexDb) -> None:
    index_vault(vault, db, FakeEmbedder())

    hits = db.fts_query("дедлайн", 10, speakers={"nobody"})

    assert hits == []


def test_an_operator_name_replaces_the_diarization_label_it_overrides(
    vault: Path, db: IndexDb
) -> None:
    """`speakers.json` renames every "Speaker 1" line, so the meeting becomes
    reachable as Даниил and stops being reachable as Speaker 1."""
    (vault / "ACME" / "260831 - Weekly sync" / "speakers.json").write_text(
        json.dumps({"schema_version": 1, "assignments": {"0": "Даниил", "2": "Даниил"}}),
        encoding="utf-8",
    )

    index_vault(vault, db, FakeEmbedder())

    assert _located(db, db.fts_query("дедлайн", 10, speakers={"даниил"})) == {
        (ACME_DIR, "transcript")
    }
    assert db.fts_query("дедлайн", 10, speakers={"speaker 1"}) == []


def test_only_speakers_with_transcript_lines_become_known(vault: Path, db: IndexDb) -> None:
    """The unlabelled `unsorted` transcript contributes no tag."""
    index_vault(vault, db, FakeEmbedder())

    known = db.known_speakers()

    assert set(known) == {"speaker 1", "speaker 2"}


def test_summaries_and_notes_are_unreachable_through_a_speaker_filter(
    vault: Path, db: IndexDb
) -> None:
    """Their meeting has speakers, but the docs themselves carry no tags."""
    index_vault(vault, db, FakeEmbedder())

    assert db.fts_query("Friday", 10, speakers={"speaker 1"}) == []
    assert db.fts_query("materials", 10, speakers={"speaker 1"}) == []


def test_a_transcript_breadcrumb_names_its_speakers_in_order_of_first_appearance(
    vault: Path, db: IndexDb
) -> None:
    index_vault(vault, db, FakeEmbedder())

    text = _chunk_text(db, "дедлайн", meeting_dir=ACME_DIR, kind="transcript")

    assert text.splitlines()[0] == TRANSCRIPT_BREADCRUMB


def test_a_transcript_without_speaker_labels_gets_a_breadcrumb_without_them(
    vault: Path, db: IndexDb
) -> None:
    index_vault(vault, db, FakeEmbedder())

    text = _chunk_text(db, "дедлайн", meeting_dir=UNSORTED_DIR, kind="transcript")

    assert text.splitlines()[0] == UNLABELLED_BREADCRUMB


def test_a_notes_breadcrumb_is_unchanged(vault: Path, db: IndexDb) -> None:
    index_vault(vault, db, FakeEmbedder())

    text = _chunk_text(db, "materials", meeting_dir=ACME_DIR, kind="note")

    assert text.splitlines()[0] == NOTE_BREADCRUMB


def test_the_text_handed_to_the_embedder_names_the_speakers(vault: Path, db: IndexDb) -> None:
    embedder = FakeEmbedder()

    index_vault(vault, db, embedder)

    embedded_first_lines = [text.splitlines()[0] for batch in embedder.calls for text in batch]
    assert TRANSCRIPT_BREADCRUMB in embedded_first_lines
