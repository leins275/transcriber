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
from faster_whisper import WhisperModel  # type: ignore[import-untyped]

from transcription.errors import ErrorKind, ServiceError
from transcription.filters import apply_filters
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptResult


def _cuda_device_count() -> int:
    """The CUDA probe. Monkeypatched in tests -- never touches real hardware there."""
    try:
        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # pragma: no cover - defensive; ctranslate2 always exposes this
        return 0


def _resolve_device_and_compute_type(config: Any) -> tuple[str, str]:
    """Resolve `device`/`compute_type`, honouring an explicit device over the probe."""
    device = getattr(config, "device", "auto") or "auto"
    compute_type = getattr(config, "compute_type", None)

    if device == "auto":
        device = "cuda" if _cuda_device_count() > 0 else "cpu"

    if not compute_type:
        compute_type = "float16" if device == "cuda" else "int8"

    return device, compute_type


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


def _classify_transcribe_failure(exc: Exception) -> ErrorKind:
    """Distinguish a CUDA/CTranslate2 runtime-load failure from a genuine
    decode failure: a missing/unloadable CUDA runtime is `model_load`, never
    `audio_decode` -- mislabeling it points the operator at the audio file
    for what is actually an environment problem (FR-7, FR-8).
    """
    message = str(exc).lower()
    if any(marker in message for marker in _MODEL_LOAD_ERROR_MARKERS):
        return ErrorKind.MODEL_LOAD
    return ErrorKind.AUDIO_DECODE


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
        self._model_state: str = "unloaded"
        self._device, self._compute_type = _resolve_device_and_compute_type(config)
        self._model_id: str = getattr(config, "model", "base") or "base"
        self._model_path: str | None = getattr(config, "model_path", "") or None
        self._word_timestamps: bool = bool(getattr(config, "word_timestamps", False))
        self._filter_hallucinations: bool = bool(getattr(config, "filter_hallucinations", True))

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
        kwargs: dict[str, Any] = {
            "model_size_or_path": self._model_id,
            "device": self._device,
            "compute_type": self._compute_type,
            "download_root": self._model_path,
            # Never silently fetch weights over the network (FR-3
            # acceptance: "loads the model with no network access"). The
            # model directory is populated by F4's installer; if it isn't
            # there, this raises instead of downloading, and is reported as
            # `model_load` below, naming `model_path`.
            "local_files_only": True,
        }
        if self._device == "cpu":
            kwargs["cpu_threads"] = getattr(self.config, "cpu_threads", 4)

        try:
            model = WhisperModel(**kwargs)
        except Exception as exc:
            self._model_state = "unloaded"
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                f"failed to load model {self._model_id!r} from {self._model_path!r}: {exc}",
            ) from exc

        self._model = model
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

        try:
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=language,
                beam_size=5,
                vad_filter=True,
                word_timestamps=self._word_timestamps,
            )
        except Exception as exc:
            kind = _classify_transcribe_failure(exc)
            if kind is ErrorKind.MODEL_LOAD:
                raise ServiceError(
                    ErrorKind.MODEL_LOAD, f"model runtime failed on {self._device}: {exc}"
                ) from exc
            raise ServiceError(
                ErrorKind.AUDIO_DECODE,
                f"failed to decode {audio_path.name}: {exc}",
            ) from exc

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
                kind = _classify_transcribe_failure(exc)
                if kind is ErrorKind.MODEL_LOAD:
                    raise ServiceError(
                        ErrorKind.MODEL_LOAD, f"model runtime failed on {self._device}: {exc}"
                    ) from exc
                raise ServiceError(
                    ErrorKind.AUDIO_DECODE,
                    f"failed to decode {audio_path.name}: {exc}",
                ) from exc

            mapped = _map_segment(segment, len(raw_segments), include_words=self._word_timestamps)
            raw_segments.append(mapped)

            fraction = min(segment.end / duration, 1.0) if duration > 0 else 1.0
            on_progress(fraction)

        kept, n_filtered = apply_filters(raw_segments, enabled=self._filter_hallucinations)
        segments_out: list[dict[str, Any]] = [dict(segment) for segment in kept]
        text = "".join(str(segment["text"]) for segment in segments_out)

        language_out = getattr(info, "language", None) or language
        language_probability = getattr(info, "language_probability", None)

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
