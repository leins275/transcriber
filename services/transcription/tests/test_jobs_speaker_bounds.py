"""Per-job speaker bounds and matching threshold, and what counts as a name.

Covers T2 of the roster-bounds feature: `POST /v1/jobs` carries optional
`min_speakers` / `max_speakers` / `speaker_match_threshold`, the job manager
hands the bounds to the diarization engine on both diarizing paths and the
threshold to both naming passes, invalid tuning is refused before a ledger
row exists, `GET /v1/jobs/{id}` echoes none of it, and a sibling meeting
whose voice is "named" only by a generic `Speaker N` label pre-names
nothing (FR-1, FR-4, FR-6).

Model-free, GPU-free and network-free (NFR-1): `FakeProvider` stands in for
whisper and `FakeDiarizer` (plus the local `RecordingDiarizer`, which reads
back the kwargs the engine seam was handed) for pyannote.

Note on the refusal contract: the blueprint says 422, but this app answers
`400` with `error_kind: "invalid_request"` for every `RequestValidationError`
(`app.py::_validation_error_handler`). The app's real contract is what is
asserted here.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDiarizer, FakeProvider
from fastapi.testclient import TestClient

from transcription import providers
from transcription.app import create_app
from transcription.config import Config
from transcription.diarization import DiarizationOutput
from transcription.jobs import TERMINAL_STATUSES, JobManager
from transcription.ledger import Ledger
from transcription.providers.base import CancelToken

AUTH = {"Authorization": "Bearer test-token"}

# A voice at cosine ~0.45 to [1.0, 0.0]: below the service default 0.5,
# above the strict 0.4 the shell sends for a roster project.
NEAR_MISS_EMBEDDING = [0.45, 0.893]


class RecordingDiarizer(FakeDiarizer):
    """A `FakeDiarizer` that reads back the extra kwargs it was handed.

    The engine seam is the observation point: `bounds` holds one dict per
    pass with exactly the keys the job manager passed beyond `cancel` /
    `on_progress`, so an omitted bound and a bound passed as `None` are
    distinguishable.
    """

    def __init__(self, config: Any = None, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self.bounds: list[dict[str, Any]] = []

    def diarize(
        self,
        audio_path: Path,
        *,
        cancel: CancelToken,
        on_progress: Callable[[str, float | None], None] | None = None,
        **bounds: Any,
    ) -> DiarizationOutput:
        self.bounds.append(dict(bounds))
        return super().diarize(audio_path, cancel=cancel, on_progress=on_progress)


@pytest.fixture
def config(tmp_app_dir: Path) -> Config:
    providers.register("fake", FakeProvider)
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


def _app(config: Config, diarizer: FakeDiarizer) -> Any:
    def job_manager_factory(cfg: Config, led: Ledger) -> JobManager:
        return JobManager(cfg, led, diarizer_factory=lambda _cfg: diarizer)

    return create_app(config, job_manager_factory=job_manager_factory)


def _wait_terminal(client: TestClient, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        body: dict[str, Any] = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if body["status"] in TERMINAL_STATUSES:
            return body
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} still {body['status']}")
        time.sleep(0.01)


async def _wait_until_terminal(manager: JobManager, job_id: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.status(job_id).status not in TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not finish in {timeout}s")
        await asyncio.sleep(0.01)


def _submit(client: TestClient, body: dict[str, Any]) -> str:
    response = client.post("/v1/jobs", json=body, headers=AUTH)
    assert response.status_code == 202, response.text
    job_id: str = response.json()["job_id"]
    return job_id


def _run_over_http(config: Config, diarizer: FakeDiarizer, body: dict[str, Any]) -> dict[str, Any]:
    """Submit one job and return its terminal status body."""
    with TestClient(_app(config, diarizer)) as client:
        return _wait_terminal(client, _submit(client, body))


def _recording(project: Path, name: str = "260902 - Standup") -> Path:
    """An unfiled recording sitting in its meeting folder, ready to decode."""
    audio = project / name / "source.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake-audio-bytes")
    return audio


def _filed_meeting(project: Path, name: str = "260903 - Review") -> Path:
    """A meeting transcribed without diarization: recording + transcript,
    no `speakers.json` yet."""
    meeting = project / name
    meeting.mkdir(parents=True)
    (meeting / "source.wav").write_bytes(b"fake-audio-bytes")
    doc = {
        "schema_version": 1,
        "created_at": "2026-09-03T10:00:00+00:00",
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
    return meeting


def _sibling_naming_a_voice(project: Path, *, name: str, embedding: list[float]) -> Path:
    """A sibling meeting in the same project whose one diarized voice
    carries `name` in the operator's `speakers.json` -- the project's
    speaker memory a later pass matches against."""
    sibling = project / "260901 - Planning"
    sibling.mkdir(parents=True)
    (sibling / "transcript.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"id": 0, "start": 0.0, "end": 1.0, "text": "hi", "speaker": "Speaker 2"}
                ],
                "diarization": {
                    "status": "succeeded",
                    "model": "m",
                    "speaker_embeddings": {"Speaker 2": embedding},
                },
            }
        ),
        encoding="utf-8",
    )
    (sibling / "speakers.json").write_text(
        json.dumps({"schema_version": 1, "assignments": {"0": name}}), encoding="utf-8"
    )
    return sibling


def _write_assignments(meeting: Path, assignments: dict[str, str]) -> None:
    """The meeting's own `speakers.json`, as the transcript viewer saves it."""
    (meeting / "speakers.json").write_text(
        json.dumps({"schema_version": 1, "assignments": assignments}), encoding="utf-8"
    )


