"""Tests for the local faster-whisper provider (FR-3, FR-6, FR-7, FR-12).

A `FakeWhisperModel` stands in for `faster_whisper.WhisperModel` -- no model
is ever loaded, no GPU is touched and no network access happens (FR-15).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from transcription.errors import ErrorKind, ServiceError
from transcription.providers import local_whisper
from transcription.providers.base import CancelToken

# What `WhisperModel.detect_language` reports by default in these tests: an
# English recording, with the full ~100-language distribution abbreviated to
# the handful that matter here.
_DEFAULT_LANGUAGE_PROBS: list[tuple[str, float]] = [("en", 0.9), ("ru", 0.05), ("uk", 0.01)]


def _config(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "model": "large-v3",
        "model_path": "/models",
        "device": "cpu",
        "compute_type": None,
        "filter_hallucinations": True,
        "word_timestamps": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@dataclass(frozen=True, kw_only=True)
class _FWWord:
    word: str
    start: float
    end: float
    probability: float


@dataclass(frozen=True, kw_only=True)
class _FWSegment:
    start: float
    end: float
    text: str
    avg_logprob: float = -0.1
    no_speech_prob: float = 0.05
    compression_ratio: float = 1.0
    words: list[_FWWord] | None = None


@dataclass(frozen=True, kw_only=True)
class _FWInfo:
    duration: float = 1.0
    language: str = "en"
    language_probability: float = 0.99


def _default_segments() -> list[_FWSegment]:
    return [
        _FWSegment(start=0.0, end=0.3, text="hello "),
        _FWSegment(start=0.3, end=0.6, text="there "),
        _FWSegment(start=0.6, end=1.0, text="world"),
    ]


class _CountingSegments:
    """Iterable that records how many segments were actually pulled."""

    def __init__(self, segments: list[_FWSegment]) -> None:
        self._segments = segments
        self.next_calls = 0

    def __iter__(self) -> Iterator[_FWSegment]:
        for segment in self._segments:
            self.next_calls += 1
            yield segment


def make_fake_model_factory(
    *,
    segments: list[_FWSegment] | None = None,
    info: _FWInfo | None = None,
    raise_on_construct: Exception | None = None,
    raise_on_transcribe: Exception | None = None,
    counting: bool = False,
    language_probs: list[tuple[str, float]] | None = None,
) -> Any:
    """Build a class usable as a `WhisperModel` replacement, tracking calls."""

    state: dict[str, Any] = {
        "construct_calls": 0,
        "construct_kwargs": None,
        "detect_language_calls": 0,
        "detect_language_kwargs": None,
        "transcribe_audio": None,
    }
    segs = segments if segments is not None else _default_segments()
    fw_info = info if info is not None else _FWInfo()
    probs = language_probs if language_probs is not None else _DEFAULT_LANGUAGE_PROBS

    class _Model:
        def __init__(self, **kwargs: Any) -> None:
            state["construct_calls"] += 1
            state["construct_kwargs"] = kwargs
            if raise_on_construct is not None:
                raise raise_on_construct

        def detect_language(self, **kwargs: Any) -> tuple[str, float, list[tuple[str, float]]]:
            """Mirrors `WhisperModel.detect_language`'s return contract in
            faster-whisper 1.2.1: `(language, language_probability,
            all_language_probs)`, the last one covering every language the
            model knows -- not just the two this feature cares about."""
            state["detect_language_calls"] += 1
            state["detect_language_kwargs"] = kwargs
            top_language, top_probability = max(probs, key=lambda item: item[1])
            return top_language, top_probability, list(probs)

        def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, _FWInfo]:
            state["transcribe_kwargs"] = kwargs
            state["transcribe_audio"] = audio
            if raise_on_transcribe is not None:
                raise raise_on_transcribe
            if counting:
                return _CountingSegments(segs), fw_info
            return iter(segs), fw_info

    _Model.state = state  # type: ignore[attr-defined]
    return _Model


class _FakeBatchedPipeline:
    """Stands in for `faster_whisper.BatchedInferencePipeline`: records the
    wrapped model and forwards `transcribe` to it, the same delegation shape
    as the real class."""

    instances: list[_FakeBatchedPipeline] = []

    def __init__(self, *, model: Any) -> None:
        self.model = model
        self.transcribe_kwargs: dict[str, Any] | None = None
        self.transcribe_audio: Any = None
        _FakeBatchedPipeline.instances.append(self)

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, Any]:
        self.transcribe_kwargs = kwargs
        self.transcribe_audio = audio
        forwarded = {key: value for key, value in kwargs.items() if key != "batch_size"}
        return self.model.transcribe(audio, **forwarded)


@pytest.fixture(autouse=True)
def _reset_fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs against the fake pipeline class with a clean slate:
    the real `BatchedInferencePipeline` must never wrap a fake model."""
    _FakeBatchedPipeline.instances = []
    monkeypatch.setattr(local_whisper, "BatchedInferencePipeline", _FakeBatchedPipeline)


