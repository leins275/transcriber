"""HTTP surface of the SSE chat (`POST /v1/chat`): event order, think-block
suppression, error and truncation surfacing. Offline throughout (FakeLlm +
FakeEmbedder + a tmp-file index)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeEmbedder, FakeLlm
from fastapi.testclient import TestClient

from transcription.app import create_app
from transcription.config import Config
from transcription.errors import ErrorKind
from transcription.jobs import JobManager
from transcription.ledger import Ledger
from transcription.search.index_db import IndexDb
from transcription.search.indexer import index_vault

AUTH = {"Authorization": "Bearer test-token"}

LONG_RU = "Обсуждали дедлайн по проекту и планы на следующую неделю в подробностях. "


@pytest.fixture
def vault_root(tmp_app_dir: Path) -> Path:
    root = tmp_app_dir / "vault"
    meeting = root / "ACME" / "260831 - Weekly sync"
    meeting.mkdir(parents=True)
    segments = [
        {"id": index, "start": float(index), "end": float(index) + 1.0, "text": LONG_RU}
        for index in range(3)
    ]
    (meeting / "transcript.json").write_text(
        json.dumps({"schema_version": 1, "text": LONG_RU * 3, "segments": segments}),
        encoding="utf-8",
    )
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


def _app(config: Config, vault_root: Path, llm: FakeLlm) -> Any:
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
            llm_factory=lambda _cfg: llm,
            embedder_factory=lambda _cfg: FakeEmbedder(),
            index_db_factory=lambda c: IndexDb(
                c.index_db_path,
                embedding_model=FakeEmbedder.name,
                embedding_dim=FakeEmbedder.DIM,
            ),
        )

    return create_app(config, job_manager_factory=job_manager_factory)


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into `(event, data)` pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name is not None and data is not None:
            events.append((name, data))
    return events


def _chat(client: TestClient, question: str) -> list[tuple[str, dict[str, Any]]]:
    response = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": question}]},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return _events(response.text)


def test_the_stream_is_sources_then_deltas_then_done(config: Config, vault_root: Path) -> None:
    llm = FakeLlm(responses=["Дедлайн перенесли на пятницу [S1]."])
    app = _app(config, vault_root, llm)
    with TestClient(app) as client:
        events = _chat(client, "когда дедлайн?")

    names = [name for name, _data in events]
    assert names[0] == "sources"
    assert names[-1] == "done"
    assert "delta" in names
    # The retrieval found the indexed meeting and reported it as a source.
    sources = events[0][1]["sources"]
    assert sources and sources[0]["meeting_dir"] == "ACME/260831 - Weekly sync"
    # The deltas concatenate to the visible answer.
    answer = "".join(data["text"] for name, data in events if name == "delta")
    assert answer == "Дедлайн перенесли на пятницу [S1]."
    assert events[-1][1]["finish_reason"] == "stop"


def test_think_blocks_never_reach_the_stream(config: Config, vault_root: Path) -> None:
    llm = FakeLlm(responses=["<think>secret chain of thought</think>The answer."])
    app = _app(config, vault_root, llm)
    with TestClient(app) as client:
        events = _chat(client, "вопрос?")

    answer = "".join(data["text"] for name, data in events if name == "delta")
    assert "secret" not in answer
    assert answer == "The answer."


def test_a_length_stop_is_reported_in_done(config: Config, vault_root: Path) -> None:
    llm = FakeLlm(responses=[("An answer cut mid-", "length")])
    app = _app(config, vault_root, llm)
    with TestClient(app) as client:
        events = _chat(client, "вопрос?")

    assert events[-1][0] == "done"
    assert events[-1][1]["finish_reason"] == "length"


def test_a_mid_stream_failure_becomes_an_error_event(config: Config, vault_root: Path) -> None:
    llm = FakeLlm(raise_kind=ErrorKind.INTERNAL)
    app = _app(config, vault_root, llm)
    with TestClient(app) as client:
        events = _chat(client, "вопрос?")

    assert events[-1][0] == "error"
    assert events[-1][1]["error_kind"] == "internal"


def test_the_llm_stays_loaded_after_a_chat_turn(config: Config, vault_root: Path) -> None:
    # llm_keep_loaded defaults to false, but chat is interactive: no unload.
    llm = FakeLlm(responses=["ok then"])
    app = _app(config, vault_root, llm)
    with TestClient(app) as client:
        _chat(client, "вопрос?")

    assert llm.unload_calls == 0


def test_chat_requires_the_bearer_token(config: Config, vault_root: Path) -> None:
    app = _app(config, vault_root, FakeLlm())
    with TestClient(app) as client:
        response = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "x"}]})
        assert response.status_code == 401


def test_a_history_ending_on_assistant_is_a_400(config: Config, vault_root: Path) -> None:
    app = _app(config, vault_root, FakeLlm())
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={"messages": [{"role": "assistant", "content": "hello"}]},
            headers=AUTH,
        )
        assert response.status_code == 400
