"""mlx-whisper on the Apple Silicon GPU (the `mlx` whisper backend).

CTranslate2 -- faster-whisper's engine -- runs on the CPU on a Mac (its
Metal backend is an unmerged pull request), so on Apple Silicon the
transcription goes through MLX instead: Apple's array framework runs the
same large-v3 weights on the GPU, measured at roughly twice whisper.cpp's
speed on M-series machines. The library is baked into the macOS build
(the `whisper-mlx` extra); the weights (`config.whisper_mlx_repo`, fp16
large-v3) are a first-run download into
``<app_dir>/models/whisper-mlx/<repo basename>/``.

mlx-whisper has no VAD and decodes a whole waveform per call, so the
provider feeds it the recording's *speech* in ten-minute batches
(`whisper_common.speech_batches`), maps the timestamps back and reports
progress per batch -- the same silence-skipping faster-whisper does on
its own. Decoding is greedy with temperature fallback (mlx-whisper has no
beam search); `condition_on_previous_text` is off, as on the other
engines, so a hallucination never seeds the next window.

The model is released after each job unless `whisper_keep_loaded` says
otherwise: on a 16 GB MacBook Air the GPU shares the unified memory with
the LLM that summarizes next.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from transcription.engines import READY_MARKER, mlx_weights_dir
from transcription.errors import ErrorKind, ServiceError
from transcription.providers import whisper_common as common
from transcription.providers.audio import decode_audio
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptResult

logger = logging.getLogger("transcription")

# One engine call per ten minutes of speech: long enough that the per-call
# overhead vanishes, short enough for progress and cancellation to mean
# something on a two-hour recording.
BATCH_SEC = 600.0
# What the detector looks at: one encoder window of speech.
DETECTION_WINDOW_SEC = 30.0


class MlxWhisperProvider:
    """The `mlx` backend behind the `local` provider on Apple Silicon."""

    name = "mlx"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._weights_dir: Path = mlx_weights_dir(config)
        self._model_id: str = str(getattr(config, "whisper_mlx_repo", "")).rsplit("/", 1)[-1]
        self._word_timestamps: bool = bool(getattr(config, "word_timestamps", True))
        self._filter_hallucinations: bool = bool(getattr(config, "filter_hallucinations", True))
        self._resegment_gap_sec: float = float(getattr(config, "resegment_gap_sec", 0.6))
        self._vad_parameters = common.vad_parameters(config)
        self._model: Any = None
        self._model_state: str = "unloaded"
        self._device = "metal"
        self._compute_type = "float16"
        # Importing here, at construction, is what lets the provider
        # factory fall back to faster-whisper when the library is missing.
        self._lib = self._import_library()
        if not (self._weights_dir / READY_MARKER).exists():
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                f"the MLX whisper weights are not installed under {self._weights_dir}",
            )

    @staticmethod
    def _import_library() -> Any:
        """The import seam tests replace: the `mlx_whisper` module."""
        import mlx_whisper  # noqa: PLC0415 - lazy, the whole point

        return mlx_whisper

    def describe(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            model=self._model_id,
            device=self._device,
            compute_type=self._compute_type,
            model_state=self._model_state,  # type: ignore[arg-type]
        )

    # -- loading -------------------------------------------------------------

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        self._model_state = "loading"
        try:
            import mlx.core as mx  # noqa: PLC0415

            holder = self._lib.transcribe.ModelHolder
            self._model = holder.get_model(str(self._weights_dir), mx.float16)
        except Exception as exc:
            self._model_state = "unloaded"
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                f"failed to load the MLX whisper weights from {self._weights_dir}: {exc}",
            ) from exc
        self._model_state = "loaded"
        logger.info(
            "mlx whisper loaded",
            extra={"event": "whisper_loaded", "engine": self.name, "model": self._model_id},
        )
        return self._model

    def unload(self) -> None:
        """Release the weights (the GPU's memory is the machine's)."""
        if self._model is None:
            return
        self._model = None
        self._model_state = "unloaded"
        try:
            holder = self._lib.transcribe.ModelHolder
            holder.model = None
            holder.model_path = None
            import mlx.core as mx  # noqa: PLC0415

            mx.clear_cache()
        except Exception as exc:  # noqa: BLE001 - best effort; the reference is gone either way
            logger.debug("mlx cache not cleared: %s", exc)

    # -- decoding ------------------------------------------------------------

    def _detect_language(self, model: Any, speech: Any) -> tuple[str, float]:
        """ru/en/tr from the model's own language head over one window of
        speech; the unconstrained `transcribe` detection is the fallback
        when the lower-level API has moved."""
        window = speech[: int(DETECTION_WINDOW_SEC * common.SAMPLE_RATE)]
        try:
            audio_mod = self._lib.audio
            mel = audio_mod.log_mel_spectrogram(
                window, n_mels=model.dims.n_mels, padding=audio_mod.N_SAMPLES
            )
            segment = audio_mod.pad_or_trim(mel, audio_mod.N_FRAMES, axis=-2)
            _, probs = model.detect_language(segment)
            if isinstance(probs, list):
                probs = probs[0]
            return common.constrain_language(probs)
        except Exception as exc:  # noqa: BLE001 - fall back to the high-level API
            logger.info("mlx language head unavailable (%s); detecting through transcribe", exc)
        result = self._lib.transcribe(
            window, path_or_hf_repo=str(self._weights_dir), language=None, verbose=None
        )
        language = str(result.get("language") or "")
        if language in common.DECODE_LANGUAGES:
            return language, common.NO_EVIDENCE_PROBABILITY
        return common.DEFAULT_LANGUAGE, common.NO_EVIDENCE_PROBABILITY

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
            waveform = decode_audio(str(audio_path))
        except Exception as exc:
            raise ServiceError(
                ErrorKind.AUDIO_DECODE, f"failed to decode {audio_path.name}: {exc}"
            ) from exc
        duration = float(len(waveform)) / common.SAMPLE_RATE

        try:
            batches = common.speech_batches(
                waveform, parameters=self._vad_parameters, max_sec=BATCH_SEC
            )
            decode_language = language
            probability: float | None = None
            if decode_language is None:
                speech = common.detection_audio(waveform, self._vad_parameters)
                decode_language, probability = self._detect_language(model, speech)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                ErrorKind.AUDIO_DECODE, f"failed to prepare {audio_path.name}: {exc}"
            ) from exc

        raw_segments: list[dict[str, Any]] = []
        for index, batch in enumerate(batches):
            cancel.raise_if_cancelled()
            try:
                result = self._lib.transcribe(
                    batch.audio,
                    path_or_hf_repo=str(self._weights_dir),
                    language=decode_language,
                    task="transcribe",
                    word_timestamps=self._word_timestamps,
                    condition_on_previous_text=False,
                    verbose=None,
                    fp16=True,
                )
            except ServiceError:
                raise
            except Exception as exc:
                raise ServiceError(
                    ErrorKind.AUDIO_DECODE, f"mlx whisper failed on {audio_path.name}: {exc}"
                ) from exc
            segments = [
                common.map_segment(
                    new_id=len(raw_segments) + offset,
                    start=segment["start"],
                    end=segment["end"],
                    text=str(segment.get("text", "")),
                    avg_logprob=segment.get("avg_logprob"),
                    no_speech_prob=segment.get("no_speech_prob"),
                    compression_ratio=segment.get("compression_ratio"),
                    words=segment.get("words") if self._word_timestamps else None,
                )
                for offset, segment in enumerate(result.get("segments") or [])
                if str(segment.get("text", "")).strip()
            ]
            common.restore_times(segments, batch.chunks)
            raw_segments.extend(segments)
            on_progress((index + 1) / len(batches))
        if not batches:
            on_progress(1.0)

        segments_out, text, n_filtered = common.finish(
            raw_segments,
            filter_hallucinations=self._filter_hallucinations,
            word_timestamps=self._word_timestamps,
            resegment_gap_sec=self._resegment_gap_sec,
        )
        return TranscriptResult(
            segments=segments_out,
            text=text,
            language=decode_language,
            language_probability=probability,
            duration_sec=duration,
            model=self._model_id,
            device=self._device,
            compute_type=self._compute_type,
            filtered_segment_count=n_filtered,
        )
