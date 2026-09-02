# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Adapted from Vexa (Vexa-ai/vexa), Apache-2.0 -- origin:
# core/meetings/services/transcription/src/transcription/main.py:227-245 (model
# load) and :462-484 (segment mapping)
"""Local faster-whisper provider (FR-3, FR-6, FR-7, FR-12).

The model is loaded lazily -- never at import, never at provider
construction -- only on the first `transcribe` call, and cached on the
instance for the process's lifetime (FR-3 acceptance: "the second job's log
contains no model-load event").
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import ctranslate2  # type: ignore[import-untyped]
import numpy as np
from faster_whisper import (  # type: ignore[import-untyped]
    BatchedInferencePipeline,
    WhisperModel,
    decode_audio,
)
from faster_whisper.vad import (  # type: ignore[import-untyped]
    VadOptions,
    collect_chunks,
    get_speech_timestamps,
)

from transcription.errors import ErrorKind, ServiceError
from transcription.filters import apply_filters
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptResult
from transcription.segmentation import resegment


def _cuda_device_count() -> int:
    """The CUDA probe. Monkeypatched in tests -- never touches real hardware there."""
    try:
        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # pragma: no cover - defensive; ctranslate2 always exposes this
        return 0


def _resolve_device_and_compute_type(config: Any) -> tuple[str, str, bool]:
    """Resolve `device`/`compute_type`, honouring an explicit device over the
    probe. The third element records whether `device` was resolved from
    `"auto"` rather than named explicitly -- only an auto-resolved `cuda`
    is eligible for `_ensure_model`'s CPU fallback (E4): an operator who
    names `device: cuda` explicitly gets exactly what they asked for, never
    a silent downgrade.
    """
    device = getattr(config, "device", "auto") or "auto"
    compute_type = getattr(config, "compute_type", None)
    is_auto = device == "auto"

    if is_auto:
        device = "cuda" if _cuda_device_count() > 0 else "cpu"

    if not compute_type:
        compute_type = "float16" if device == "cuda" else "int8"

    return device, compute_type, is_auto


# Substrings of a raw exception message that indicate a CTranslate2/CUDA
# runtime-load failure (a missing/incompatible GPU driver or library), not a
# genuine decode failure -- both surface as a bare exception from
# `model.transcribe(...)` (FR-7, FR-8). Matched case-insensitively.
_MODEL_LOAD_ERROR_MARKERS = (
    "cuda",
    "cublas",
    "cudnn",
    "cudart",
    "nvrtc",
    "ctranslate2",
    "dll is not found",
    "cannot be loaded",
    "cannot open shared object file",
)


def _looks_like_cuda_runtime_failure(exc: Exception) -> bool:
    """Whether `exc`'s message names a CUDA/CTranslate2 runtime-load problem
    (a missing/unloadable cuBLAS/cuDNN, not a genuine decode failure) --
    shared between `_classify_transcribe_failure` (below) and
    `_ensure_model`'s CPU-fallback decision (E4)."""
    message = str(exc).lower()
    return any(marker in message for marker in _MODEL_LOAD_ERROR_MARKERS)


def _classify_transcribe_failure(exc: Exception) -> ErrorKind:
    """Distinguish a CUDA/CTranslate2 runtime-load failure from a genuine
    decode failure: a missing/unloadable CUDA runtime is `model_load`, never
    `audio_decode` -- mislabeling it points the operator at the audio file
    for what is actually an environment problem (FR-7, FR-8).
    """
    if _looks_like_cuda_runtime_failure(exc):
        return ErrorKind.MODEL_LOAD
    return ErrorKind.AUDIO_DECODE


def _decode_failure(exc: Exception, audio_path: Path, device: str) -> ServiceError:
    """The classified `ServiceError` for a failure raised out of the model --
    shared by the detection pass, the decode call and the segment iteration,
    which all fail the same two ways (FR-7, FR-8)."""
    if _classify_transcribe_failure(exc) is ErrorKind.MODEL_LOAD:
        return ServiceError(ErrorKind.MODEL_LOAD, f"model runtime failed on {device}: {exc}")
    return ServiceError(ErrorKind.AUDIO_DECODE, f"failed to decode {audio_path.name}: {exc}")