@pytest.fixture(autouse=True)
def fake_decode_audio(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """`decode_audio` is the only thing in this provider that reads the file
    off disk; tests hand back a fixed waveform instead so no fixture audio is
    needed. Returns the list of sources it was asked to decode."""
    calls: list[Any] = []
    waveform = np.zeros(16_000, dtype=np.float32)

    def _decode(source: Any, **kwargs: Any) -> Any:
        calls.append(source)
        return waveform

    monkeypatch.setattr(local_whisper, "decode_audio", _decode, raising=False)
    return calls


@pytest.fixture(autouse=True)
def fake_vad(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Silero stands in for the real VAD model: unit tests never load it.

    Records the waveform it was asked to scan and the options it got, and by
    default reports the whole waveform as speech. A test can plant its own
    `chunks` (including `[]`, "no speech anywhere") before transcribing.
    """
    state: dict[str, Any] = {"audio": None, "options": None, "calls": 0, "chunks": None}

    def _get_speech_timestamps(audio: Any, vad_options: Any = None, **kwargs: Any) -> list[Any]:
        state["calls"] += 1
        state["audio"] = audio
        state["options"] = vad_options
        planted = state["chunks"]
        if planted is not None:
            return list(planted)
        return [{"start": 0, "end": len(audio)}]

    monkeypatch.setattr(local_whisper, "get_speech_timestamps", _get_speech_timestamps)
    return state


def test_model_not_constructed_at_provider_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    local_whisper.LocalWhisperProvider(_config())

    assert model_cls.state["construct_calls"] == 0


def test_model_constructed_once_across_two_transcribe_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())

    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )
    provider.transcribe(
        Path("b.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert model_cls.state["construct_calls"] == 1


def test_describe_model_state_walks_unloaded_loading_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = local_whisper.LocalWhisperProvider(_config())
    observed: list[str] = []

    class _ObservingModel:
        def __init__(self, **kwargs: Any) -> None:
            observed.append(provider.describe().model_state)

        def detect_language(self, **kwargs: Any) -> tuple[str, float, list[tuple[str, float]]]:
            return "en", 0.9, list(_DEFAULT_LANGUAGE_PROBS)

        def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, _FWInfo]:
            return iter(_default_segments()), _FWInfo()

    monkeypatch.setattr(local_whisper, "WhisperModel", _ObservingModel)

    assert provider.describe().model_state == "unloaded"

    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert observed == ["loading"]
    assert provider.describe().model_state == "loaded"


def test_device_auto_resolves_to_cuda_with_float16_when_gpu_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_whisper, "_cuda_device_count", lambda: 1)

    provider = local_whisper.LocalWhisperProvider(_config(device="auto"))

    info = provider.describe()
    assert info.device == "cuda"
    assert info.compute_type == "float16"


def test_device_auto_resolves_to_cpu_with_int8_when_no_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_whisper, "_cuda_device_count", lambda: 0)

    provider = local_whisper.LocalWhisperProvider(_config(device="auto"))

    info = provider.describe()
    assert info.device == "cpu"
    assert info.compute_type == "int8"


def test_explicit_device_wins_over_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_whisper, "_cuda_device_count", lambda: 1)

    provider = local_whisper.LocalWhisperProvider(_config(device="cpu"))

    assert provider.describe().device == "cpu"
    assert provider.describe().compute_type == "int8"


def test_constructor_kwargs_are_exactly_the_documented_set_on_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config(device="cpu", model_path="/my/models"))
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    kwargs = model_cls.state["construct_kwargs"]
    assert set(kwargs) == {
        "model_size_or_path",
        "device",
        "compute_type",
        "download_root",
        "cpu_threads",
        "local_files_only",
    }
    # The literal, already-model-specific `model_path` itself, not the bare
    # model id (Defect 2 fix): `config.model_path`/`TRANSCRIBER_MODEL_PATH`
    # already names the exact snapshot directory
    # (`docs/config-contract.md`), and `faster_whisper` special-cases a
    # literal local directory via `os.path.isdir(...)`, bypassing the hub
    # cache convention entirely.
    assert kwargs["model_size_or_path"] == "/my/models"
    assert kwargs["device"] == "cpu"
    assert kwargs["download_root"] == "/my/models"
    assert kwargs["local_files_only"] is True


def test_constructor_kwargs_omit_cpu_threads_on_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)
    monkeypatch.setattr(local_whisper, "_cuda_device_count", lambda: 1)

    provider = local_whisper.LocalWhisperProvider(_config(device="auto"))
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    kwargs = model_cls.state["construct_kwargs"]
    assert set(kwargs) == {
        "model_size_or_path",
        "device",
        "compute_type",
        "download_root",
        "local_files_only",
    }


def test_whisper_model_constructor_failure_maps_to_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory(raise_on_construct=RuntimeError("disk full"))
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
        )

    assert exc_info.value.kind == ErrorKind.MODEL_LOAD
    assert "disk full" in exc_info.value.message


def test_auto_resolved_cuda_construction_failure_falls_back_to_cpu_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E4: `ctranslate2.get_cuda_device_count() > 0` succeeds even when
    cuBLAS/cuDNN cannot actually be loaded (e.g. the first-run CUDA runtime
    download never ran) -- an `auto`-resolved `cuda` whose model
    construction fails with a CUDA/CTranslate2 runtime-load error must fall
    back to CPU and still produce a usable model, rather than hard-failing
    `model_load` on a machine the spec documents CPU as a best-effort
    fallback for."""
    monkeypatch.setattr(local_whisper, "_cuda_device_count", lambda: 1)
    calls: list[dict[str, Any]] = []

    class _FallbackModel:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)
            if kwargs["device"] == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")

        def detect_language(self, **kwargs: Any) -> tuple[str, float, list[tuple[str, float]]]:
            return "en", 0.9, list(_DEFAULT_LANGUAGE_PROBS)

        def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, _FWInfo]:
            return iter(_default_segments()), _FWInfo()

    monkeypatch.setattr(local_whisper, "WhisperModel", _FallbackModel)

    provider = local_whisper.LocalWhisperProvider(_config(device="auto"))
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.text == "hello there world"
    assert len(calls) == 2
    assert calls[0]["device"] == "cuda"
    assert calls[1]["device"] == "cpu"
    assert calls[1]["compute_type"] == "int8"
    info = provider.describe()
    assert info.device == "cpu"
    assert info.compute_type == "int8"
    assert info.model_state == "loaded"


