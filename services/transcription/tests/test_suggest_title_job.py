"""The `suggest_title` job: a short meeting name out of `summary.md`.

Drives `JobManager` (and, once, the HTTP surface) with `FakeLlm`: no model,
no llama.cpp, no network. The job is read-only -- its whole output is the
`{"title": ...}` result manifest -- so every test also has the meeting
folder to check for things that must not have appeared.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeLlm
from fastapi.testclient import TestClient

from transcription.app import create_app
from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError
from transcription.jobs import TERMINAL_STATUSES, JobManager
from transcription.ledger import Ledger

AUTH = {"Authorization": "Bearer test-token"}
MEETING_NAME = "260731 - Запись встречи 31.07.2026 11 04 56"


def _config(tmp_app_dir: Path, **overrides: Any) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
        **overrides,
    )


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    return _config(tmp_app_dir)


@pytest.fixture
def ledger(config: Config) -> Iterator[Ledger]:
    led = Ledger(config.db_path)
    yield led
    led.close()


@pytest.fixture
def meeting_dir(tmp_app_dir: Path) -> Path:
    """A vault-shaped meeting folder holding a summary and nothing else --
    the job reads `summary.md` alone, so no transcript is needed."""
    meeting = tmp_app_dir / "vault" / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True)
    (meeting / "summary.md").write_text(
        "## Overview\n\nWe reviewed the Q3 budget.\n", encoding="utf-8"
    )
    return meeting


def _manager(config: Config, ledger: Ledger, llm: FakeLlm) -> JobManager:
    return JobManager(config, ledger, llm_factory=lambda _cfg: llm)


async def _wait_until_terminal(manager: JobManager, job_id: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.status(job_id).status not in TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not finish in {timeout}s")
        await asyncio.sleep(0.01)


async def _submit(manager: JobManager, meeting: Path) -> str:
    return await manager.submit(
        job_type="suggest_title", input_path=str(meeting), output_dir=str(meeting)
    )


async def _run(manager: JobManager, meeting: Path) -> str:
    await manager.start()
    job_id = await _submit(manager, meeting)
    await _wait_until_terminal(manager, job_id)
    return job_id


def _manifest(ledger: Ledger, job_id: str) -> Any:
    row = ledger.get_job(job_id)
    assert row is not None
    return json.loads(row["result_json"]) if row["result_json"] else None


# ------------------------------------------------------------------ success


async def test_the_title_is_the_jobs_result_and_nothing_is_written_or_renamed(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    before = sorted(path.name for path in meeting_dir.iterdir())
    llm = FakeLlm(responses=["Q3 budget review"])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run(manager, meeting_dir)

        job = manager.status(job_id)
        assert job.status == "succeeded", job.error_message
        assert job.progress == 1.0
        assert job.phase is None
        assert _manifest(ledger, job_id) == {"title": "Q3 budget review"}
        row = ledger.get_job(job_id)
        assert row is not None
        assert row["job_type"] == "suggest_title"

        # The model was handed the summary, free-form (no JSON schema).
        assert "We reviewed the Q3 budget." in llm.calls[0][1]["content"]
        assert llm.schemas == [None]
        # Read-only: the folder holds what it held, under the name it had.
        assert meeting_dir.is_dir()
        assert sorted(path.name for path in meeting_dir.iterdir()) == before
        # The default `llm_keep_loaded=False` releases the model afterwards.
        assert llm.unload_calls == 1
    finally:
        await manager.aclose()


async def test_the_model_stays_loaded_when_the_operator_asked_for_that(
    tmp_app_dir: Path, meeting_dir: Path
) -> None:
    config = _config(tmp_app_dir, llm_keep_loaded=True)
    ledger = Ledger(config.db_path)
    llm = FakeLlm(responses=["Q3 budget review"])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run(manager, meeting_dir)

        assert manager.status(job_id).status == "succeeded"
        assert llm.unload_calls == 0
    finally:
        await manager.aclose()
        ledger.close()


async def test_the_answer_is_sanitized_and_its_reasoning_dropped(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm(
        responses=[
            "Let me think - which title fits?\n</think>\n\n"
            '**"Budget review - Q3/Q4: what\'s next?"**.\n\nThis title names the topic.'
        ]
    )
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run(manager, meeting_dir)

        assert manager.status(job_id).status == "succeeded"
        assert _manifest(ledger, job_id) == {"title": "Budget review Q3 Q4 what's next"}
        # No reasoning sidecar: this job writes nothing at all.
        assert [path.name for path in meeting_dir.iterdir()] == ["summary.md"]
    finally:
        await manager.aclose()


async def test_only_what_fits_the_context_window_is_read(
    tmp_app_dir: Path, meeting_dir: Path
) -> None:
    lines = [f"line {i} of a very long hand-written summary" for i in range(2000)]
    (meeting_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    config = _config(tmp_app_dir, llm_ctx=2048)
    ledger = Ledger(config.db_path)
    llm = FakeLlm(responses=["Long summary"])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run(manager, meeting_dir)

        assert manager.status(job_id).status == "succeeded"
        assert len(llm.calls) == 1
        prompt = llm.calls[0][1]["content"]
        assert lines[0] in prompt, "the top of the summary is what is read"
        assert lines[-1] not in prompt
        assert llm.count_tokens(prompt) < 2048
    finally:
        await manager.aclose()
        ledger.close()


# ----------------------------------------------------------------- failures


async def test_a_meeting_without_a_summary_is_rejected_at_submission(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    (meeting_dir / "summary.md").unlink()
    manager = _manager(config, ledger, FakeLlm())
    try:
        with pytest.raises(ServiceError) as excinfo:
            await _submit(manager, meeting_dir)

        assert excinfo.value.kind is ErrorKind.INVALID_REQUEST
        assert "summary.md" in excinfo.value.message
        assert ledger.list_jobs() == [], "a rejected submit creates no ledger row"
    finally:
        await manager.aclose()


async def test_a_summary_deleted_after_submission_fails_as_invalid_request(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = FakeLlm()
    manager = _manager(config, ledger, llm)
    try:
        # Submitted while the summary exists; the worker only starts below.
        job_id = await _submit(manager, meeting_dir)
        (meeting_dir / "summary.md").unlink()
        await manager.start()
        await _wait_until_terminal(manager, job_id)

        job = manager.status(job_id)
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.INVALID_REQUEST
        assert "summary.md" in (job.error_message or "")
        assert llm.calls == []
    finally:
        await manager.aclose()


async def test_an_empty_summary_fails_as_invalid_request_without_asking_the_model(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    (meeting_dir / "summary.md").write_text("  \n\n", encoding="utf-8")
    llm = FakeLlm(responses=["Never asked"])
    manager = _manager(config, ledger, llm)
    try:
        job_id = await _run(manager, meeting_dir)

        job = manager.status(job_id)
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.INVALID_REQUEST
        assert "empty" in (job.error_message or "")
        assert llm.calls == []
        assert _manifest(ledger, job_id) is None
    finally:
        await manager.aclose()


@pytest.mark.parametrize("answer", ["", '""', "---", "<think>only thoughts</think>"])
async def test_an_answer_with_nothing_usable_fails_the_job_clearly(
    config: Config, ledger: Ledger, meeting_dir: Path, answer: str
) -> None:
    manager = _manager(config, ledger, FakeLlm(responses=[answer]))
    try:
        job_id = await _run(manager, meeting_dir)

        job = manager.status(job_id)
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.LLM_OUTPUT
        assert "no usable title" in (job.error_message or "")
        assert _manifest(ledger, job_id) is None
    finally:
        await manager.aclose()


async def test_an_answer_cut_off_at_the_token_limit_fails_honestly(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    manager = _manager(config, ledger, FakeLlm(responses=[("Half a tit", "length")]))
    try:
        job_id = await _run(manager, meeting_dir)

        job = manager.status(job_id)
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.LLM_OUTPUT
        assert "token limit" in (job.error_message or "")
    finally:
        await manager.aclose()


async def test_an_llm_load_failure_fails_the_job_and_the_worker_survives(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    manager = _manager(config, ledger, FakeLlm(raise_kind=ErrorKind.MODEL_LOAD))
    try:
        first = await _run(manager, meeting_dir)
        assert manager.status(first).status == "failed"
        assert manager.status(first).error_kind is ErrorKind.MODEL_LOAD

        # The serial worker is still alive for the next job.
        second = await _submit(manager, meeting_dir)
        await _wait_until_terminal(manager, second)
        assert manager.status(second).status == "failed"
    finally:
        await manager.aclose()


# ------------------------------------------------- phases, guards, the wire


class _PausingLlm(FakeLlm):
    """A `FakeLlm` whose `complete()` waits at its door until released."""

    def __init__(self, *, responses: list[str | tuple[str, str]]) -> None:
        super().__init__(responses=responses)
        self.reached = threading.Event()
        self.release = threading.Event()

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        self.reached.set()
        if not self.release.wait(timeout=10.0):
            raise AssertionError("the paused job was never released")
        return super().complete(messages, **kwargs)


async def test_a_running_job_reports_reading_the_summary_and_counts_as_an_llm_job(
    config: Config, ledger: Ledger, meeting_dir: Path
) -> None:
    llm = _PausingLlm(responses=["Budget review"])
    manager = _manager(config, ledger, llm)
    try:
        await manager.start()
        job_id = await _submit(manager, meeting_dir)
        if not await asyncio.to_thread(llm.reached.wait, 10.0):
            raise TimeoutError("the job never reached its completion call")

        job = manager.status(job_id)
        assert job.status == "running"
        assert job.phase == "reading summary"
        assert job.progress is None
        # The model-delete guard must see it: the GGUF is in use.
        assert manager.has_active_llm_job()

        llm.release.set()
        await _wait_until_terminal(manager, job_id)
        assert manager.status(job_id).status == "succeeded"
        assert not manager.has_active_llm_job()
    finally:
        llm.release.set()
        await manager.aclose()


def test_over_http_the_result_route_answers_the_title(config: Config, meeting_dir: Path) -> None:
    llm = FakeLlm(responses=["«Обзор бюджета - третий квартал»."])

    def job_manager_factory(cfg: Config, ledger: Ledger) -> JobManager:
        return JobManager(cfg, ledger, llm_factory=lambda _cfg: llm)

    app = create_app(config, job_manager_factory=job_manager_factory)
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={
                "job_type": "suggest_title",
                "input_path": str(meeting_dir),
                "output_dir": str(meeting_dir),
            },
            headers=AUTH,
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        deadline = time.monotonic() + 30.0
        while True:
            status = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
            if status["status"] in TERMINAL_STATUSES:
                break
            assert time.monotonic() < deadline, "the job never finished"
            time.sleep(0.01)
        assert status["status"] == "succeeded"
        assert status["job_type"] == "suggest_title"

        result = client.get(f"/v1/jobs/{job_id}/result", headers=AUTH)
        assert result.status_code == 200
        assert result.json() == {"title": "Обзор бюджета третий квартал"}


def test_over_http_a_meeting_without_a_summary_is_a_400(config: Config, meeting_dir: Path) -> None:
    (meeting_dir / "summary.md").unlink()
    app = create_app(
        config,
        job_manager_factory=lambda cfg, ledger: JobManager(
            cfg, ledger, llm_factory=lambda _cfg: FakeLlm()
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={
                "job_type": "suggest_title",
                "input_path": str(meeting_dir),
                "output_dir": str(meeting_dir),
            },
            headers=AUTH,
        )
        assert response.status_code == 400, response.text
        assert response.json()["error_kind"] == "invalid_request"
