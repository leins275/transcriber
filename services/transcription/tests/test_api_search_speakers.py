"""Speaker-scoped hybrid search over the HTTP surface: the `speaker`
parameter of `POST /v1/search` (FR-4).

Same shape as `test_api_search.py` -- a real tmp-file index built by the
indexer with `FakeEmbedder`, no model and no network. The vault holds two
meetings: an ACME retro where Иван Петров (an operator name assigned in
`speakers.json`) and Анна Кузнецова (a diarization label) each speak, plus
an OTHER kickoff where only Анна speaks. Both meetings talk about the
дедлайн, so an unscoped query finds both and only the filter can tell them
apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import FakeEmbedder
from fastapi.testclient import TestClient

from transcription.app import create_app
from transcription.config import Config
from transcription.jobs import JobManager
from transcription.ledger import Ledger
from transcription.search.index_db import IndexDb
from transcription.search.indexer import index_vault

AUTH = {"Authorization": "Bearer test-token"}

ACME_RETRO = "ACME/260831 - Security retro"
OTHER_KICKOFF = "OTHER/260830 - Kickoff"

# A phrase only Иван's lines carry, so a snippet proves which chunk was
# picked. Each segment's text is long enough to exceed the indexer's
# per-chunk token budget on its own, which keeps every speaker's lines in
# chunks of their own.
IVAN_TEXT = "Обсудили квартальный отчёт и дедлайн по проекту, а также планы на неделю. " * 16
ANNA_TEXT = "Обсудили релиз и дедлайн, дедлайн держим, и планы на следующую неделю. " * 16


def _write_meeting(
    root: Path,
    meeting_dir: str,
    *,
    labels: list[str],
    overrides: dict[str, str] | None = None,
    note: str | None = None,
) -> None:
    """One meeting whose segment `i` is spoken by `labels[i]`."""
    meeting = root / meeting_dir
    meeting.mkdir(parents=True)
    segments = [
        {
            "id": index,
            "start": float(index) * 60.0,
            "end": float(index) * 60.0 + 59.0,
            "text": IVAN_TEXT if label == "SPEAKER_00" else ANNA_TEXT,
            "speaker": label,
        }
        for index, label in enumerate(labels)
    ]
    (meeting / "transcript.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "text": "\n".join(str(seg["text"]) for seg in segments),
                "segments": segments,
            }
        ),
        encoding="utf-8",
    )
    if overrides is not None:
        (meeting / "speakers.json").write_text(
            json.dumps({"assignments": overrides}), encoding="utf-8"
        )
    if note is not None:
        (meeting / "note.md").write_text(note, encoding="utf-8")


@pytest.fixture
def vault_root(tmp_app_dir: Path) -> Path:
    root = tmp_app_dir / "vault"
    _write_meeting(
        root,
        ACME_RETRO,
        labels=["SPEAKER_00", "SPEAKER_01", "SPEAKER_01", "SPEAKER_01"],
        overrides={"0": "Иван Петров"},
        note="Дедлайн по проекту зафиксирован, письмо всей команде отправим завтра утром.",
    )
    _write_meeting(root, OTHER_KICKOFF, labels=["Анна Кузнецова", "Анна Кузнецова"])
    return root


@pytest.fixture
def config(tmp_app_dir: Path, vault_root: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        index_db_path=str(tmp_app_dir / "data" / "index.sqlite3"),
        vault_root=str(vault_root),
        token="test-token",  # noqa: S106 -- test fixture
    )


@pytest.fixture
def app(config: Config, vault_root: Path):  # noqa: ANN201 - fixture
    embedder = FakeEmbedder()
    db = IndexDb(
        config.index_db_path,
        embedding_model=FakeEmbedder.name,
        embedding_dim=FakeEmbedder.DIM,
    )
    index_vault(vault_root, db, embedder)
    db.close()

    def job_manager_factory(cfg: Config, ledger: Ledger) -> JobManager:
        return JobManager(
            cfg,
            ledger,
            embedder_factory=lambda _cfg: FakeEmbedder(),
            index_db_factory=lambda c: IndexDb(
                c.index_db_path,
                embedding_model=FakeEmbedder.name,
                embedding_dim=FakeEmbedder.DIM,
            ),
        )

    return create_app(config, job_manager_factory=job_manager_factory)


def test_a_speaker_filter_keeps_only_that_speakers_chunks(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/search",
            json={"query": "дедлайн", "speaker": "Иван Петров"},
            headers=AUTH,
        )

        assert response.status_code == 200
        hits = [(hit["kind"], hit["meeting_dir"]) for hit in response.json()["results"]]
        assert hits == [("transcript", ACME_RETRO)]


def test_a_query_without_a_speaker_still_finds_every_meeting(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post("/v1/search", json={"query": "дедлайн"}, headers=AUTH)

        assert response.status_code == 200
        found = {hit["meeting_dir"] for hit in response.json()["results"]}
        assert found == {ACME_RETRO, OTHER_KICKOFF}


def test_a_blank_speaker_means_no_filter(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/search", json={"query": "дедлайн", "speaker": "   "}, headers=AUTH
        )

        assert response.status_code == 200
        found = {hit["meeting_dir"] for hit in response.json()["results"]}
        assert found == {ACME_RETRO, OTHER_KICKOFF}


def test_a_speaker_nobody_in_the_vault_answers_empty_not_an_error(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/search", json={"query": "дедлайн", "speaker": "Nobody"}, headers=AUTH
        )

        assert response.status_code == 200
        assert response.json() == {"results": []}


def test_a_filtered_hits_snippet_comes_from_that_speakers_lines(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/search",
            json={"query": "дедлайн", "speaker": "Иван Петров"},
            headers=AUTH,
        )

        # Only Иван's lines mention the quarterly report; Анна's chunks of
        # the same meeting rank higher on the term "дедлайн".
        assert "квартальный отчёт" in response.json()["results"][0]["snippet"]


def test_a_speaker_filtered_hit_keeps_the_pinned_wire_shape(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/search",
            json={"query": "дедлайн", "speaker": "Иван Петров"},
            headers=AUTH,
        )

        assert set(response.json()["results"][0]) == {
            "kind",
            "project",
            "meeting_dir",
            "meeting_title",
            "meeting_date",
            "snippet",
            "score",
            "start_sec",
            "timestamp",
        }


def test_an_overlong_speaker_is_rejected_as_an_invalid_request(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/search",
            json={"query": "дедлайн", "speaker": "и" * 201},
            headers=AUTH,
        )

        assert response.status_code == 400
        assert response.json()["error_kind"] == "invalid_request"