def test_explicit_cuda_device_construction_failure_does_not_fall_back_to_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E4 (negative case): an operator who names `device: cuda` explicitly
    gets exactly what they asked for -- a load failure there must still
    raise `model_load`, never silently downgrade to CPU."""
    calls: list[dict[str, Any]] = []

    class _AlwaysCudaFailModel:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")

    monkeypatch.setattr(local_whisper, "WhisperModel", _AlwaysCudaFailModel)

    provider = local_whisper.LocalWhisperProvider(_config(device="cuda"))

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
        )

    assert exc_info.value.kind == ErrorKind.MODEL_LOAD
    assert len(calls) == 1, "an explicit device must never trigger a second, cpu, attempt"
    assert provider.describe().device == "cuda"


def test_transcribe_decode_failure_maps_to_audio_decode_naming_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory(
        raise_on_transcribe=RuntimeError("Invalid data found when processing input")
    )
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            Path("bad.mp4"), language=None, on_progress=lambda _: None, cancel=CancelToken()
        )

    assert exc_info.value.kind == ErrorKind.AUDIO_DECODE
    assert "bad.mp4" in exc_info.value.message
    assert "Invalid data found when processing input" in exc_info.value.message


def test_cuda_runtime_load_failure_during_transcribe_maps_to_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E8: a missing/unloadable CUDA runtime must be `model_load`, not
    `audio_decode` -- it is an environment problem, not a bad audio file."""
    model_cls = make_fake_model_factory(
        raise_on_transcribe=RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
    )
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
        )

    assert exc_info.value.kind == ErrorKind.MODEL_LOAD