# The operator's language universe (F2 spec pinned "exactly two"; Turkish
# added 2026-09). Auto-detection chooses between these and nothing else, so
# a mis-detected outside language can never reach the decoder.
_DECODE_LANGUAGES = ("ru", "en", "tr")
# Used only if the model reports a distribution naming no target at all --
# the decode language must still be exactly one of `_DECODE_LANGUAGES`
# (FR-1).
_DEFAULT_LANGUAGE = "en"
# The probability recorded with that fallback: the model gave the chosen
# language no weight at all. FR-4 requires `language_probability` to be
# populated on *every* auto-detected run, so this branch reports "no evidence"
# as 0.0 rather than a null the F3 consumer would have to special-case.
_NO_EVIDENCE_PROBABILITY = 0.0

# `decode_audio` resamples to 16 kHz (faster-whisper's fixed model rate), and
# so does every VAD/feature path below.
_SAMPLE_RATE = 16_000
# How much of the recording the detection pass VAD-scans. Detection itself
# only ever consumes one ~30 s window, but finding the *speech* in a recording
# means running Silero first, and Silero over a whole file costs a measured
# ~5.7 s per hour of audio -- on top of the identical pass `transcribe` runs
# for the decode. Ten minutes bounds that at ~1 s (NFR-1's overhead budget)
# while still skipping any realistic lead-in of silence, hold music or
# keyboard noise before the first word.
_DETECTION_PREFIX_SEC = 600


def _constrain_language(all_language_probs: Any) -> tuple[str, float]:
    """Argmax over `_DECODE_LANGUAGES` alone, from faster-whisper's full
    ~100-language distribution (FR-1). Returns the chosen language and the
    probability it was chosen on (`0.0` when no target was reported)."""
    candidates = [
        (language, probability)
        for language, probability in (all_language_probs or [])
        if language in _DECODE_LANGUAGES
    ]
    if not candidates:
        return _DEFAULT_LANGUAGE, _NO_EVIDENCE_PROBABILITY
    return max(candidates, key=lambda item: item[1])


def _detection_audio(waveform: Any, vad_parameters: dict[str, Any]) -> Any:
    """The audio the language detector should look at: speech only.

    `WhisperModel.transcribe` -- the unconstrained auto path this feature
    replaced -- VAD-filters the waveform *before* extracting the features it
    detects the language from, so stock detection never saw silence. A bare
    `detect_language(audio=waveform)` does (its `vad_filter` defaults to
    `False`), which on a call that opens with silence, hold music or keyboard
    noise makes the ru/en choice -- and the probability FR-4 records -- from
    non-speech audio. So the same tightened VAD settings the decode pass uses
    are applied here first, over a bounded prefix (`_DETECTION_PREFIX_SEC`).

    `detect_language`'s own `vad_filter=` is deliberately not used: it hands
    `vad_parameters` straight to `get_speech_timestamps`, which reads
    attributes off it, so the dict shape `transcribe` accepts raises
    `AttributeError` there -- and it would scan the entire file.

    Falls back to the raw prefix when the VAD hears nothing at all: that is
    the pre-fix behaviour, and it beats handing the encoder an empty array
    (which raises inside faster-whisper), so a hard-to-hear recording still
    gets a language and a transcript.
    """
    prefix = waveform[: _DETECTION_PREFIX_SEC * _SAMPLE_RATE]
    speech_chunks = get_speech_timestamps(prefix, VadOptions(**vad_parameters))
    if not speech_chunks:
        return prefix
    audio_chunks, _ = collect_chunks(prefix, speech_chunks)
    return np.concatenate(audio_chunks, axis=0)


def _map_segment(segment: Any, new_id: int, *, include_words: bool) -> dict[str, Any]:
    """Vexa's `verbose_json` segment mapper, trimmed to the FR-6 fields."""
    mapped: dict[str, Any] = {
        "id": new_id,
        "start": segment.start,
        "end": segment.end,
        "text": segment.text,
        "avg_logprob": segment.avg_logprob,
        "no_speech_prob": segment.no_speech_prob,
        "compression_ratio": segment.compression_ratio,
    }
    if include_words:
        words = getattr(segment, "words", None) or []
        mapped["words"] = [
            {
                "word": word.word,
                "start": word.start,
                "end": word.end,
                "probability": word.probability,
            }
            for word in words
        ]
    return mapped


