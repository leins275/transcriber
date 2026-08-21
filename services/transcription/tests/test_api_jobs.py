"""HTTP job-lifecycle tests: queue, poll, cancel, result, list, reconcile.

Uses `TestClient(app)` **with** the `with` block so lifespan actually runs --
the worker task starts, the ledger is reconciled on startup -- unlike
`test_api_contract.py`. `FakeProvider` (and small local subclasses of it,
mirroring `tests/test_jobs.py`) stand in for a real model so no GPU, model or
network is ever touched (FR-15).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider
from fastapi.testclient import TestClient

from transcription import providers
from transcription.app import create_app
from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError
from transcription.ledger import Ledger
from transcription.providers.base import CancelToken, TranscriptResult

AUTH = {"Authorization": "Bearer test-token"}
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class SlowProvider(FakeProvider):
    """A `FakeProvider` whose chunks take measurable time (proves async progress)."""

    def __init__(self, *args: object, chunk_sleep: float = 0.03, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.chunk_sleep = chunk_sleep

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        self.model_state = "loading"
        self.model_state = "loaded"

        if self.raise_kind is not None:
            raise ServiceError(self.raise_kind, f"fake provider raised {self.raise_kind.value}")

        for fraction in (0.25, 0.5, 0.75, 1.0):
            cancel.raise_if_cancelled()
            time.sleep(self.chunk_sleep)
            on_progress(fraction)

        text = "".join(seg.text for seg in self._segments)
        return TranscriptResult(
            segments=[seg.as_dict() for seg in self._segments],
            text=text,
            language=language or self.language,
            language_probability=0.99,
            duration_sec=1.0,
            model=self.model,
            device=self.device,
            compute_type=self.compute_type,
        )


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
    )


@pytest.fixture
def audio_file(tmp_app_dir: Path) -> Path:
    path = tmp_app_dir / "audio.wav"
    path.write_bytes(b"fake-audio-bytes")
    return path


def _submit(
    client: TestClient, audio_file: Path, output_dir: Path, *, provider: str | None = None
) -> str:
    body: dict[str, str] = {"audio_path": str(audio_file), "output_dir": str(output_dir)}
    if provider is not None:
        body["provider"] = provider
    response = client.post("/v1/jobs", json=body, headers=AUTH)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert isinstance(job_id, str) and job_id
    return job_id


def _poll_until_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while True:
        response = client.get(f"/v1/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        if last is not None:
            assert body["progress"] >= last["progress"]
        last = body
        if body["status"] in TERMINAL_STATUSES:
            return body
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not reach a terminal state in {timeout}s")
        time.sleep(0.01)


def test_post_job_returns_202_quickly_while_the_provider_still_runs(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    providers.register("fake", lambda cfg: SlowProvider(cfg, chunk_sleep=0.05))
    app = create_app(config)

    with TestClient(app) as client:
        body = {"audio_path": str(audio_file), "output_dir": str(tmp_app_dir / "out")}

        started = time.perf_counter()
        response = client.post("/v1/jobs", json=body, headers=AUTH)
        elapsed = time.perf_counter() - started

        assert response.status_code == 202
        assert elapsed < 0.2
        job_id = response.json()["job_id"]

        status = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        assert status["status"] in {"queued", "running"}

        _poll_until_terminal(client, job_id)


def test_poll_job_status_progress_is_monotonic_and_reports_metrics(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    providers.register("fake", lambda cfg: SlowProvider(cfg, chunk_sleep=0.02))
    app = create_app(config)

    with TestClient(app) as client:
        job_id = _submit(client, audio_file, tmp_app_dir / "out")
        final = _poll_until_terminal(client, job_id)

        assert final["status"] == "succeeded"
        assert final["progress"] == 1.0
        assert final["elapsed_sec"] > 0
        assert final["audio_duration_sec"] == 1.0
        assert final["provider"] == "fake"
        assert final["cost_usd"] is None


def test_job_result_matches_written_transcript_and_404_before_success(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    providers.register("fake", lambda cfg: SlowProvider(cfg, chunk_sleep=0.05))
    app = create_app(config)

    with TestClient(app) as client:
        output_dir = tmp_app_dir / "out"
        job_id = _submit(client, audio_file, output_dir)

        pending = client.get(f"/v1/jobs/{job_id}/result", headers=AUTH)
        assert pending.status_code == 404

        _poll_until_terminal(client, job_id)

        result = client.get(f"/v1/jobs/{job_id}/result", headers=AUTH)
        assert result.status_code == 200
        on_disk = json.loads((output_dir / "transcript.json").read_text(encoding="utf-8"))
        assert result.json() == on_disk


def test_list_jobs_newest_first_honours_limit_and_status_filters(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    app = create_app(config)

    # `JobManager` caches one provider instance per provider *name* for the
    # lifetime of the process (so a model only loads once) -- re-registering
    # a different fake under the same name would not change an already-built
    # instance, so the failing job uses a distinct provider name.
    providers.register("fake-fail", lambda cfg: FakeProvider(cfg, raise_kind=ErrorKind.TIMEOUT))

    with TestClient(app) as client:
        first = _submit(client, audio_file, tmp_app_dir / "a")
        _poll_until_terminal(client, first)

        failed = _submit(client, audio_file, tmp_app_dir / "b", provider="fake-fail")
        _poll_until_terminal(client, failed)

        third = _submit(client, audio_file, tmp_app_dir / "c")
        _poll_until_terminal(client, third)

        succeeded = client.get("/v1/jobs", params={"status": "succeeded"}, headers=AUTH)
        assert succeeded.status_code == 200
        assert [row["job_id"] for row in succeeded.json()] == [third, first]

        limited = client.get("/v1/jobs", params={"limit": 1, "status": "succeeded"}, headers=AUTH)
        assert [row["job_id"] for row in limited.json()] == [third]


def test_cancel_running_job_returns_200_and_job_becomes_cancelled(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    providers.register("fake", lambda cfg: SlowProvider(cfg, chunk_sleep=0.1))
    app = create_app(config)

    with TestClient(app) as client:
        job_id = _submit(client, audio_file, tmp_app_dir / "out")
        time.sleep(0.05)  # let the worker dequeue and enter the running state

        response = client.delete(f"/v1/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200

        final = _poll_until_terminal(client, job_id)
        assert final["status"] == "cancelled"
        assert not (tmp_app_dir / "out" / "transcript.json").exists()


def test_failed_job_returns_200_with_specific_error_kind(
    config: Config, audio_file: Path, tmp_app_dir: Path
) -> None:
    providers.register(
        "fake", lambda cfg: FakeProvider(cfg, raise_kind=ErrorKind.PROVIDER_RATE_LIMITED)
    )
    app = create_app(config)

    with TestClient(app) as client:
        job_id = _submit(client, audio_file, tmp_app_dir / "out")
        final = _poll_until_terminal(client, job_id)

        assert final["status"] == "failed"
        assert final["error_kind"] == "provider_rate_limited"
        assert final["error_message"]


def test_get_jobs_defaults_to_a_bounded_limit(config: Config, tmp_app_dir: Path) -> None:
    """E10: `GET /v1/jobs` with no `limit` must not return the whole table."""
    ledger = Ledger(config.db_path)
    try:
        for i in range(60):
            ledger.insert_job(
                f"job-{i}",
                provider="fake",
                model="fake-model",
                device="cpu",
                source_path=str(tmp_app_dir / "audio.wav"),
                output_path=str(tmp_app_dir / "out"),
            )
    finally:
        ledger.close()

    providers.register("fake", FakeProvider)
    app = create_app(config)
    with TestClient(app) as client:
        response = client.get("/v1/jobs", headers=AUTH)

    assert response.status_code == 200
    assert 0 < len(response.json()) <= 50


def test_get_job_status_hydrates_from_ledger_after_restart(tmp_app_dir: Path) -> None:
    """E11: a job seeded straight into the ledger (simulating a job this
    process never held in memory, e.g. after a restart) must still answer
    `GET /v1/jobs/{id}` instead of a spurious 404."""
    db_path = tmp_app_dir / "data" / "jobs.sqlite3"
    seed_ledger = Ledger(str(db_path))
    seed_ledger.insert_job(
        "old-job",
        provider="fake",
        model="fake-model",
        device="cpu",
        source_path=str(tmp_app_dir / "audio.wav"),
        output_path=str(tmp_app_dir / "out"),
    )
    seed_ledger.mark_running("old-job")
    seed_ledger.finish_succeeded(
        "old-job", elapsed_sec=1.5, audio_duration_sec=3.0, segment_count=2
    )
    seed_ledger.close()

    config = Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(db_path),
        token="test-token",  # noqa: S106 -- test fixture
    )
    providers.register("fake", FakeProvider)
    app = create_app(config)

    with TestClient(app) as client:
        response = client.get("/v1/jobs/old-job", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["provider"] == "fake"


def test_startup_reconciles_a_pre_seeded_running_row_to_failed(tmp_app_dir: Path) -> None:
    db_path = tmp_app_dir / "data" / "jobs.sqlite3"
    seed_ledger = Ledger(str(db_path))
    seed_ledger.insert_job(
        "stuck-job",
        provider="fake",
        model="fake-model",
        device="cpu",
        source_path=str(tmp_app_dir / "audio.wav"),
        output_path=str(tmp_app_dir / "out"),
    )
    seed_ledger.mark_running("stuck-job")
    seed_ledger.close()

    config = Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(db_path),
        token="test-token",  # noqa: S106 -- test fixture
    )
    providers.register("fake", FakeProvider)
    app = create_app(config)

    with TestClient(app):
        pass

    check_ledger = Ledger(str(db_path))
    try:
        row = check_ledger.get_job("stuck-job")
    finally:
        check_ledger.close()

    assert row is not None
    assert row["status"] == "failed"
    assert row["error_kind"] == "internal"
