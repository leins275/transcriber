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
from transcription.diarization import SpeakerTurn
from transcription.errors import ErrorKind, ServiceError
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


async def test_speaker_embeddings_land_in_the_document_under_display_labels(
    config: Config, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    # Keyed by the diarizer's raw cluster labels; the document must hold
    # them under the normalized display labels so they join against the
    # operator's speakers.json renames.
    diarizer = FakeDiarizer(
        embeddings={"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]},
    )
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        await _run_one(manager, audio_file, output_dir, diarize=True)

        doc = _read_transcript(output_dir)
        assert doc["diarization"]["speaker_embeddings"] == {
            "Speaker 1": [1.0, 0.0],
            "Speaker 2": [0.0, 1.0],
        }
    finally:
        await manager.aclose()


async def test_a_diarized_document_without_embeddings_omits_the_key(
    config: Config, ledger: Ledger, audio_file: Path, output_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: FakeDiarizer())
    try:
        await _run_one(manager, audio_file, output_dir, diarize=True)

        doc = _read_transcript(output_dir)
        # Omitted, not nulled: a document produced before (or without) the
        # embeddings feature stays byte-identical.
        assert "speaker_embeddings" not in doc["diarization"]
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


# -- the `diarize` job: speakers for an already-transcribed meeting ------------


def _write_filed_meeting(root: Path, *, with_source: bool = True) -> Path:
    """A meeting folder the way the vault leaves it after an undiarized
    transcription: `source.<ext>`, a two-segment `transcript.json` and the
    operator's hand-made `speakers.json`."""
    meeting = root / "ACME" / "260901 - Planning"
    meeting.mkdir(parents=True)
    if with_source:
        (meeting / "source.wav").write_bytes(b"fake-audio-bytes")
    doc = {
        "schema_version": 1,
        "created_at": "2026-09-01T10:00:00+00:00",
        "source": {
            "path": str(meeting / "source.wav"),
            "filename": "source.wav",
            "duration_sec": 1.0,
        },
        "provider": {"name": "fake", "model": "fake-model", "device": "cpu", "compute_type": ""},
        "language": "en",
        "language_probability": 0.9,
        "text": "hello world",
        "segments": [
            {"id": 0, "start": 0.0, "end": 0.5, "text": "hello "},
            {"id": 1, "start": 0.5, "end": 1.0, "text": "world"},
        ],
        "stats": {"elapsed_sec": 0.1, "realtime_factor": 0.1, "cost_usd": None, "currency": None},
    }
    (meeting / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    (meeting / "speakers.json").write_text(
        json.dumps({"schema_version": 1, "assignments": {"0": "Anna"}}, indent=2),
        encoding="utf-8",
    )
    return meeting


async def _run_diarize(manager: JobManager, meeting: Path) -> str:
    await manager.start()
    job_id = await manager.submit(
        job_type="diarize", input_path=str(meeting), output_dir=str(meeting)
    )
    await _wait_until_terminal(manager, job_id)
    return job_id


async def test_a_diarize_job_labels_an_existing_transcript_and_keeps_its_ids(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    meeting = _write_filed_meeting(tmp_app_dir)
    speakers_before = (meeting / "speakers.json").read_bytes()
    diarizer = FakeDiarizer(
        embeddings={"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]},
        turns=[
            # A word-less transcript: the segment envelope votes. The first
            # turn straddles both segments, so a splitting pass would have
            # cut segment 1 -- this job must not.
            SpeakerTurn(start=0.0, end=0.6, speaker="SPEAKER_00"),
            SpeakerTurn(start=0.6, end=1.0, speaker="SPEAKER_01"),
        ],
    )
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        job_id = await _run_diarize(manager, meeting)

        job = manager.status(job_id)
        assert job.status == "succeeded", job.error_message
        assert job.job_type == "diarize"
        assert [called.name for called in diarizer.calls] == ["source.wav"]

        doc = _read_transcript(meeting)
        assert [seg["id"] for seg in doc["segments"]] == [0, 1]
        assert [seg["speaker"] for seg in doc["segments"]] == ["Speaker 1", "Speaker 2"]
        assert doc["diarization"]["status"] == "succeeded"
        assert doc["diarization"]["speaker_embeddings"] == {
            "Speaker 1": [1.0, 0.0],
            "Speaker 2": [0.0, 1.0],
        }
        # Everything else in the document survives the rewrite.
        assert doc["text"] == "hello world"
        assert doc["created_at"] == "2026-09-01T10:00:00+00:00"
        # The operator's file is not the job's to touch.
        assert (meeting / "speakers.json").read_bytes() == speakers_before
        result = json.loads(job.result_json or "{}")
        assert result["speaker_count"] == 2
        assert result["embeddings"] == 2
    finally:
        await manager.aclose()


async def test_a_diarize_job_needs_a_transcript_and_a_recording(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: FakeDiarizer())
    try:
        empty = tmp_app_dir / "ACME" / "260902 - Empty"
        empty.mkdir(parents=True)
        with pytest.raises(ServiceError) as refused:
            await manager.submit(job_type="diarize", input_path=str(empty), output_dir=str(empty))
        assert refused.value.kind is ErrorKind.INVALID_REQUEST
        assert "transcribe first" in refused.value.message

        meeting = _write_filed_meeting(tmp_app_dir, with_source=False)
        job_id = await _run_diarize(manager, meeting)
        job = manager.status(job_id)
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.INVALID_REQUEST
        assert "no recording" in (job.error_message or "")
    finally:
        await manager.aclose()


async def test_a_diarize_job_fails_when_the_engine_cannot_load(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    providers.register("fake", FakeProvider)
    meeting = _write_filed_meeting(tmp_app_dir)
    transcript_before = (meeting / "transcript.json").read_bytes()
    diarizer = FakeDiarizer(raise_kind=ErrorKind.MODEL_LOAD)
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        job_id = await _run_diarize(manager, meeting)

        job = manager.status(job_id)
        # Unlike a transcription, whose transcript is still the deliverable,
        # this job has nothing to deliver without the pass.
        assert job.status == "failed"
        assert job.error_kind is ErrorKind.MODEL_LOAD
        assert (meeting / "transcript.json").read_bytes() == transcript_before
    finally:
        await manager.aclose()


async def test_a_diarized_transcription_prefills_speakers_from_a_named_sibling(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    """The pipeline wiring end to end: a sibling meeting whose voice the
    operator named, then a new diarized transcription in the same project
    opens with that name already assigned."""
    providers.register("fake", FakeProvider)
    project = tmp_app_dir / "ACME"
    sibling = project / "260901 - Planning"
    sibling.mkdir(parents=True)
    (sibling / "transcript.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"id": 0, "start": 0.0, "end": 1.0, "text": "hi", "speaker": "Speaker 1"}
                ],
                "diarization": {
                    "status": "succeeded",
                    "model": "m",
                    "speaker_embeddings": {"Speaker 1": [1.0, 0.0]},
                },
            }
        ),
        encoding="utf-8",
    )
    (sibling / "speakers.json").write_text(
        json.dumps({"schema_version": 1, "assignments": {"0": "Anna"}}), encoding="utf-8"
    )
    audio = project / "260902 - Standup" / "source.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake-audio-bytes")
    diarizer = FakeDiarizer(embeddings={"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        job_id = await _run_one(manager, audio, audio.parent, diarize=True)

        assert manager.status(job_id).status == "succeeded"
        assignments = json.loads((audio.parent / "speakers.json").read_text(encoding="utf-8"))
        # Segment 0 is Speaker 1 (the voice named Anna next door); segment 1
        # is a new voice and stays unnamed.
        assert assignments["assignments"] == {"0": "Anna"}
    finally:
        await manager.aclose()