class LocalWhisperProvider:
    """Lazy-loaded local faster-whisper provider (FR-3)."""

    name = "local"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._model: Any = None
        self._pipeline: Any = None
        self._model_state: str = "unloaded"
        self._device, self._compute_type, self._device_is_auto = _resolve_device_and_compute_type(
            config
        )
        self._model_id: str = getattr(config, "model", "base") or "base"
        self._model_path: str | None = getattr(config, "model_path", "") or None
        self._word_timestamps: bool = bool(getattr(config, "word_timestamps", True))
        self._filter_hallucinations: bool = bool(getattr(config, "filter_hallucinations", True))
        self._batch_size: int = int(getattr(config, "batch_size", 8) or 0)
        self._vad_min_silence_ms: int = int(getattr(config, "vad_min_silence_ms", 500))
        self._resegment_gap_sec: float = float(getattr(config, "resegment_gap_sec", 0.6))

    def describe(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            model=self._model_id,
            device=self._device,
            compute_type=self._compute_type,
            model_state=self._model_state,  # type: ignore[arg-type]
        )

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model

        self._model_state = "loading"
        # `self._model_path` (`config.model_path`/`TRANSCRIBER_MODEL_PATH`)
        # is the literal, already-model-specific snapshot directory -- not a
        # parent "models" directory (`docs/config-contract.md`: the app
        # derives it via `app_paths::model_dir()`, which already names the
        # exact `<app folder>\models\faster-whisper-large-v3\` directory;
        # `model_download.py` writes its flat snapshot straight into
        # whatever `models_dir` it is given, for the same reason). Passing
        # that directory as `model_size_or_path` hits
        # `faster_whisper.WhisperModel.__init__`'s `os.path.isdir(...)`
        # branch, which loads it directly and bypasses the Hugging Face Hub
        # cache convention entirely -- T14's real-machine verification
        # (`docs/verification-installer.md` "Blocker 2") found the previous
        # `model_size_or_path=self._model_id` (the bare model name) routed
        # through that hub-cache mechanism instead, which
        # `model_download.py`'s on-disk layout never matched.
        # `download_root`/`local_files_only` are still passed so the
        # *fallback* branch (the directory not existing yet -- a fresh
        # install with no model downloaded) still refuses to fetch anything
        # over the network (FR-3 acceptance: "loads the model with no
        # network access"), instead surfacing as `model_load` below.
        model_size_or_path = self._model_path or self._model_id

        def _construct_kwargs(device: str, compute_type: str) -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "model_size_or_path": model_size_or_path,
                "device": device,
                "compute_type": compute_type,
                "download_root": self._model_path,
                "local_files_only": True,
            }
            if device == "cpu":
                kwargs["cpu_threads"] = getattr(self.config, "cpu_threads", 4)
            return kwargs

        try:
            model = WhisperModel(**_construct_kwargs(self._device, self._compute_type))
        except Exception as exc:
            # E4: an `auto`-resolved `cuda` that turns out not to be
            # actually loadable (missing driver, or the first-run CUDA
            # runtime download never completed) is the documented
            # best-effort-CPU-fallback case (spec "Out of scope: CPU-only
            # optimization" -- "CPU fallback is best-effort, not a tested or
            # optimized target"), not a broken install. An *explicit*
            # `device: cuda` in config is a deliberate operator override, so
            # it never falls back -- honouring an explicit choice literally,
            # the same rule `_resolve_device_and_compute_type` already
            # applies to the probe.
            if (
                self._device == "cuda"
                and self._device_is_auto
                and _looks_like_cuda_runtime_failure(exc)
            ):
                fallback_compute_type = "int8"
                try:
                    model = WhisperModel(**_construct_kwargs("cpu", fallback_compute_type))
                except Exception as cpu_exc:
                    self._model_state = "unloaded"
                    raise ServiceError(
                        ErrorKind.MODEL_LOAD,
                        f"failed to load model {self._model_id!r} from "
                        f"{self._model_path!r} on cpu fallback (after cuda failed: "
                        f"{exc}): {cpu_exc}",
                    ) from cpu_exc
                self._device = "cpu"
                self._compute_type = fallback_compute_type
            else:
                self._model_state = "unloaded"
                raise ServiceError(
                    ErrorKind.MODEL_LOAD,
                    f"failed to load model {self._model_id!r} from {self._model_path!r}: {exc}",
                ) from exc

        self._model = model
        # Batched decoding cuts audio into VAD-derived chunks and decodes
        # them in parallel -- a large throughput win on CUDA, and the chunks
        # are naturally utterance-shaped. CPU (including the E4 fallback,
        # which rewrote `self._device` above) keeps the sequential path:
        # batching multiplies peak memory, and CPU is best-effort territory.
        if self._device == "cuda" and self._batch_size > 1:
            self._pipeline = BatchedInferencePipeline(model=model)
        self._model_state = "loaded"
        return model

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        cancel.raise_if_cancelled()
        model = self._ensure_model()

        # The tightened VAD: real pauses actually terminate segments instead
        # of being bridged. Shared with the detection pass below so both agree
        # on what counts as speech.
        vad_parameters: dict[str, Any] = {
            "min_silence_duration_ms": self._vad_min_silence_ms,
            "speech_pad_ms": 400,
        }

        # Auto means "pick between Russian and English", never "let the model
        # roam its full language set" -- unconstrained detection is what
        # transcribed an English meeting in Russian (FR-1). `detect_language`
        # takes a waveform rather than a path, so the file is decoded here and
        # the *same* waveform is handed to the decode call below: one file
        # read, one extra encoder window, no second decode (NFR-1). Detection
        # runs on the VAD-filtered speech in that waveform, never on raw
        # silence (`_detection_audio`); the decode call still gets the
        # unfiltered waveform, because it runs its own VAD and needs the
        # original timeline to map segment timestamps back. An explicit
        # language skips all of it and reaches faster-whisper exactly as
        # before (FR-2).
        audio_input: Any = str(audio_path)
        decode_language = language
        detected_probability: float | None = None
        if decode_language is None:
            try:
                audio_input = decode_audio(str(audio_path))
                _, _, all_language_probs = model.detect_language(
                    audio=_detection_audio(audio_input, vad_parameters)
                )
            except Exception as exc:
                raise _decode_failure(exc, audio_path, self._device) from exc
            decode_language, detected_probability = _constrain_language(all_language_probs)

        # `condition_on_previous_text=False`: each ~30 s window is decoded
        # without being biased by the previous one -- the bias is what
        # produces run-on segments and repetition-loop hallucinations on
        # conversational audio.
        decode_kwargs: dict[str, Any] = {
            "language": decode_language,
            "beam_size": 5,
            "vad_filter": True,
            "vad_parameters": vad_parameters,
            "condition_on_previous_text": False,
            "word_timestamps": self._word_timestamps,
        }

        try:
            if self._pipeline is not None:
                segments_iter, info = self._pipeline.transcribe(
                    audio_input, batch_size=self._batch_size, **decode_kwargs
                )
            else:
                segments_iter, info = model.transcribe(audio_input, **decode_kwargs)
        except Exception as exc:
            raise _decode_failure(exc, audio_path, self._device) from exc

        duration = getattr(info, "duration", 0.0) or 0.0
        raw_segments: list[dict[str, Any]] = []

        iterator: Iterator[Any] = iter(segments_iter)
        while True:
            cancel.raise_if_cancelled()
            try:
                segment = next(iterator)
            except StopIteration:
                break
            except ServiceError:
                raise
            except Exception as exc:
                raise _decode_failure(exc, audio_path, self._device) from exc

            mapped = _map_segment(segment, len(raw_segments), include_words=self._word_timestamps)
            raw_segments.append(mapped)

            fraction = min(segment.end / duration, 1.0) if duration > 0 else 1.0
            on_progress(fraction)

        kept, n_filtered = apply_filters(raw_segments, enabled=self._filter_hallucinations)
        # Filter first (confidence stats belong to the segment they were
        # measured on), then split the survivors into utterance-sized pieces.
        if self._word_timestamps:
            segments_out: list[dict[str, Any]] = resegment(kept, gap_sec=self._resegment_gap_sec)
        else:
            segments_out = [dict(segment) for segment in kept]
        text = "".join(str(segment["text"]) for segment in segments_out)

        # The language actually decoded in, not what `info` echoes back: on an
        # auto run that is the constrained choice above, on a forced run the
        # requested one (FR-4). `info.language` is only trustworthy as a
        # probability source on a forced run.
        language_out = decode_language
        language_probability = (
            detected_probability
            if language is None
            else getattr(info, "language_probability", None)
        )

        return TranscriptResult(
            segments=segments_out,
            text=text,
            language=language_out,
            language_probability=language_probability,
            duration_sec=duration,
            model=self._model_id,
            device=self._device,
            compute_type=self._compute_type,
            cost_usd=None,
            currency=None,
            filtered_segment_count=n_filtered,
        )