def test_genuine_decode_failure_still_maps_to_audio_decode_not_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E8 (negative case): a real format/decode error must stay `audio_decode`."""
    model_cls = make_fake_model_factory(
        raise_on_transcribe=RuntimeError("Invalid data found when processing input")
    )
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            Path("bad.mp4"), language=None, on_progress=lambda _: None, cancel=CancelToken()
        )

    assert exc_info.value.kind == ErrorKind.AUDIO_DECODE


def test_constructor_always_requests_local_files_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """E5: the model constructor must never be allowed to fall back to a
    network download."""
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert model_cls.state["construct_kwargs"]["local_files_only"] is True


def test_segment_mapping_has_documented_fields_and_renumbered_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert [seg["id"] for seg in result.segments] == [0, 1, 2]
    first = result.segments[0]
    assert set(first) == {
        "id",
        "start",
        "end",
        "text",
        "avg_logprob",
        "no_speech_prob",
        "compression_ratio",
    }
    assert "words" not in first


def test_segment_mapping_includes_words_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [
        _FWSegment(
            start=0.0,
            end=1.0,
            text="hi",
            words=[_FWWord(word="hi", start=0.0, end=1.0, probability=0.9)],
        )
    ]
    model_cls = make_fake_model_factory(segments=segments)
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config(word_timestamps=True))
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert "words" in result.segments[0]
    assert result.segments[0]["words"][0]["word"] == "hi"


def test_on_progress_is_non_decreasing_and_ends_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    progress: list[float] = []
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=progress.append, cancel=CancelToken()
    )

    assert progress == sorted(progress)
    assert progress[-1] == 1.0


def test_all_silence_result_yields_empty_text_when_filter_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    silent_segments = [
        _FWSegment(start=0.0, end=1.0, text="[music]", no_speech_prob=0.9, avg_logprob=-1.2),
        _FWSegment(start=1.0, end=2.0, text="[applause]", no_speech_prob=0.95, avg_logprob=-1.5),
    ]
    model_cls = make_fake_model_factory(segments=silent_segments, info=_FWInfo(duration=2.0))
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config(filter_hallucinations=True))
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.text == ""
    assert result.segments == []
    assert result.filtered_segment_count == 2


def test_all_silence_result_yields_raw_text_when_filter_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    silent_segments = [
        _FWSegment(start=0.0, end=1.0, text="[music]", no_speech_prob=0.9, avg_logprob=-1.2),
    ]
    model_cls = make_fake_model_factory(segments=silent_segments, info=_FWInfo(duration=1.0))
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config(filter_hallucinations=False))
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.text == "[music]"
    assert result.filtered_segment_count == 0


def test_cancel_after_second_segment_stops_iteration_without_exhausting_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory(counting=True)
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    cancel = CancelToken()
    progress: list[float] = []

    def on_progress(fraction: float) -> None:
        progress.append(fraction)
        if len(progress) == 2:
            cancel.set()

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(Path("a.wav"), language=None, on_progress=on_progress, cancel=cancel)

    assert exc_info.value.kind == ErrorKind.CANCELLED
    assert len(progress) == 2


def test_decode_disables_context_conditioning_and_tunes_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quality tuning: each window is decoded without previous-text bias
    (run-on / repetition-loop prevention) and VAD breaks segments at real
    conversational pauses."""
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    kwargs = model_cls.state["transcribe_kwargs"]
    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["vad_filter"] is True
    assert kwargs["vad_parameters"]["min_silence_duration_ms"] == 500
    assert kwargs["vad_parameters"]["speech_pad_ms"] == 400
    # CPU keeps the sequential path: no batch_size leaks into the model call.
    assert "batch_size" not in kwargs


