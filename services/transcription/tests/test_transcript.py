"""Tests for the transcript schema (FR-6) and the atomic writer (NFR-5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from transcription.errors import ErrorKind
from transcription.schema import Health, JobCreate, JobStatus, Segment
from transcription.transcript import build_document, write_atomic


def _make_doc(*, words: list[dict] | None = None, cost_usd: float | None = None):
    segment = Segment(
        id=0,
        start=0.0,
        end=1.5,
        text="hello world",
        avg_logprob=-0.2,
        no_speech_prob=0.01,
        compression_ratio=1.1,
        words=words,
    )
    return build_document(
        source_path="C:/vault/ELS/meeting.wav",
        duration_sec=90.0,
        provider_name="local",
        model="large-v3",
        device="cuda",
        compute_type="float16",
        language="en",
        language_probability=0.98,
        text="hello world",
        segments=[segment],
        elapsed_sec=12.0,
        realtime_factor=12.0 / 90.0,
        cost_usd=cost_usd,
        currency=None if cost_usd is None else "USD",
    )


def test_transcript_doc_serializes_v1_shape() -> None:
    doc = _make_doc()
    data = doc.model_dump(mode="json")

    assert data["schema_version"] == 1
    assert set(data.keys()) == {
        "schema_version",
        "created_at",
        "source",
        "provider",
        "language",
        "language_probability",
        "text",
        "segments",
        "stats",
    }


def test_segment_fields_present_and_words_absent_when_none() -> None:
    doc = _make_doc(words=None)
    data = doc.model_dump(mode="json")
    segment = data["segments"][0]

    for key in (
        "id",
        "start",
        "end",
        "text",
        "avg_logprob",
        "no_speech_prob",
        "compression_ratio",
    ):
        assert key in segment

    assert "words" not in segment


def test_segment_words_present_when_requested() -> None:
    doc = _make_doc(words=[{"word": "hi", "start": 0.0, "end": 0.3}])
    data = doc.model_dump(mode="json")
    segment = data["segments"][0]

    assert segment["words"] == [{"word": "hi", "start": 0.0, "end": 0.3}]


def test_stats_shape_and_cost_usd_null_for_local() -> None:
    doc = _make_doc(cost_usd=None)
    raw = doc.model_dump_json()
    data = json.loads(raw)

    stats = data["stats"]
    assert set(stats.keys()) == {"elapsed_sec", "realtime_factor", "cost_usd", "currency"}
    assert "cost_usd" in stats
    assert stats["cost_usd"] is None
    assert stats["currency"] is None


def test_stats_cost_usd_present_when_a_cost_is_reported() -> None:
    doc = _make_doc(cost_usd=0.006)
    data = json.loads(doc.model_dump_json())

    assert data["stats"]["cost_usd"] == pytest.approx(0.006)
    assert data["stats"]["currency"] == "USD"


def test_segment_with_none_confidence_fields_builds_and_writes(
    tmp_path: Path,
) -> None:
    """E2: a `_normalize_segments` shape whose confidence fields are
    missing/`None` must cross the `Segment` -> `build_document` ->
    `write_atomic` seam without a `ValidationError` -- the fields are
    optional and must never be fabricated."""
    segment = Segment(
        id=0,
        start=0.0,
        end=1.0,
        text="hi",
        avg_logprob=None,
        no_speech_prob=None,
        compression_ratio=None,
    )
    doc = build_document(
        source_path="C:/vault/ELS/meeting.wav",
        duration_sec=1.0,
        provider_name="local",
        model="small",
        device="cpu",
        compute_type="",
        language="en",
        language_probability=None,
        text="hi",
        segments=[segment],
        elapsed_sec=1.0,
        realtime_factor=1.0,
        cost_usd=0.006,
        currency="USD",
    )

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    result_path = write_atomic(doc, out_dir)

    on_disk = json.loads(result_path.read_text(encoding="utf-8"))
    written_segment = on_disk["segments"][0]
    assert written_segment["avg_logprob"] is None
    assert written_segment["no_speech_prob"] is None
    assert written_segment["compression_ratio"] is None


def test_write_atomic_round_trips_and_leaves_no_partial_file(tmp_path: Path) -> None:
    doc = _make_doc()
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    result_path = write_atomic(doc, out_dir)

    assert result_path == out_dir / "transcript.json"
    on_disk = json.loads(result_path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1

    remaining = list(out_dir.iterdir())
    assert remaining == [result_path]
    assert not any(p.suffix == ".tmp" for p in remaining)


def test_write_atomic_writes_non_latin_text_as_utf8_not_escapes(tmp_path: Path) -> None:
    r"""Cyrillic transcript text must be readable in the file, not `\uXXXX`."""
    russian = "Да, ребят, всем привет."
    segment = Segment(
        id=0,
        start=0.0,
        end=1.5,
        text=russian,
        avg_logprob=-0.2,
        no_speech_prob=0.01,
        compression_ratio=1.1,
        words=None,
    )
    doc = build_document(
        source_path="C:/vault/unsorted/260822 - source/source.mp4",
        duration_sec=90.0,
        provider_name="local",
        model="large-v3",
        device="cuda",
        compute_type="float16",
        language="ru",
        language_probability=0.99,
        text=russian,
        segments=[segment],
        elapsed_sec=12.0,
        realtime_factor=12.0 / 90.0,
        cost_usd=None,
        currency=None,
    )
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    result_path = write_atomic(doc, out_dir)

    raw = result_path.read_text(encoding="utf-8")
    assert russian in raw
    assert r"\u04" not in raw
    assert json.loads(raw)["text"] == russian


def test_write_atomic_overwrites_cleanly(tmp_path: Path) -> None:
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    write_atomic(_make_doc(), out_dir)
    second_doc = _make_doc(cost_usd=0.01)
    result_path = write_atomic(second_doc, out_dir)

    on_disk = json.loads(result_path.read_text(encoding="utf-8"))
    assert on_disk["stats"]["cost_usd"] == pytest.approx(0.01)

    remaining = list(out_dir.iterdir())
    assert remaining == [result_path]


def test_write_atomic_cleans_up_temp_file_on_dump_failure(tmp_path, monkeypatch) -> None:
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    import transcription.transcript as transcript_module

    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(transcript_module.json, "dump", _raise)

    with pytest.raises(RuntimeError):
        write_atomic(_make_doc(), out_dir)

    remaining = list(out_dir.iterdir())
    assert remaining == []


def test_write_atomic_previous_good_file_survives_a_failed_rewrite(tmp_path, monkeypatch) -> None:
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    write_atomic(_make_doc(), out_dir)
    good_bytes = (out_dir / "transcript.json").read_bytes()

    import transcription.transcript as transcript_module

    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(transcript_module.json, "dump", _raise)

    with pytest.raises(RuntimeError):
        write_atomic(_make_doc(cost_usd=99.0), out_dir)

    remaining = list(out_dir.iterdir())
    assert remaining == [out_dir / "transcript.json"]
    assert (out_dir / "transcript.json").read_bytes() == good_bytes


def test_job_create_rejects_empty_audio_path() -> None:
    with pytest.raises(ValidationError):
        JobCreate(audio_path="", output_dir="C:/vault/ELS")


def test_job_create_accepts_valid_paths() -> None:
    job = JobCreate(audio_path="C:/meetings/a.wav", output_dir="C:/vault/ELS")
    assert job.audio_path == "C:/meetings/a.wav"
    assert job.provider is None


def test_job_status_status_literal_set() -> None:
    for status in ("queued", "running", "succeeded", "failed", "cancelled"):
        job_status = JobStatus(job_id="abc", status=status, progress=0.0)
        assert job_status.status == status

    with pytest.raises(ValidationError):
        JobStatus(job_id="abc", status="bogus", progress=0.0)


def test_job_status_progress_is_clamped_to_0_1() -> None:
    over = JobStatus(job_id="abc", status="running", progress=1.5)
    under = JobStatus(job_id="abc", status="running", progress=-0.5)

    assert over.progress == 1.0
    assert under.progress == 0.0


def test_job_status_error_kind_typed_from_errors_module() -> None:
    job_status = JobStatus(
        job_id="abc",
        status="failed",
        progress=1.0,
        error_kind=ErrorKind.PROVIDER_AUTH,
        error_message="nope",
    )
    assert job_status.error_kind is ErrorKind.PROVIDER_AUTH


def test_health_model_state_literal_set() -> None:
    for state in ("unloaded", "loading", "loaded"):
        health = Health(
            status="ok",
            version="0.1.0",
            provider="local",
            model="large-v3",
            device="cuda",
            model_state=state,
        )
        assert health.model_state == state

    with pytest.raises(ValidationError):
        Health(
            status="ok",
            version="0.1.0",
            provider="local",
            model="large-v3",
            device="cuda",
            model_state="bogus",
        )


# -- diarization block (speaker classification) ------------------------------


def test_segment_speaker_is_omitted_when_absent_and_kept_when_set() -> None:
    from transcription.schema import DiarizationInfo

    unlabelled = Segment(id=0, start=0.0, end=1.0, text="hi")
    labelled = Segment(id=1, start=1.0, end=2.0, text="there", speaker="Speaker 1")

    assert "speaker" not in unlabelled.model_dump(mode="json")
    assert labelled.model_dump(mode="json")["speaker"] == "Speaker 1"

    # And through a full document round-trip.
    doc = _make_doc()
    doc = doc.model_copy(
        update={
            "segments": [labelled],
            "diarization": DiarizationInfo(
                status="succeeded", model="pyannote/speaker-diarization-3.1", speaker_count=1
            ),
        }
    )
    data = doc.model_dump(mode="json")
    assert data["segments"][0]["speaker"] == "Speaker 1"
    assert data["diarization"]["status"] == "succeeded"
    assert data["diarization"]["speaker_count"] == 1


def test_diarization_block_is_omitted_entirely_when_the_pass_never_ran() -> None:
    data = _make_doc().model_dump(mode="json")

    assert "diarization" not in data


def test_a_failed_diarization_pass_is_recorded_with_its_taxonomy_kind() -> None:
    from transcription.schema import DiarizationInfo

    info = DiarizationInfo(
        status="failed",
        model="pyannote/speaker-diarization-3.1",
        error_kind=ErrorKind.MODEL_LOAD,
        error_message="missing hf token",
    )
    doc = _make_doc().model_copy(update={"diarization": info})

    data = doc.model_dump(mode="json")
    assert data["diarization"]["status"] == "failed"
    assert data["diarization"]["error_kind"] == "model_load"


def test_job_create_accepts_an_optional_diarize_flag() -> None:
    assert JobCreate(audio_path="a.wav", output_dir="out").diarize is None
    assert JobCreate(audio_path="a.wav", output_dir="out", diarize=True).diarize is True
    assert JobCreate(audio_path="a.wav", output_dir="out", diarize=False).diarize is False
