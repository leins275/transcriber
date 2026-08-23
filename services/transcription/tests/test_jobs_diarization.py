"""Tests for the diarization pass inside the job manager.

Drives `JobManager` with `FakeProvider` + `FakeDiarizer` (tests/fakes.py):
no HTTP, no model, no torch, no network (FR-15).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDiarizer, FakeProvider

from transcription import providers
from transcription.config import Config
from transcription.errors import ErrorKind
from transcription.jobs import TERMINAL_STATUSES, JobManager
from transcription.ledger import Ledger


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
def ledger(config: Config) -> Iterator[Ledger]:
    led = Ledger(config.db_path)
    yield led
    led.close()


@pytest.fixture
def audio_file(tmp_app_dir: Path) -> Path:
    path = tmp_app_dir / "audio.wav"
    path.write_bytes(b"fake-audio-bytes")
    return path


@pytest.fixture
def output_dir(tmp_app_dir: Path) -> Path:
    return tmp_app_dir / "output"


async def _wait_until_terminal(manager: JobManager, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.status(job_id).status not in TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not finish in {timeout}s")
        await asyncio.sleep(0.01)


def _read_transcript(output_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((output_dir / "transcript.json").read_text(encoding="utf-8"))
    return data


async def _run_one(
    manager: JobManager, audio_file: Path, output_dir: Path, *, diarize: bool | None = None
) -> str:
    await manager.start()
    job_id = await manager.submit(
        audio_path=str(audio_file), output_dir=str(output_dir), diarize=diarize
    )
    await _wait_until_terminal(manager, job_id)
    return job_id


async def test_a_diarized_job_labels_segments_and_records_the_pass(
    config: Config, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    diarizer = FakeDiarizer()
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        job_id = await _run_one(manager, audio_file, output_dir, diarize=True)

        job = manager.status(job_id)
        assert job.status == "succeeded"
        assert job.progress == 1.0

        doc = _read_transcript(output_dir)
        # FakeProvider's two segments sit exactly on FakeDiarizer's two turns.
        assert [seg["speaker"] for seg in doc["segments"]] == ["Speaker 1", "Speaker 2"]
        assert doc["diarization"]["status"] == "succeeded"
        assert doc["diarization"]["model"] == "fake-diarization-model"
        assert doc["diarization"]["speaker_count"] == 2
        assert [called.name for called in diarizer.calls] == [audio_file.name]
    finally:
        await manager.aclose()


async def test_an_undiarized_job_writes_a_pre_feature_document(
    config: Config, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    diarizer = FakeDiarizer()
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        await _run_one(manager, audio_file, output_dir)  # diarize defaults off

        doc = _read_transcript(output_dir)
        assert "diarization" not in doc
        assert all("speaker" not in seg for seg in doc["segments"])
        assert diarizer.calls == []
    finally:
        await manager.aclose()


async def test_the_config_default_is_used_when_the_job_does_not_say(
    tmp_app_dir: Path, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    config = Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
        diarize=True,
    )
    ledger = Ledger(config.db_path)
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: FakeDiarizer())
    try:
        await _run_one(manager, audio_file, output_dir)

        doc = _read_transcript(output_dir)
        assert doc["diarization"]["status"] == "succeeded"
    finally:
        await manager.aclose()
        ledger.close()


async def test_an_explicit_false_overrides_a_config_true(
    tmp_app_dir: Path, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    config = Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
        diarize=True,
    )
    ledger = Ledger(config.db_path)
    diarizer = FakeDiarizer()
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        await _run_one(manager, audio_file, output_dir, diarize=False)

        doc = _read_transcript(output_dir)
        assert "diarization" not in doc
        assert diarizer.calls == []
    finally:
        await manager.aclose()
        ledger.close()


async def test_a_diarization_failure_degrades_to_an_unlabelled_transcript(
    config: Config, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    manager = JobManager(
        config,
        ledger,
        diarizer_factory=lambda _cfg: FakeDiarizer(raise_kind=ErrorKind.MODEL_LOAD),
    )
    try:
        job_id = await _run_one(manager, audio_file, output_dir, diarize=True)

        job = manager.status(job_id)
        assert job.status == "succeeded", (job.error_kind, job.error_message)

        doc = _read_transcript(output_dir)
        assert all("speaker" not in seg for seg in doc["segments"])
        assert doc["diarization"]["status"] == "failed"
        assert doc["diarization"]["error_kind"] == "model_load"
        assert doc["diarization"]["error_message"]
    finally:
        await manager.aclose()


async def test_a_cancellation_raised_by_the_diarizer_cancels_the_job(
    config: Config, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    manager = JobManager(
        config,
        ledger,
        diarizer_factory=lambda _cfg: FakeDiarizer(raise_kind=ErrorKind.CANCELLED),
    )
    try:
        job_id = await _run_one(manager, audio_file, output_dir, diarize=True)

        job = manager.status(job_id)
        assert job.status == "cancelled"
        assert not (output_dir / "transcript.json").exists()
    finally:
        await manager.aclose()


async def test_the_diarizer_is_constructed_once_across_jobs(
    config: Config, ledger: Ledger, audio_file: Path, output_dir: Path, tmp_app_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    constructed: list[FakeDiarizer] = []

    def factory(_cfg: Config) -> FakeDiarizer:
        diarizer = FakeDiarizer()
        constructed.append(diarizer)
        return diarizer

    manager = JobManager(config, ledger, diarizer_factory=factory)
    try:
        await manager.start()
        for out_name in ("out-a", "out-b"):
            out = tmp_app_dir / out_name
            job_id = await manager.submit(
                audio_path=str(audio_file), output_dir=str(out), diarize=True
            )
            await _wait_until_terminal(manager, job_id)

        assert len(constructed) == 1
        assert len(constructed[0].calls) == 2
    finally:
        await manager.aclose()


async def test_transcription_progress_is_scaled_below_one_while_diarization_remains(
    config: Config, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    seen: list[float] = []

    class RecordingDiarizer(FakeDiarizer):
        def diarize(self, audio_path: Path, *, cancel: Any) -> Any:
            # Whatever the provider reported has been scaled: the bar must
            # not read 100% while this pass is still ahead.
            return super().diarize(audio_path, cancel=cancel)

    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: RecordingDiarizer())
    try:
        await manager.start()
        job_id = await manager.submit(
            audio_path=str(audio_file), output_dir=str(output_dir), diarize=True
        )
        while manager.status(job_id).status not in TERMINAL_STATUSES:
            seen.append(manager.status(job_id).progress)
            await asyncio.sleep(0.001)

        assert manager.status(job_id).progress == 1.0
        # Every observation before terminal is either scaled transcription
        # progress (<= 0.9) or the final 1.0 written at success.
        assert all(fraction <= 0.9 or fraction == 1.0 for fraction in seen)
    finally:
        await manager.aclose()
