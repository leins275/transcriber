"""Per-segment voice identification through its three seams: the engine's
`embed_spans`, the job manager's naming step, and `GET /v1/voices/status`.

Torch-free and model-free like the rest of the suite: the engine's one
torch-touching call (`_embed_batch`) is replaced, and the job manager runs
against a scripted diarizer.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeDiarizer, FakeProvider
from fastapi.testclient import TestClient

from transcription import providers
from transcription.app import create_app
from transcription.config import Config
from transcription.diarization import SpeakerTurn
from transcription.diarizer import PyannoteDiarizer
from transcription.errors import ErrorKind, ServiceError
from transcription.jobs import TERMINAL_STATUSES, JobManager
from transcription.ledger import Ledger
from transcription.providers.base import CancelToken
from transcription.voice_index import VOICE_INDEX_FILENAME

AUTH = {"Authorization": "Bearer test-token"}
RATE = 16000

ALICE = [1.0, 0.0, 0.0, 0.0]
BOB = [0.0, 1.0, 0.0, 0.0]
ALICE_ISH = [0.9, 0.1, 0.0, 0.2]
BOB_ISH = [0.1, 0.9, 0.0, 0.2]


# -- the engine: `PyannoteDiarizer.embed_spans` ---------------------------------


class FakeEmbedder:
    """Stands in for the pipeline's embedding model: answers, per crop,
    `[length in samples, first sample value]` so a test can see exactly
    which audio each span was embedded from."""

    min_num_samples = 400

    def __init__(self) -> None:
        self.batches: list[list[int]] = []

    def __call__(self, crops: list[list[float]]) -> list[list[float]]:
        self.batches.append([len(crop) for crop in crops])
        return [[float(len(crop)), float(crop[0])] for crop in crops]


def _engine(monkeypatch: pytest.MonkeyPatch, *, seconds: int, embedder: Any) -> PyannoteDiarizer:
    diarizer = PyannoteDiarizer(
        SimpleNamespace(
            diarization_model="m",
            diarization_model_path="",
            diarization_min_speakers=None,
            diarization_max_speakers=None,
            hf_token=None,
            device="cpu",
        )
    )
    # Sample i holds the value i, so a crop's first value is its offset.
    waveform = [float(i) for i in range(seconds * RATE)]
    monkeypatch.setattr(diarizer, "_ensure_pipeline", lambda: SimpleNamespace(_embedding=embedder))
    monkeypatch.setattr(diarizer, "_decode", lambda _path: {"waveform": [waveform]})
    monkeypatch.setattr(diarizer, "_embed_batch", lambda model, crops: model(crops))
    return diarizer


def test_each_span_is_embedded_from_its_own_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = FakeEmbedder()
    diarizer = _engine(monkeypatch, seconds=30, embedder=embedder)

    vectors = diarizer.embed_spans(
        Path("meeting.wav"),
        [(10.0, 13.0), (0.0, 2.0)],
        cancel=CancelToken(),
        max_span_sec=10.0,
    )

    # One batch, sorted shortest first and cropped (centred) to the
    # shortest member; results come back in the caller's order.
    assert embedder.batches == [[2 * RATE, 2 * RATE]]
    assert vectors == [
        [2.0 * RATE, 10.0 * RATE + RATE / 2],
        [2.0 * RATE, 0.0],
    ]


def test_a_long_span_is_embedded_from_its_middle(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = FakeEmbedder()
    diarizer = _engine(monkeypatch, seconds=60, embedder=embedder)

    (vector,) = diarizer.embed_spans(
        Path("meeting.wav"), [(10.0, 50.0)], cancel=CancelToken(), max_span_sec=10.0
    )

    assert vector == [10.0 * RATE, 25.0 * RATE]


def test_spans_the_model_cannot_use_come_back_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class NanEmbedder(FakeEmbedder):
        def __call__(self, crops: list[list[float]]) -> list[list[float]]:
            return [[float("nan"), 0.0] for _crop in crops]

    too_short = (1.0, 1.0 + 100 / RATE)
    past_the_end = (40.0, 45.0)
    diarizer = _engine(monkeypatch, seconds=30, embedder=FakeEmbedder())
    assert diarizer.embed_spans(
        Path("m.wav"), [too_short, past_the_end], cancel=CancelToken(), max_span_sec=10.0
    ) == [None, None]

    diarizer = _engine(monkeypatch, seconds=30, embedder=NanEmbedder())
    assert diarizer.embed_spans(
        Path("m.wav"), [(0.0, 3.0)], cancel=CancelToken(), max_span_sec=10.0
    ) == [None]


def test_a_pipeline_without_an_embedding_model_embeds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diarizer = _engine(monkeypatch, seconds=30, embedder=None)

    assert diarizer.embed_spans(
        Path("m.wav"), [(0.0, 3.0)], cancel=CancelToken(), max_span_sec=10.0
    ) == [None]
    assert diarizer.embed_spans(Path("m.wav"), [], cancel=CancelToken(), max_span_sec=10.0) == []


def test_spans_are_batched_and_progress_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = FakeEmbedder()
    diarizer = _engine(monkeypatch, seconds=30, embedder=embedder)
    seen: list[tuple[str, float | None]] = []

    vectors = diarizer.embed_spans(
        Path("m.wav"),
        [(0.0, 2.0)] * 40,
        cancel=CancelToken(),
        max_span_sec=10.0,
        on_progress=lambda phase, fraction: seen.append((phase, fraction)),
    )

    assert [len(batch) for batch in embedder.batches] == [32, 8]
    assert len(vectors) == 40 and all(vector is not None for vector in vectors)
    assert seen == [("matching voices in segments", 0.8), ("matching voices in segments", 1.0)]


def test_a_cancelled_job_stops_embedding_and_a_model_failure_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diarizer = _engine(monkeypatch, seconds=30, embedder=FakeEmbedder())
    cancelled = CancelToken()
    cancelled.set()
    with pytest.raises(ServiceError) as cancel_info:
        diarizer.embed_spans(Path("m.wav"), [(0.0, 3.0)], cancel=cancelled, max_span_sec=10.0)
    assert cancel_info.value.kind is ErrorKind.CANCELLED

    class Broken(FakeEmbedder):
        def __call__(self, crops: list[list[float]]) -> list[list[float]]:
            raise RuntimeError("CUDA out of memory")

    diarizer = _engine(monkeypatch, seconds=30, embedder=Broken())
    with pytest.raises(ServiceError) as failure_info:
        diarizer.embed_spans(Path("m.wav"), [(0.0, 3.0)], cancel=CancelToken(), max_span_sec=10.0)
    assert failure_info.value.kind is ErrorKind.MODEL_LOAD


# -- the job manager's naming step ------------------------------------------------


@pytest.fixture
def vault(tmp_app_dir: Path) -> Path:
    return tmp_app_dir / "vault"


@pytest.fixture
def config(tmp_app_dir: Path, vault: Path) -> Config:
    vault.mkdir()
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        index_db_path=str(vault / ".transcriber" / "index.sqlite3"),
        vault_root=str(vault),
        token="test-token",  # noqa: S106 -- test fixture
    )


@pytest.fixture
def ledger(config: Config) -> Iterator[Ledger]:
    led = Ledger(config.db_path)
    yield led
    led.close()


def _write_known_voices(vault: Path) -> None:
    """A sibling meeting where the operator named Alice and Bob."""
    sibling = vault / "ACME" / "260830 - Kickoff"
    sibling.mkdir(parents=True)
    (sibling / "transcript.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"id": 0, "start": 0.0, "end": 30.0, "text": "hi", "speaker": "Speaker 1"},
                    {"id": 1, "start": 30.0, "end": 60.0, "text": "hi", "speaker": "Speaker 2"},
                ],
                "diarization": {
                    "status": "succeeded",
                    "model": "m",
                    "speaker_embeddings": {"Speaker 1": ALICE, "Speaker 2": BOB},
                },
            }
        ),
        encoding="utf-8",
    )
    (sibling / "speakers.json").write_text(
        json.dumps({"schema_version": 1, "assignments": {"0": "Alice", "1": "Bob"}}),
        encoding="utf-8",
    )


def _write_meeting_to_identify(vault: Path) -> Path:
    """Seven 5-second segments (and one too short to embed), undiarized."""
    meeting = vault / "ACME" / "260901 - Planning"
    meeting.mkdir(parents=True)
    (meeting / "source.wav").write_bytes(b"fake-audio-bytes")
    segments = [
        {"id": index, "start": index * 5.0, "end": index * 5.0 + 5.0, "text": "word "}
        for index in range(7)
    ]
    segments.append({"id": 7, "start": 35.0, "end": 35.5, "text": "ok"})
    doc = {
        "schema_version": 1,
        "created_at": "2026-09-01T10:00:00+00:00",
        "source": {
            "path": str(meeting / "source.wav"),
            "filename": "source.wav",
            "duration_sec": 35.5,
        },
        "provider": {"name": "fake", "model": "fake-model", "device": "cpu", "compute_type": ""},
        "language": "en",
        "language_probability": 0.9,
        "text": "word " * 8,
        "segments": segments,
        "stats": {"elapsed_sec": 0.1, "realtime_factor": 0.1, "cost_usd": None, "currency": None},
    }
    (meeting / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
    return meeting


class SpanEmbeddingDiarizer(FakeDiarizer):
    """A `FakeDiarizer` that can also embed spans: every span starting at or
    after `bob_from` seconds sounds like Bob, the rest like Alice."""

    def __init__(self, *, bob_from: float, fail: Exception | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bob_from = bob_from
        self.fail = fail
        self.span_calls: list[list[tuple[float, float]]] = []

    def embed_spans(
        self,
        audio_path: Path,
        spans: Sequence[tuple[float, float]],
        *,
        cancel: CancelToken,
        max_span_sec: float,
        on_progress: Any = None,
    ) -> list[list[float] | None]:
        assert audio_path.name == "source.wav"
        assert max_span_sec == 10.0
        self.span_calls.append(list(spans))
        if self.fail is not None:
            raise self.fail
        if on_progress is not None:
            on_progress("matching voices in segments", 1.0)
        return [BOB_ISH if start >= self.bob_from else ALICE_ISH for start, _end in spans]


def _one_merged_cluster(**kwargs: Any) -> SpanEmbeddingDiarizer:
    """The diarizer heard one voice for the whole meeting, and its averaged
    embedding sounds like Alice."""
    return SpanEmbeddingDiarizer(
        turns=[SpeakerTurn(start=0.0, end=36.0, speaker="SPEAKER_00")],
        embeddings={"SPEAKER_00": ALICE_ISH},
        **kwargs,
    )


async def _run_diarize(manager: JobManager, meeting: Path) -> Any:
    await manager.start()
    job_id = await manager.submit(
        job_type="diarize", input_path=str(meeting), output_dir=str(meeting)
    )
    deadline = time.monotonic() + 5.0
    while manager.status(job_id).status not in TERMINAL_STATUSES:
        assert time.monotonic() < deadline, "the job did not finish"
        await asyncio.sleep(0.01)
    return manager.status(job_id)


def _assignments(meeting: Path) -> dict[str, str]:
    data: dict[str, dict[str, str]] = json.loads(
        (meeting / "speakers.json").read_text(encoding="utf-8")
    )
    return data["assignments"]


async def test_a_diarize_job_names_segments_by_their_own_voice(
    config: Config, ledger: Ledger, vault: Path
) -> None:
    providers.register("fake", FakeProvider)
    _write_known_voices(vault)
    meeting = _write_meeting_to_identify(vault)
    diarizer = _one_merged_cluster(bob_from=20.0)
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        job = await _run_diarize(manager, meeting)

        assert job.status == "succeeded", job.error_message
        assert job.warnings == []
        # Only the seven segments long enough to carry a voice were embedded.
        assert diarizer.span_calls == [[(index * 5.0, index * 5.0 + 5.0) for index in range(7)]]
        manifest = json.loads(job.result_json or "{}")
        assert manifest["auto_named_segments"] == 8
        assert manifest["segments_named_by_own_voice"] == 3
        assert manifest["recognized_names"] == ["Alice", "Bob"]
        assert _assignments(meeting) == {
            "0": "Alice",
            "1": "Alice",
            "2": "Alice",
            "3": "Alice",
            "4": "Bob",
            "5": "Bob",
            "6": "Bob",
            # Too short to embed: it keeps its cluster's name.
            "7": "Alice",
        }
        # The memory was read through the index, which now exists on disk
        # next to the search index.
        assert (vault / ".transcriber" / VOICE_INDEX_FILENAME).is_file()
    finally:
        await manager.aclose()


async def test_a_failing_segment_pass_is_a_warning_and_clusters_are_still_named(
    config: Config, ledger: Ledger, vault: Path
) -> None:
    providers.register("fake", FakeProvider)
    _write_known_voices(vault)
    meeting = _write_meeting_to_identify(vault)
    diarizer = _one_merged_cluster(
        bob_from=20.0, fail=ServiceError(ErrorKind.MODEL_LOAD, "CUDA out of memory")
    )
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        job = await _run_diarize(manager, meeting)

        assert job.status == "succeeded", job.error_message
        assert job.warnings == ["per-segment voice matching skipped: CUDA out of memory"]
        manifest = json.loads(job.result_json or "{}")
        assert manifest["segments_named_by_own_voice"] == 0
        assert set(_assignments(meeting).values()) == {"Alice"}
    finally:
        await manager.aclose()


async def test_a_project_that_knows_nobody_skips_the_segment_pass(
    config: Config, ledger: Ledger, vault: Path
) -> None:
    providers.register("fake", FakeProvider)
    meeting = _write_meeting_to_identify(vault)
    diarizer = _one_merged_cluster(bob_from=20.0)
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        job = await _run_diarize(manager, meeting)

        assert job.status == "succeeded", job.error_message
        assert diarizer.span_calls == []
        assert not (meeting / "speakers.json").exists()
    finally:
        await manager.aclose()


async def test_an_engine_without_span_embedding_names_by_cluster_only(
    config: Config, ledger: Ledger, vault: Path
) -> None:
    providers.register("fake", FakeProvider)
    _write_known_voices(vault)
    meeting = _write_meeting_to_identify(vault)
    diarizer = FakeDiarizer(
        turns=[SpeakerTurn(start=0.0, end=36.0, speaker="SPEAKER_00")],
        embeddings={"SPEAKER_00": ALICE_ISH},
    )
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        job = await _run_diarize(manager, meeting)

        assert job.status == "succeeded", job.error_message
        assert job.warnings == []
        assert set(_assignments(meeting).values()) == {"Alice"}
    finally:
        await manager.aclose()


# -- GET /v1/voices/status --------------------------------------------------------


def test_voices_status_reports_the_project_and_follows_an_edit(config: Config, vault: Path) -> None:
    _write_known_voices(vault)
    (vault / "ACME" / "260829 - Empty shell").mkdir()

    with TestClient(create_app(config)) as client:
        first = client.get("/v1/voices/status", params={"project": "ACME"}, headers=AUTH)

        assert first.status_code == 200
        body = first.json()
        assert body["project"] == "ACME"
        assert body["rescanned"] == ["260830 - Kickoff"]
        assert [(voice["name"], voice["samples"]) for voice in body["voices"]] == [
            ("Alice", 1),
            ("Bob", 1),
        ]
        states = {meeting["name"]: meeting["state"] for meeting in body["meetings"]}
        assert states == {"260830 - Kickoff": "named", "260829 - Empty shell": "no_transcript"}
        kickoff = next(m for m in body["meetings"] if m["name"] == "260830 - Kickoff")
        assert kickoff["voices"][0] == {
            "label": "Speaker 1",
            "name": "Alice",
            "speech_sec": 30.0,
            "quality": "ok",
            "conflicts_with": None,
        }

        # Nothing changed: nothing is re-read.
        second = client.get("/v1/voices/status", params={"project": "ACME"}, headers=AUTH)
        assert second.json()["rescanned"] == []

        # The operator renames a speaker; the next read picks it up by itself.
        speakers = vault / "ACME" / "260830 - Kickoff" / "speakers.json"
        speakers.write_text(
            json.dumps({"schema_version": 1, "assignments": {"0": "Alicia", "1": "Bob"}}),
            encoding="utf-8",
        )
        third = client.get("/v1/voices/status", params={"project": "ACME"}, headers=AUTH)
        assert third.json()["rescanned"] == ["260830 - Kickoff"]
        assert [voice["name"] for voice in third.json()["voices"]] == ["Alicia", "Bob"]


def test_voices_status_requires_auth_and_a_real_project(config: Config, vault: Path) -> None:
    with TestClient(create_app(config)) as client:
        assert client.get("/v1/voices/status", params={"project": "ACME"}).status_code == 401
        for bad in ("../evil", "", "a/../b"):
            response = client.get("/v1/voices/status", params={"project": bad}, headers=AUTH)
            assert response.status_code == 400, bad
        # A project nobody has filed anything under is simply empty.
        empty = client.get("/v1/voices/status", params={"project": "NOPE"}, headers=AUTH)
        assert empty.status_code == 200
        assert empty.json()["voices"] == [] and empty.json()["meetings"] == []