def test_cuda_uses_batched_pipeline_with_configured_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)
    monkeypatch.setattr(local_whisper, "_cuda_device_count", lambda: 1)

    provider = local_whisper.LocalWhisperProvider(_config(device="auto", batch_size=4))
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert len(_FakeBatchedPipeline.instances) == 1
    pipeline = _FakeBatchedPipeline.instances[0]
    assert pipeline.transcribe_kwargs is not None
    assert pipeline.transcribe_kwargs["batch_size"] == 4
    assert pipeline.transcribe_kwargs["condition_on_previous_text"] is False


def test_cpu_never_constructs_batched_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config(device="cpu", batch_size=8))
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert _FakeBatchedPipeline.instances == []


def test_batch_size_of_one_keeps_sequential_path_on_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)
    monkeypatch.setattr(local_whisper, "_cuda_device_count", lambda: 1)

    provider = local_whisper.LocalWhisperProvider(_config(device="auto", batch_size=1))
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert _FakeBatchedPipeline.instances == []


def test_word_timestamps_resegment_multi_utterance_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline fix: one Whisper segment spanning several utterances
    comes back as one segment per utterance."""
    words = [
        _FWWord(word=" Привет.", start=0.0, end=0.4, probability=0.9),
        _FWWord(word=" Как", start=0.5, end=0.7, probability=0.9),
        _FWWord(word=" дела?", start=0.7, end=1.0, probability=0.9),
        _FWWord(word=" Хорошо", start=2.0, end=2.5, probability=0.9),
    ]
    segments = [_FWSegment(start=0.0, end=2.5, text=" Привет. Как дела? Хорошо", words=list(words))]
    model_cls = make_fake_model_factory(segments=segments, info=_FWInfo(duration=2.5))
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config(word_timestamps=True))
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert [seg["text"] for seg in result.segments] == [" Привет.", " Как дела?", " Хорошо"]
    assert [seg["id"] for seg in result.segments] == [0, 1, 2]
    assert result.text == " Привет. Как дела? Хорошо"


def test_auto_detection_picks_the_stronger_of_ru_en_even_when_another_language_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-1: unconstrained detection is what transcribed an English meeting in
    Russian. With `uk` outranking both targets, the decode language is still
    the higher of `ru`/`en` -- never a third language."""
    model_cls = make_fake_model_factory(
        language_probs=[("uk", 0.7), ("ru", 0.05), ("en", 0.2), ("de", 0.02)]
    )
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert model_cls.state["transcribe_kwargs"]["language"] == "en"


def test_auto_detection_constraint_applies_on_the_batched_pipeline_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-1: the constraint is applied before the decode call, so the batched
    (CUDA) path is forced exactly like the sequential one."""
    model_cls = make_fake_model_factory(language_probs=[("uk", 0.7), ("ru", 0.2), ("en", 0.05)])
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)
    monkeypatch.setattr(local_whisper, "_cuda_device_count", lambda: 1)

    provider = local_whisper.LocalWhisperProvider(_config(device="auto", batch_size=4))
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    pipeline = _FakeBatchedPipeline.instances[0]
    assert pipeline.transcribe_kwargs is not None
    assert pipeline.transcribe_kwargs["language"] == "ru"


def test_auto_detection_falls_back_to_english_when_neither_target_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-1: the decode language is always exactly `ru` or `en`, even if the
    model reports a distribution containing neither. FR-4 ("`language_-
    probability` is populated on auto-detected runs") holds unconditionally:
    the fallback records `0.0` -- no evidence for the chosen language -- rather
    than a null the downstream F3 consumer would have to special-case."""
    model_cls = make_fake_model_factory(language_probs=[("uk", 0.7), ("de", 0.3)])
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert model_cls.state["transcribe_kwargs"]["language"] == "en"
    assert result.language == "en"
    assert result.language_probability == 0.0


