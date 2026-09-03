"""Tests for the pyannote engine seam (`diarizer.py`).

The real `pyannote.audio`/torch stack is never imported here (FR-15): the
`_import_pyannote` / `_resolve_torch_device` seams are monkeypatched with
in-memory fakes, exactly as the provider tests fake `WhisperModel`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from transcription.diarization import SpeakerTurn
from transcription.diarizer import PyannoteDiarizer
from transcription.errors import ErrorKind, ServiceError
from transcription.providers.base import CancelToken


@dataclass
class FakeAnnotationTurn:
    start: float
    end: float


class FakeAnnotation:
    def __init__(self, tracks: list[tuple[float, float, str]]) -> None:
        self._tracks = tracks

    def itertracks(self, *, yield_label: bool) -> Any:
        assert yield_label
        for start, end, label in self._tracks:
            yield FakeAnnotationTurn(start, end), "_", label

    def labels(self) -> list[str]:
        # pyannote answers the distinct labels in sorted order; the
        # embeddings matrix rows follow this order.
        return sorted({label for _start, _end, label in self._tracks})


@dataclass
class FakePipeline:
    tracks: list[tuple[float, float, str]]
    raise_on_call: Exception | None = None
    # What `return_embeddings=True` answers alongside the annotation; a
    # pipeline without embedding support raises TypeError on the kwarg.
    embeddings: list[list[float]] | None = None
    supports_embeddings: bool = True
    devices: list[Any] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def to(self, device: Any) -> None:
        self.devices.append(device)

    def __call__(self, audio: str, **kwargs: Any) -> Any:
        wants_embeddings = bool(kwargs.pop("return_embeddings", False))
        if wants_embeddings and not self.supports_embeddings:
            raise TypeError("unexpected keyword argument 'return_embeddings'")
        self.calls.append((audio, kwargs))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        annotation = FakeAnnotation(self.tracks)
        if wants_embeddings:
            return annotation, self.embeddings
        return annotation


class FakePipelineClass:
    """Stands in for `pyannote.audio.Pipeline`; records `from_pretrained` calls."""

    def __init__(self, pipeline: FakePipeline | None) -> None:
        self.pipeline = pipeline
        self.from_pretrained_calls: list[tuple[str, dict[str, Any]]] = []
        self.raise_on_load: Exception | None = None

    def from_pretrained(self, source: str, **kwargs: Any) -> FakePipeline | None:
        self.from_pretrained_calls.append((source, kwargs))
        if self.raise_on_load is not None:
            raise self.raise_on_load
        return self.pipeline


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "diarization_model": "pyannote/speaker-diarization-3.1",
        "diarization_model_path": "",
        "diarization_min_speakers": None,
        "diarization_max_speakers": None,
        "hf_token": None,
        "device": "auto",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    diarizer: PyannoteDiarizer,
    pipeline_cls: FakePipelineClass,
    *,
    device: str = "cpu",
) -> None:
    monkeypatch.setattr(diarizer, "_import_pyannote", lambda: pipeline_cls)
    # The real decoder needs a real recording (and torch); the fakes below
    # only ever look at what was handed to the pipeline, so hand them the
    # file name.
    monkeypatch.setattr(diarizer, "_decode", lambda path: path.name)

    def fake_resolve() -> str:
        diarizer.device = device if diarizer.device == "auto" else diarizer.device
        return diarizer.device

    monkeypatch.setattr(diarizer, "_resolve_torch_device", fake_resolve)


def test_diarize_returns_sorted_speaker_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline(tracks=[(5.0, 6.0, "SPEAKER_01"), (0.0, 2.0, "SPEAKER_00")])
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    output = diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert output.turns == [
        SpeakerTurn(start=0.0, end=2.0, speaker="SPEAKER_00"),
        SpeakerTurn(start=5.0, end=6.0, speaker="SPEAKER_01"),
    ]


def test_the_pipeline_is_loaded_once_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline_cls = FakePipelineClass(FakePipeline(tracks=[]))
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, pipeline_cls)

    diarizer.diarize(Path("a.wav"), cancel=CancelToken())
    diarizer.diarize(Path("b.wav"), cancel=CancelToken())

    assert len(pipeline_cls.from_pretrained_calls) == 1


def test_a_missing_pyannote_package_is_a_classified_model_load_failure() -> None:
    diarizer = PyannoteDiarizer(_config())

    def raise_import_error() -> Any:
        raise ServiceError(
            ErrorKind.MODEL_LOAD,
            "speaker diarization requires the optional 'pyannote.audio' package "
            "(install the service's 'diarization' extra)",
        )

    # The real `_import_pyannote` raises exactly this when the import fails;
    # asserting on the real one would need pyannote absent from the
    # environment, which the suite cannot guarantee either way.
    diarizer._import_pyannote = raise_import_error  # type: ignore[method-assign]

    with pytest.raises(ServiceError) as exc_info:
        diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert exc_info.value.kind is ErrorKind.MODEL_LOAD
    assert "diarization" in exc_info.value.message


def test_a_gated_model_answering_none_names_the_token_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline_cls = FakePipelineClass(None)  # from_pretrained -> None: gated, no terms
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, pipeline_cls)

    with pytest.raises(ServiceError) as exc_info:
        diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert exc_info.value.kind is ErrorKind.MODEL_LOAD
    assert "token" in exc_info.value.message.lower()


def test_a_load_failure_is_model_load(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline_cls = FakePipelineClass(FakePipeline(tracks=[]))
    pipeline_cls.raise_on_load = RuntimeError("could not download model")
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, pipeline_cls)

    with pytest.raises(ServiceError) as exc_info:
        diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert exc_info.value.kind is ErrorKind.MODEL_LOAD


def test_an_auth_shaped_runtime_failure_is_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(tracks=[], raise_on_call=RuntimeError("401 Client Error: gated repo"))
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    with pytest.raises(ServiceError) as exc_info:
        diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert exc_info.value.kind is ErrorKind.MODEL_LOAD


def test_a_plain_runtime_failure_over_the_audio_is_audio_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(tracks=[], raise_on_call=RuntimeError("unreadable waveform"))
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    with pytest.raises(ServiceError) as exc_info:
        diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert exc_info.value.kind is ErrorKind.AUDIO_DECODE


def test_cancellation_is_honoured_before_the_pipeline_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline_cls = FakePipelineClass(FakePipeline(tracks=[]))
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, pipeline_cls)
    cancel = CancelToken()
    cancel.set()

    with pytest.raises(ServiceError) as exc_info:
        diarizer.diarize(Path("meeting.wav"), cancel=cancel)

    assert exc_info.value.kind is ErrorKind.CANCELLED
    assert pipeline_cls.from_pretrained_calls == []


def test_speaker_bounds_are_passed_through_to_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(tracks=[])
    diarizer = PyannoteDiarizer(_config(diarization_min_speakers=2, diarization_max_speakers=4))
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert pipeline.calls == [("meeting.wav", {"min_speakers": 2, "max_speakers": 4})]


def test_the_hf_token_is_handed_to_from_pretrained(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline_cls = FakePipelineClass(FakePipeline(tracks=[]))
    diarizer = PyannoteDiarizer(_config(hf_token="hf_secret"))  # noqa: S106 -- test fixture
    _wire(monkeypatch, diarizer, pipeline_cls)

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    source, kwargs = pipeline_cls.from_pretrained_calls[0]
    assert source == "pyannote/speaker-diarization-3.1"
    assert kwargs == {"use_auth_token": "hf_secret"}


def test_a_local_snapshot_directory_is_loaded_by_its_config_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = tmp_path / "diarization-snapshot"
    snapshot.mkdir()
    (snapshot / "config.yaml").write_text("pipeline: {}", encoding="utf-8")
    pipeline_cls = FakePipelineClass(FakePipeline(tracks=[]))
    diarizer = PyannoteDiarizer(_config(diarization_model_path=str(snapshot)))
    _wire(monkeypatch, diarizer, pipeline_cls)

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    source, _kwargs = pipeline_cls.from_pretrained_calls[0]
    assert source == str(snapshot / "config.yaml")


def test_an_explicit_device_is_honoured_over_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline(tracks=[])
    diarizer = PyannoteDiarizer(_config(device="cpu"))
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline), device="cuda")

    diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert diarizer.device == "cpu"


# -- per-speaker embeddings (project-level speaker memory groundwork) --------


def test_embeddings_are_mapped_to_raw_labels_in_labels_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(
        tracks=[(5.0, 6.0, "SPEAKER_01"), (0.0, 2.0, "SPEAKER_00")],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],  # rows follow labels() order: 00, 01
    )
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    output = diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert output.embeddings == {
        "SPEAKER_00": [1.0, 0.0],
        "SPEAKER_01": [0.0, 1.0],
    }


def test_a_pipeline_without_embedding_support_still_diarizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hand-picked pipeline may predate `return_embeddings`; the TypeError
    # triggers one retry without the kwarg and the turns still land.
    pipeline = FakePipeline(tracks=[(0.0, 2.0, "SPEAKER_00")], supports_embeddings=False)
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    output = diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert output.turns == [SpeakerTurn(start=0.0, end=2.0, speaker="SPEAKER_00")]
    assert output.embeddings is None


def test_a_non_finite_embedding_row_is_dropped_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pyannote hands back a NaN row for a speaker with no clean speech;
    # storing it would poison any later similarity math.
    pipeline = FakePipeline(
        tracks=[(0.0, 2.0, "SPEAKER_00"), (3.0, 4.0, "SPEAKER_01")],
        embeddings=[[0.5, 0.5], [float("nan"), 1.0]],
    )
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    output = diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert output.embeddings == {"SPEAKER_00": [0.5, 0.5]}


def test_a_row_count_mismatch_degrades_to_no_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(
        tracks=[(0.0, 2.0, "SPEAKER_00"), (3.0, 4.0, "SPEAKER_01")],
        embeddings=[[0.5, 0.5]],  # one row short
    )
    diarizer = PyannoteDiarizer(_config())
    _wire(monkeypatch, diarizer, FakePipelineClass(pipeline))

    output = diarizer.diarize(Path("meeting.wav"), cancel=CancelToken())

    assert output.embeddings is None
    assert len(output.turns) == 2  # the real artifact is untouched


def test_the_recording_is_decoded_by_faster_whisper_and_a_failure_is_audio_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pyannote is handed a waveform, never the file path: torchaudio on
    Windows cannot open the vault's mp4/m4a recordings, faster-whisper's
    bundled FFmpeg can. A file the decoder rejects is the recording's
    fault, classified as such."""
    import faster_whisper.audio as fw_audio

    def refuse(path: str, sampling_rate: int = 16000) -> None:
        raise RuntimeError(f"no such codec in {path}")

    monkeypatch.setattr(fw_audio, "decode_audio", refuse)
    diarizer = PyannoteDiarizer(_config())
    bad = tmp_path / "meeting.mp4"
    bad.write_bytes(b"not audio")

    with pytest.raises(ServiceError) as raised:
        diarizer._decode(bad)

    assert raised.value.kind is ErrorKind.AUDIO_DECODE
    assert "meeting.mp4" in raised.value.message