def _assignments(meeting: Path) -> dict[str, str]:
    data = json.loads((meeting / "speakers.json").read_text(encoding="utf-8"))
    result: dict[str, str] = data["assignments"]
    return result


# -- the bounds reach the engine ----------------------------------------------


def test_a_transcribe_job_hands_only_the_bound_it_was_given_to_the_engine(
    config: Config, tmp_app_dir: Path
) -> None:
    audio = _recording(tmp_app_dir / "ACME")
    diarizer = RecordingDiarizer()

    status = _run_over_http(
        config,
        diarizer,
        {
            "audio_path": str(audio),
            "output_dir": str(audio.parent),
            "diarize": True,
            "max_speakers": 3,
        },
    )

    assert status["status"] == "succeeded", status["error_message"]
    assert diarizer.bounds == [{"max_speakers": 3}]


def test_a_diarize_job_hands_both_of_its_speaker_bounds_to_the_engine(
    config: Config, tmp_app_dir: Path
) -> None:
    meeting = _filed_meeting(tmp_app_dir / "ACME")
    diarizer = RecordingDiarizer()

    status = _run_over_http(
        config,
        diarizer,
        {
            "job_type": "diarize",
            "input_path": str(meeting),
            "output_dir": str(meeting),
            "min_speakers": 2,
            "max_speakers": 2,
        },
    )

    assert status["status"] == "succeeded", status["error_message"]
    assert diarizer.bounds == [{"min_speakers": 2, "max_speakers": 2}]


def test_a_job_without_bounds_still_drives_an_engine_that_takes_none(
    config: Config, tmp_app_dir: Path
) -> None:
    """A bounds-free pass must call the engine exactly as before -- kwargs
    omitted, not handed over as `None` -- so an older engine still runs."""
    audio = _recording(tmp_app_dir / "ACME")

    status = _run_over_http(
        config,
        FakeDiarizer(),
        {"audio_path": str(audio), "output_dir": str(audio.parent), "diarize": True},
    )

    assert status["status"] == "succeeded", status["error_message"]
    doc = json.loads((audio.parent / "transcript.json").read_text(encoding="utf-8"))
    assert [seg["speaker"] for seg in doc["segments"]] == ["Speaker 1", "Speaker 2"]


# -- refusals: nothing runs, nothing is recorded -------------------------------


@pytest.fixture
def client(config: Config) -> TestClient:
    # Deliberately no `with` block: a refused request never reaches the
    # worker, so lifespan need not run (the pattern of test_api_contract.py).
    return TestClient(_app(config, FakeDiarizer()))


