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

import pytest

from transcription.errors import ErrorKind, ServiceError
from transcription.providers import local_whisper
from transcription.providers.base import CancelToken


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
) -> Any:
    """Build a class usable as a `WhisperModel` replacement, tracking calls."""

    state: dict[str, Any] = {"construct_calls": 0, "construct_kwargs": None}
    segs = segments if segments is not None else _default_segments()
    fw_info = info if info is not None else _FWInfo()

    class _Model:
        def __init__(self, **kwargs: Any) -> None:
            state["construct_calls"] += 1
            state["construct_kwargs"] = kwargs
            if raise_on_construct is not None:
                raise raise_on_construct

        def transcribe(self, path: str, **kwargs: Any) -> tuple[Any, _FWInfo]:
            state["transcribe_kwargs"] = kwargs
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
        _FakeBatchedPipeline.instances.append(self)

    def transcribe(self, path: str, **kwargs: Any) -> tuple[Any, Any]:
        self.transcribe_kwargs = kwargs
        forwarded = {key: value for key, value in kwargs.items() if key != "batch_size"}
        return self.model.transcribe(path, **forwarded)


@pytest.fixture(autouse=True)
def _reset_fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs against the fake pipeline class with a clean slate:
    the real `BatchedInferencePipeline` must never wrap a fake model."""
    _FakeBatchedPipeline.instances = []
    monkeypatch.setattr(local_whisper, "BatchedInferencePipeline", _FakeBatchedPipeline)


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

        def transcribe(self, path: str, **kwargs: Any) -> tuple[Any, _FWInfo]:
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

        def transcribe(self, path: str, **kwargs: Any) -> tuple[Any, _FWInfo]:
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
