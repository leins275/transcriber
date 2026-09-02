"""HTTP surface of hybrid search: `POST /v1/search` over a real (tmp-file)
index built by the indexer with `FakeEmbedder`. Offline throughout."""

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

LONG_RU = "Обсуждали дедлайн по проекту и планы на следующую неделю в подробностях. "


def _write_meeting(root: Path, project: str, name: str, *, note: str | None = None) -> None:
    meeting = root / project / name
    meeting.mkdir(parents=True)
    segments = [
        {"id": index, "start": float(index), "end": float(index) + 1.0, "text": LONG_RU}
        for index in range(3)
    ]
    (meeting / "transcript.json").write_text(
        json.dumps({"schema_version": 1, "text": LONG_RU * 3, "segments": segments}),
        encoding="utf-8",
    )
    if note is not None:
        (meeting / "note.md").write_text(note, encoding="utf-8")


@pytest.fixture
def vault_root(tmp_app_dir: Path) -> Path:
    root = tmp_app_dir / "vault"
    _write_meeting(
        root,
        "ACME",
        "260831 - Security retro",
        note="Remember to send the postmortem writeup to the whole team tomorrow.",
    )
    _write_meeting(root, "OTHER", "260830 - Kickoff")
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
    # Build the index up front with the fake embedder, then hand the app a
    # JobManager wired to the same DB/embedder fakes.
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


def test_search_answers_hits_with_the_pinned_wire_shape(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post("/v1/search", json={"query": "дедлайн"}, headers=AUTH)

        assert response.status_code == 200
        results = response.json()["results"]
        assert results, "the Russian body text must match"
        first = results[0]
        assert set(first) == {
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
        assert "/" in first["meeting_dir"] and "\\" not in first["meeting_dir"]
        assert "дедлайн" in first["snippet"]


def test_an_exact_title_hit_outranks_body_matches(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post("/v1/search", json={"query": "Security retro"}, headers=AUTH)

        results = response.json()["results"]
        assert results[0]["meeting_title"] == "Security retro"


def test_the_project_filter_narrows_results(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/search", json={"query": "дедлайн", "project": "OTHER"}, headers=AUTH
        )

        results = response.json()["results"]
        assert results
        assert all(result["project"] == "OTHER" for result in results)


def test_the_date_filter_narrows_results_in_both_spellings(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        vault_form = client.post(
            "/v1/search", json={"query": "дедлайн", "date": "260830"}, headers=AUTH
        )
        results = vault_form.json()["results"]
        assert results
        assert all(result["meeting_date"] == "2026-08-30" for result in results)

        iso_form = client.post(
            "/v1/search", json={"query": "дедлайн", "date": "2026-08-30"}, headers=AUTH
        )
        assert [r["meeting_dir"] for r in iso_form.json()["results"]] == [
            r["meeting_dir"] for r in results
        ]


def test_an_unparseable_date_degrades_to_no_filter(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post(
            "/v1/search", json={"query": "дедлайн", "date": "not-a-day"}, headers=AUTH
        )
        dates = {result["meeting_date"] for result in response.json()["results"]}
        assert dates == {"2026-08-30", "2026-08-31"}


def test_a_note_hit_carries_its_kind(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post("/v1/search", json={"query": "postmortem"}, headers=AUTH)

        results = response.json()["results"]
        assert results and results[0]["kind"] == "note"


def test_a_missing_index_answers_empty_not_500(config: Config) -> None:
    # No index was ever built: the default factory creates an empty DB.
    def job_manager_factory(cfg: Config, ledger: Ledger) -> JobManager:
        return JobManager(
            cfg,
            ledger,
            embedder_factory=lambda _cfg: FakeEmbedder(),
            index_db_factory=lambda c: IndexDb(
                str(Path(c.index_db_path).with_name("fresh.sqlite3")),
                embedding_model=FakeEmbedder.name,
                embedding_dim=FakeEmbedder.DIM,
            ),
        )

    app = create_app(config, job_manager_factory=job_manager_factory)
    with TestClient(app) as client:
        response = client.post("/v1/search", json={"query": "anything"}, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["results"] == []


def test_index_status_reports_indexed_pending_and_transcriptless(
    app,  # noqa: ANN001
    vault_root: Path,
) -> None:
    # Added after the fixture's index pass: a transcript nobody indexed yet,
    # and a meeting with no transcript at all.
    _write_meeting(vault_root, "ACME", "260830 - Later meeting")
    (vault_root / "ACME" / "260829 - Empty shell").mkdir()

    with TestClient(app) as client:
        response = client.get("/v1/index/status", params={"project": "ACME"}, headers=AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["project"] == "ACME"
        assert body["indexing"] is False
        assert body["total_count"] == 3
        assert body["indexed_count"] == 1
        by_name = {meeting["name"]: meeting for meeting in body["meetings"]}
        assert by_name["260831 - Security retro"]["state"] == "indexed"
        assert by_name["260831 - Security retro"]["chunks"] > 0
        assert by_name["260830 - Later meeting"]["state"] == "pending"
        assert by_name["260829 - Empty shell"]["state"] == "no_transcript"


def test_index_status_rejects_a_traversal_project(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.get("/v1/index/status", params={"project": "../evil"}, headers=AUTH)

        assert response.status_code == 400


def test_search_requires_the_bearer_token(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post("/v1/search", json={"query": "x"})

        assert response.status_code == 401


def test_an_empty_query_is_a_400(app) -> None:  # noqa: ANN001
    with TestClient(app) as client:
        response = client.post("/v1/search", json={"query": ""}, headers=AUTH)

        assert response.status_code == 400