@pytest.mark.parametrize(
    "tuning",
    [
        pytest.param({"max_speakers": 0}, id="max-speakers-below-one"),
        pytest.param({"min_speakers": 4, "max_speakers": 2}, id="minimum-above-maximum"),
        pytest.param({"speaker_match_threshold": 1.5}, id="threshold-above-one"),
        pytest.param({"speaker_match_threshold": -0.1}, id="negative-threshold"),
    ],
)
def test_out_of_range_speaker_tuning_is_refused_and_leaves_no_job_behind(
    client: TestClient, tmp_app_dir: Path, tuning: dict[str, Any]
) -> None:
    audio = _recording(tmp_app_dir / "ACME")
    body = {"audio_path": str(audio), "output_dir": str(audio.parent), "diarize": True, **tuning}

    response = client.post("/v1/jobs", json=body, headers=AUTH)

    assert response.status_code == 400, response.text
    assert response.json()["error_kind"] == "invalid_request"
    assert client.get("/v1/jobs", headers=AUTH).json() == []


@pytest.mark.parametrize(
    "tuning",
    [
        pytest.param({"max_speakers": 2}, id="bounds"),
        pytest.param({"speaker_match_threshold": 0.4}, id="threshold"),
    ],
)
@pytest.mark.parametrize(
    "job_type",
    [
        pytest.param("summarize", id="summarize"),
        pytest.param("export", id="export"),
        # An index job takes no paths at all, so tuning is the only thing
        # left for the validator to refuse -- exactly the criterion.
        pytest.param("index", id="index"),
    ],
)
def test_speaker_tuning_is_refused_on_a_job_that_never_diarizes(
    client: TestClient, tmp_app_dir: Path, job_type: str, tuning: dict[str, Any]
) -> None:
    meeting = _filed_meeting(tmp_app_dir / "ACME")
    paths = {} if job_type == "index" else {"input_path": str(meeting), "output_dir": str(meeting)}
    body = {"job_type": job_type, **paths, **tuning}

    response = client.post("/v1/jobs", json=body, headers=AUTH)

    assert response.status_code == 400, response.text
    assert response.json()["error_kind"] == "invalid_request"
    # The schema refused it, not `submit`: an index job would otherwise be
    # turned away for a missing vault root with a message of its own, and
    # this case would pass without the tuning rule existing at all.
    assert response.json()["error_message"] == "request validation failed"
    assert client.get("/v1/jobs", headers=AUTH).json() == []


def test_the_job_status_echoes_neither_the_bounds_nor_the_threshold(
    config: Config, tmp_app_dir: Path
) -> None:
    audio = _recording(tmp_app_dir / "ACME")
    body = {
        "audio_path": str(audio),
        "output_dir": str(audio.parent),
        "diarize": True,
        "max_speakers": 2,
        "speaker_match_threshold": 0.4,
    }

    status = _run_over_http(config, RecordingDiarizer(), body)

    assert "max_speakers" not in status
    assert "speaker_match_threshold" not in status


# -- what counts as a name (FR-4) ---------------------------------------------


