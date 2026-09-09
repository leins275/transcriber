"""The chat's automatic speaker scope (`POST /v1/chat`, FR-5): a question
naming a speaker the index knows retrieves that speaker's chunks and
nothing else; a question naming nobody -- or somebody the index has never
heard -- retrieves exactly as it did before the feature.

Offline throughout (FakeLlm + FakeEmbedder + a tmp-file index), following
`test_api_chat.py`'s fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeEmbedder, FakeLlm
from fastapi.testclient import TestClient

from transcription.app import create_app
from transcription.config import Config
from transcription.jobs import JobManager
from transcription.ledger import Ledger
from transcription.search.index_db import IndexDb
from transcription.search.indexer import index_vault

AUTH = {"Authorization": "Bearer test-token"}

LONG_RU = "Обсуждали дедлайн по проекту и планы на следующую неделю в подробностях. "

# The vault the fixtures below build: three meetings, three speaker casts.
WEEKLY = "ACME/260831 - Weekly sync"  # Иван Петров and Anna, alternating
DESIGN = "ACME/260829 - Design review"  # Марк Орлов alone
KICKOFF = "OTHER/260830 - Kickoff"  # Anna alone


def _write_meeting(root: Path, project: str, name: str, speakers: list[str]) -> None:
    """One meeting whose four segments cycle through ``speakers``; every
    segment carries the same body text, so retrieval separates the
    meetings by speaker and by date, never by wording."""
    meeting = root / project / name
    meeting.mkdir(parents=True)
    segments = [
        {
            "id": index,
            "start": float(index),
            "end": float(index) + 1.0,
            "text": LONG_RU,
            "speaker": speakers[index % len(speakers)],
        }
        for index in range(4)
    ]
    (meeting / "transcript.json").write_text(
        json.dumps(
            {"schema_version": 1, "text": LONG_RU * 4, "segments": segments},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def vault_root(tmp_app_dir: Path) -> Path:
    root = tmp_app_dir / "vault"
    _write_meeting(root, "ACME", "260831 - Weekly sync", ["Иван Петров", "Anna"])
    _write_meeting(root, "ACME", "260829 - Design review", ["Марк Орлов"])
    _write_meeting(root, "OTHER", "260830 - Kickoff", ["Anna"])
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
def app(config: Config, vault_root: Path) -> Any:
    embedder = FakeEmbedder()
    db = IndexDb(
        config.index_db_path,
        embedding_model=FakeEmbedder.name,
        embedding_dim=FakeEmbedder.DIM,
    )
    index_vault(vault_root, db, embedder)
    db.close()

    llm = FakeLlm(responses=["Вот что нашлось [S1]."])

    def job_manager_factory(cfg: Config, ledger: Ledger) -> JobManager:
        return JobManager(
            cfg,
            ledger,
            llm_factory=lambda _cfg: llm,
            embedder_factory=lambda _cfg: FakeEmbedder(),
            index_db_factory=lambda c: IndexDb(
                c.index_db_path,
                embedding_model=FakeEmbedder.name,
                embedding_dim=FakeEmbedder.DIM,
            ),
        )

    return create_app(config, job_manager_factory=job_manager_factory)


def _source_dirs(app: Any, question: str, *, project: str | None = None) -> set[str]:
    """The meeting dirs the chat cited for ``question`` (the `sources`
    event opens every stream)."""
    body: dict[str, Any] = {"messages": [{"role": "user", "content": question}]}
    if project is not None:
        body["project"] = project
    with TestClient(app) as client:
        response = client.post("/v1/chat", json=body, headers=AUTH)
    assert response.status_code == 200
    for block in response.text.split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0] == "event: sources":
            payload = json.loads(lines[1][len("data: ") :])
            return {source["meeting_dir"] for source in payload["sources"]}
    raise AssertionError(f"the stream carried no sources event: {response.text!r}")


def test_a_question_naming_a_speaker_cites_only_meetings_that_speaker_spoke_in(app: Any) -> None:
    cited = _source_dirs(app, "что говорил Иван про дедлайн")

    assert cited == {WEEKLY}


def test_a_speaker_who_spoke_in_two_meetings_is_cited_from_both(app: Any) -> None:
    cited = _source_dirs(app, "что говорила Anna про дедлайн")

    assert cited == {WEEKLY, KICKOFF}


def test_a_question_naming_nobody_retrieves_across_every_speaker(app: Any) -> None:
    cited = _source_dirs(app, "что обсуждали про дедлайн")

    assert cited == {WEEKLY, DESIGN, KICKOFF}


def test_a_name_the_index_never_heard_leaves_retrieval_unscoped(app: Any) -> None:
    cited = _source_dirs(app, "что сказал Пётр про дедлайн")

    assert cited == {WEEKLY, DESIGN, KICKOFF}


def test_a_name_absent_from_the_requests_project_is_not_a_filter(app: Any) -> None:
    # Иван speaks only in ACME, so within OTHER his name is just a word.
    cited = _source_dirs(app, "что говорил Иван про дедлайн", project="OTHER")

    assert cited == {KICKOFF}


def test_the_speaker_and_date_filters_compose(app: Any) -> None:
    # 260830 is the Kickoff, where Иван never speaks: both filters hold, so
    # the chat cites nothing rather than the day's other voices.
    cited = _source_dirs(app, "что говорил Иван про дедлайн за 260830")

    assert cited == set()
