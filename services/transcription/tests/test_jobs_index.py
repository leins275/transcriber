"""Tests for the `index` job type inside the job manager.

Drives `JobManager` with `FakeEmbedder` and a tmp-file `IndexDb`: no HTTP,
no model, no network (FR-15).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import FakeEmbedder

from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError
from transcription.jobs import TERMINAL_STATUSES, JobManager
from transcription.ledger import Ledger
from transcription.search.index_db import IndexDb


@pytest.fixture
def vault_root(tmp_app_dir: Path) -> Path:
    root = tmp_app_dir / "vault"
    meeting = root / "ACME" / "260831 - Weekly sync"
    meeting.mkdir(parents=True)
    (meeting / "note.md").write_text(
        "Send the follow-up materials to the whole team after the demo.", encoding="utf-8"
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


@pytest.fixture
def ledger(config: Config) -> Iterator[Ledger]:
    led = Ledger(config.db_path)
    yield led
    led.close()


def _manager(config: Config, ledger: Ledger, embedder: FakeEmbedder) -> JobManager:
    return JobManager(
        config,
        ledger,
        embedder_factory=lambda _cfg: embedder,
        index_db_factory=lambda cfg: IndexDb(
            cfg.index_db_path,
            embedding_model=FakeEmbedder.name,
            embedding_dim=FakeEmbedder.DIM,
        ),
    )


async def _wait_until_terminal(manager: JobManager, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.status(job_id).status not in TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not finish in {timeout}s")
        await asyncio.sleep(0.01)


async def test_an_index_job_walks_the_vault_and_reports_stats(
    config: Config, ledger: Ledger
) -> None:
    embedder = FakeEmbedder()
    manager = _manager(config, ledger, embedder)
    try:
        await manager.start()
        job_id = await manager.submit(job_type="index")
        await _wait_until_terminal(manager, job_id)

        job = manager.status(job_id)
        assert job.status == "succeeded"
        assert job.progress == 1.0
        assert job.result_json is not None
        stats = json.loads(job.result_json)["stats"]
        assert stats["indexed"] == 1
        # The embedder is released after every index pass.
        assert embedder.unload_calls == 1
    finally:
        await manager.aclose()


async def test_a_second_queued_index_job_is_absorbed_into_the_first(
    config: Config, ledger: Ledger
) -> None:
    manager = _manager(config, ledger, FakeEmbedder())
    try:
        # Worker not started: both submissions stay queued.
        first = await manager.submit(job_type="index")
        second = await manager.submit(job_type="index")

        assert first == second
        assert len(ledger.list_jobs(limit=10)) == 1
    finally:
        await manager.aclose()


async def test_an_index_job_without_a_vault_root_is_refused_before_any_ledger_row(
    tmp_app_dir: Path, config: Config, ledger: Ledger
) -> None:
    bare = Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=config.db_path,
        token="test-token",  # noqa: S106 -- test fixture
    )
    manager = _manager(bare, ledger, FakeEmbedder())
    try:
        with pytest.raises(ServiceError) as exc_info:
            await manager.submit(job_type="index")

        assert exc_info.value.kind is ErrorKind.INVALID_REQUEST
        assert ledger.list_jobs(limit=10) == []
    finally:
        await manager.aclose()


async def test_the_ledger_row_records_the_index_job_type(config: Config, ledger: Ledger) -> None:
    manager = _manager(config, ledger, FakeEmbedder())
    try:
        await manager.start()
        job_id = await manager.submit(job_type="index")
        await _wait_until_terminal(manager, job_id)

        rows = ledger.list_jobs(limit=10)
        assert len(rows) == 1
        assert rows[0]["job_type"] == "index"
        assert rows[0]["status"] == "succeeded"
    finally:
        await manager.aclose()


async def test_a_failing_embedder_still_succeeds_with_a_warning(
    config: Config, ledger: Ledger
) -> None:
    manager = _manager(config, ledger, FakeEmbedder(raise_kind=ErrorKind.MODEL_LOAD))
    try:
        await manager.start()
        job_id = await manager.submit(job_type="index")
        await _wait_until_terminal(manager, job_id)

        job = manager.status(job_id)
        assert job.status == "succeeded"
        assert any("embedding" in warning for warning in job.warnings)
    finally:
        await manager.aclose()