async def test_a_sibling_that_named_a_voice_only_generically_pre_names_nothing(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    """`Speaker 2` in a sibling's `speakers.json` is a seeded label, not a
    person: the same voice next door must open unnamed."""
    project = tmp_app_dir / "ACME"
    _sibling_naming_a_voice(project, name="Speaker 2", embedding=[1.0, 0.0])
    audio = _recording(project)
    diarizer = FakeDiarizer(embeddings={"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        await manager.start()
        job_id = await manager.submit(
            audio_path=str(audio), output_dir=str(audio.parent), diarize=True
        )
        await _wait_until_terminal(manager, job_id)

        assert manager.status(job_id).status == "succeeded"
        assert not (audio.parent / "speakers.json").exists()
    finally:
        await manager.aclose()


async def test_a_sibling_that_named_a_voice_anna_still_pre_names_it(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    project = tmp_app_dir / "ACME"
    _sibling_naming_a_voice(project, name="Anna", embedding=[1.0, 0.0])
    audio = _recording(project)
    diarizer = FakeDiarizer(embeddings={"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        await manager.start()
        job_id = await manager.submit(
            audio_path=str(audio), output_dir=str(audio.parent), diarize=True
        )
        await _wait_until_terminal(manager, job_id)

        assert manager.status(job_id).status == "succeeded"
        assert _assignments(audio.parent) == {"0": "Anna"}
    finally:
        await manager.aclose()


# -- the per-job matching threshold (FR-6) ------------------------------------


def test_a_near_miss_voice_stays_unnamed_under_the_configured_threshold(
    config: Config, tmp_app_dir: Path
) -> None:
    """The control for the pair below: at the service default 0.5, a voice
    0.45 away from Anna's is somebody else."""
    project = tmp_app_dir / "ACME"
    _sibling_naming_a_voice(project, name="Anna", embedding=[1.0, 0.0])
    audio = _recording(project)

    status = _run_over_http(
        config,
        FakeDiarizer(embeddings={"SPEAKER_00": NEAR_MISS_EMBEDDING}),
        {"audio_path": str(audio), "output_dir": str(audio.parent), "diarize": True},
    )

    assert status["status"] == "succeeded", status["error_message"]
    assert not (audio.parent / "speakers.json").exists()


def test_a_near_miss_voice_is_named_when_the_job_lowers_the_threshold(
    config: Config, tmp_app_dir: Path
) -> None:
    project = tmp_app_dir / "ACME"
    _sibling_naming_a_voice(project, name="Anna", embedding=[1.0, 0.0])
    audio = _recording(project)

    status = _run_over_http(
        config,
        FakeDiarizer(embeddings={"SPEAKER_00": NEAR_MISS_EMBEDDING}),
        {
            "audio_path": str(audio),
            "output_dir": str(audio.parent),
            "diarize": True,
            "speaker_match_threshold": 0.4,
        },
    )

    assert status["status"] == "succeeded", status["error_message"]
    assert _assignments(audio.parent) == {"0": "Anna"}


async def test_a_diarize_jobs_threshold_reaches_its_naming_pass_too(
    config: Config, ledger: Ledger, tmp_app_dir: Path
) -> None:
    """The second naming path: labelling an already-filed meeting honours
    the job's threshold, not only the config's."""
    project = tmp_app_dir / "ACME"
    _sibling_naming_a_voice(project, name="Anna", embedding=[1.0, 0.0])
    meeting = _filed_meeting(project)
    diarizer = FakeDiarizer(embeddings={"SPEAKER_00": NEAR_MISS_EMBEDDING})
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        await manager.start()
        job_id = await manager.submit(
            job_type="diarize",
            input_path=str(meeting),
            output_dir=str(meeting),
            speaker_match_threshold=0.4,
        )
        await _wait_until_terminal(manager, job_id)

        assert manager.status(job_id).status == "succeeded", manager.status(job_id).error_message
        assert _assignments(meeting) == {"0": "Anna"}
    finally:
        await manager.aclose()


@pytest.mark.parametrize(
    ("seeded", "expected"),
    [
        pytest.param("Speaker 1", "Anna", id="a-seeded-generic-label-gives-way"),
        pytest.param("Boris", "Boris", id="an-operator-name-wins"),
    ],
)
async def test_a_re_run_pre_names_over_a_seeded_label_but_never_over_a_real_name(
    config: Config, ledger: Ledger, tmp_app_dir: Path, seeded: str, expected: str
) -> None:
    """A meeting the operator has opened once carries `Speaker N` for every
    untouched segment in its own `speakers.json` (the viewer persists the
    whole seeded map). Re-running "Identify speakers" under a roster's
    lowered threshold must still be able to pre-name those segments; a name
    the operator typed is never overwritten."""
    project = tmp_app_dir / "ACME"
    _sibling_naming_a_voice(project, name="Anna", embedding=[1.0, 0.0])
    meeting = _filed_meeting(project)
    _write_assignments(meeting, {"0": seeded})
    diarizer = FakeDiarizer(embeddings={"SPEAKER_00": NEAR_MISS_EMBEDDING})
    manager = JobManager(config, ledger, diarizer_factory=lambda _cfg: diarizer)
    try:
        await manager.start()
        job_id = await manager.submit(
            job_type="diarize",
            input_path=str(meeting),
            output_dir=str(meeting),
            speaker_match_threshold=0.4,
        )
        await _wait_until_terminal(manager, job_id)

        assert manager.status(job_id).status == "succeeded", manager.status(job_id).error_message
        assert _assignments(meeting) == {"0": expected}
    finally:
        await manager.aclose()