@pytest.mark.parametrize("requested", ["en", "ru"])
def test_explicit_language_is_passed_through_without_any_detection(
    monkeypatch: pytest.MonkeyPatch, requested: str
) -> None:
    """FR-2: an explicit language decodes in that language regardless of what
    detection would have said -- and costs no detection pass at all."""
    model_cls = make_fake_model_factory(language_probs=[("uk", 0.9), ("en", 0.05), ("ru", 0.04)])
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    result = provider.transcribe(
        Path("a.wav"), language=requested, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert model_cls.state["transcribe_kwargs"]["language"] == requested
    assert model_cls.state["detect_language_calls"] == 0
    assert result.language == requested


def test_forced_run_reports_the_requested_language_and_model_probability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-4: on a forced run the result names the language actually decoded --
    not whatever `info.language` happens to echo -- with the model-reported
    probability."""
    model_cls = make_fake_model_factory(info=_FWInfo(language="en", language_probability=0.42))
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    result = provider.transcribe(
        Path("a.wav"), language="ru", on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.language == "ru"
    assert result.language_probability == 0.42


def test_auto_run_reports_the_constrained_language_and_its_probability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-4: on an auto run the result carries the constrained choice and the
    probability that choice was made on."""
    model_cls = make_fake_model_factory(
        info=_FWInfo(language="uk", language_probability=0.99),
        language_probs=[("uk", 0.7), ("ru", 0.25), ("en", 0.03)],
    )
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.language == "ru"
    assert result.language_probability == 0.25


def test_auto_transcribe_detects_language_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NFR-1: one detection window per job, not one per segment or per call."""
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert model_cls.state["detect_language_calls"] == 1


def test_auto_run_decodes_the_audio_once_and_reuses_it_for_the_decode_pass(
    monkeypatch: pytest.MonkeyPatch, fake_decode_audio: list[Any]
) -> None:
    """NFR-1: detection needs a waveform (`detect_language` takes no path), so
    the file is decoded once up front and that same waveform is handed to the
    transcribe call -- never decoded a second time."""
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert len(fake_decode_audio) == 1
    # The decode pass gets the decoded waveform itself; detection gets speech
    # carved out of that same array (see the VAD tests below) -- never a
    # second `decode_audio` call.
    waveform = local_whisper.decode_audio("probe")  # the fixture's fixed waveform
    assert model_cls.state["transcribe_audio"] is waveform
    assert model_cls.state["detect_language_kwargs"]["audio"] is not None


def test_auto_detection_detects_on_vad_filtered_speech(
    monkeypatch: pytest.MonkeyPatch, fake_vad: dict[str, Any]
) -> None:
    """E1: faster-whisper's own auto path VAD-filters the waveform *before*
    language detection (`WhisperModel.transcribe` runs `get_speech_timestamps`
    and detects on the filtered features). The constrained detection must be
    at least as accurate: a recording that opens with silence, hold music or
    keyboard noise must not have its ru/en choice made from that non-speech
    audio."""
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)
    # Speech starts a quarter of the way into the (1 s) waveform.
    fake_vad["chunks"] = [{"start": 4_000, "end": 12_000}]

    provider = local_whisper.LocalWhisperProvider(_config())
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    scanned = fake_vad["audio"]
    detected = model_cls.state["detect_language_kwargs"]["audio"]
    assert fake_vad["calls"] == 1
    assert len(detected) == 8_000, "detection must see the speech chunk, not the raw waveform"
    assert np.array_equal(detected, scanned[4_000:12_000])
    # The decode pass still receives the *unfiltered* waveform: `transcribe`
    # runs its own VAD and needs the original timeline to map timestamps back.
    assert len(model_cls.state["transcribe_audio"]) == 16_000


def test_detection_vad_uses_the_same_tightened_parameters_as_the_decode_pass(
    monkeypatch: pytest.MonkeyPatch, fake_vad: dict[str, Any]
) -> None:
    """E1: detection and decode must agree on what counts as speech --
    and `get_speech_timestamps` needs a `VadOptions`, not the raw dict
    (`transcribe` converts dicts, the detection path does not)."""
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config(vad_min_silence_ms=700))
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    options = fake_vad["options"]
    assert isinstance(options, local_whisper.VadOptions)
    decode_kwargs = model_cls.state["transcribe_kwargs"]["vad_parameters"]
    assert options.min_silence_duration_ms == decode_kwargs["min_silence_duration_ms"] == 700
    assert options.speech_pad_ms == decode_kwargs["speech_pad_ms"] == 400


def test_detection_falls_back_to_raw_audio_when_vad_finds_no_speech(
    monkeypatch: pytest.MonkeyPatch, fake_vad: dict[str, Any]
) -> None:
    """A file the VAD hears nothing in still gets a language and a decode --
    detecting on the raw window is exactly the pre-fix behaviour, and it beats
    handing the encoder an empty array (which raises inside faster-whisper)."""
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)
    fake_vad["chunks"] = []

    provider = local_whisper.LocalWhisperProvider(_config())
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    detected = model_cls.state["detect_language_kwargs"]["audio"]
    assert len(detected) == 16_000
    assert result.language == "en"
    assert model_cls.state["transcribe_kwargs"]["language"] == "en"


def test_detection_vad_scans_only_a_bounded_prefix_of_a_long_recording(
    monkeypatch: pytest.MonkeyPatch, fake_vad: dict[str, Any]
) -> None:
    """NFR-1: the detection pass may not turn into a second full-file Silero
    sweep on top of the one the decode pass already runs (~5.7 s per hour of
    audio, measured). It scans a bounded prefix -- long enough to skip a
    lead-in of silence or hold music, short enough to stay inside the
    overhead budget."""
    prefix_samples = local_whisper._DETECTION_PREFIX_SEC * local_whisper._SAMPLE_RATE
    long_waveform = np.zeros(prefix_samples + 16_000, dtype=np.float32)
    monkeypatch.setattr(
        local_whisper, "decode_audio", lambda *args, **kwargs: long_waveform, raising=False
    )
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert len(fake_vad["audio"]) == prefix_samples
    assert local_whisper._DETECTION_PREFIX_SEC <= 600
    # The decode pass is untouched by the detection budget: it still gets the
    # whole recording.
    assert model_cls.state["transcribe_audio"] is long_waveform


def test_forced_run_hands_the_path_straight_to_the_model(
    monkeypatch: pytest.MonkeyPatch, fake_decode_audio: list[Any]
) -> None:
    """FR-2: with no detection to run there is nothing to decode up front --
    faster-whisper reads the file itself, exactly as before this feature."""
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    provider.transcribe(
        Path("a.wav"), language="en", on_progress=lambda _: None, cancel=CancelToken()
    )

    assert fake_decode_audio == []
    assert model_cls.state["transcribe_audio"] == str(Path("a.wav"))


def test_detection_failure_maps_to_a_classified_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detection pass that blows up is classified like any other decode
    failure -- never a raw exception escaping the provider."""

    decode_calls: list[Any] = []

    class _FailingDetectModel:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def detect_language(self, **kwargs: Any) -> tuple[str, float, list[tuple[str, float]]]:
            raise RuntimeError("Invalid data found when processing input")

        def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, _FWInfo]:
            decode_calls.append(audio)
            return iter(_default_segments()), _FWInfo()

    monkeypatch.setattr(local_whisper, "WhisperModel", _FailingDetectModel)

    provider = local_whisper.LocalWhisperProvider(_config())

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            Path("bad.mp4"), language=None, on_progress=lambda _: None, cancel=CancelToken()
        )

    assert exc_info.value.kind == ErrorKind.AUDIO_DECODE
    assert "bad.mp4" in exc_info.value.message
    assert decode_calls == [], "decode must not run once detection has failed"


def test_cost_usd_and_currency_are_none_for_local_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_cls = make_fake_model_factory()
    monkeypatch.setattr(local_whisper, "WhisperModel", model_cls)

    provider = local_whisper.LocalWhisperProvider(_config())
    result = provider.transcribe(
        Path("a.wav"), language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.cost_usd is None
    assert result.currency is None
