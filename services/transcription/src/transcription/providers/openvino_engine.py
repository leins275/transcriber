"""OpenVINO GenAI's WhisperPipeline on an Intel NPU (the `openvino` backend).

On an Intel Core Ultra machine without an NVIDIA GPU, whisper has three
places to run: the NPU, the Arc iGPU and the CPU. OpenVINO's whisper
pipeline runs the same exported model on any of them, so the provider
tries the devices in the order the engine plan gives
(`engines.openvino_device_order`: NPU, then GPU, then CPU, or the one the
config pins) and keeps the first that opens. The runtime is a first-run
fetch (`openvino_runtime.py`); the weights are a pre-exported IR
(`config.whisper_openvino_repo`, turbo int8 -- OpenVINO's own validated
NPU workload) under ``<app_dir>/models/whisper-openvino/``.

The NPU compiles the model to a static graph on first load -- minutes on
a Panther Lake, cached by the driver afterwards -- so the job's
`model_state` sits at ``loading`` meanwhile and the log says why. Its
static shapes also mean one 30-second window per call and no
`initial_prompt`, both of which this provider never needs: the recording's
speech is cut into 30-second batches by the shared VAD
(`whisper_common.speech_batches`), decoded one at a time with progress and
cancellation between them, and the timestamps mapped back.

Language detection: the pipeline does not report the language it detects,
so an auto run asks faster-whisper's large-v3 (the fallback engine, always
installed) on the CPU for one window's language distribution and applies
the ru/en/tr rule -- a few seconds once per job, released with the model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from transcription import engines, hardware, openvino_runtime
from transcription.errors import ErrorKind, ServiceError
from transcription.providers import whisper_common as common
from transcription.providers.audio import decode_audio
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptResult

logger = logging.getLogger("transcription")

# One encoder window: the NPU graph is static at 30 s.
BATCH_SEC = 30.0
DETECTION_WINDOW_SEC = 30.0


class OpenVinoWhisperProvider:
    """The `openvino` backend behind the `local` provider on Intel machines."""

    name = "openvino"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._app_dir = Path(getattr(config, "app_dir", "."))
        self._weights_dir: Path = engines.openvino_weights_dir(config)
        self._model_id: str = str(getattr(config, "whisper_openvino_repo", "")).rsplit("/", 1)[-1]
        self._word_timestamps: bool = bool(getattr(config, "word_timestamps", True))
        self._filter_hallucinations: bool = bool(getattr(config, "filter_hallucinations", True))
        self._resegment_gap_sec: float = float(getattr(config, "resegment_gap_sec", 0.6))
        self._vad_parameters = common.vad_parameters(config)
        self._device_order = engines.openvino_device_order(config, hardware.detect())
        self._pipeline: Any = None
        self._detector: Any = None
        self._device = f"openvino:{self._device_order[0]}"
        self._compute_type = "int8"
        self._model_state: str = "unloaded"
        self._lib = self._import_library()
        if not (self._weights_dir / engines.READY_MARKER).exists():
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                f"the OpenVINO whisper weights are not installed under {self._weights_dir}",
            )

    def _import_library(self) -> Any:
        """The import seam tests replace: `(openvino, openvino_genai)`."""
        openvino_runtime.activate_runtime(self._app_dir)
        import openvino  # noqa: PLC0415 - lazy, the whole point
        import openvino_genai  # noqa: PLC0415

        return openvino, openvino_genai

    def describe(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            model=self._model_id,
            device=self._device,
            compute_type=self._compute_type,
            model_state=self._model_state,  # type: ignore[arg-type]
        )

    # -- loading -------------------------------------------------------------

    def _available_devices(self) -> list[str]:
        openvino, _ = self._lib
        try:
            return [str(device) for device in openvino.Core().available_devices]
        except Exception as exc:  # noqa: BLE001 - treated as "none visible"
            logger.warning("openvino could not list devices: %s", exc)
            return []

    def _open_pipeline(self, device: str) -> Any:
        _, genai = self._lib
        properties: dict[str, Any] = {}
        if device != "NPU":
            # The NPU driver keeps its own compile cache; the others take
            # OpenVINO's, beside the runtime.
            cache = openvino_runtime.openvino_runtime_dir(self._app_dir) / "cache"
            cache.mkdir(parents=True, exist_ok=True)
            properties["CACHE_DIR"] = str(cache)
        if device == "CPU":
            from transcription.providers.local_whisper import resolve_cpu_threads  # noqa: PLC0415

            properties["INFERENCE_NUM_THREADS"] = resolve_cpu_threads(self.config)
        try:
            return genai.WhisperPipeline(
                str(self._weights_dir),
                device,
                word_timestamps=self._word_timestamps,
                **properties,
            )
        except TypeError:
            # An older GenAI without the constructor flag: segment-level
            # timestamps only (`finish` skips the utterance split then).
            self._word_timestamps = False
            return genai.WhisperPipeline(str(self._weights_dir), device, **properties)

    def _ensure_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        self._model_state = "loading"
        available = self._available_devices()
        errors: list[str] = []
        for device in self._device_order:
            if not any(seen == device or seen.startswith(f"{device}.") for seen in available):
                errors.append(f"{device}: not present")
                continue
            if device == "NPU":
                logger.info(
                    "compiling the speech model for the NPU (minutes on a first run, "
                    "cached by the driver afterwards)",
                    extra={"event": "openvino_npu_compile", "model": self._model_id},
                )
            try:
                self._pipeline = self._open_pipeline(device)
            except Exception as exc:  # noqa: BLE001 - the next device gets its turn
                errors.append(f"{device}: {exc}")
                logger.warning("openvino whisper failed to open on %s: %s", device, exc)
                continue
            self._device = f"openvino:{device}"
            self._model_state = "loaded"
            logger.info(
                "openvino whisper loaded",
                extra={"event": "whisper_loaded", "engine": self.name, "device": device},
            )
            return self._pipeline
        self._model_state = "unloaded"
        raise ServiceError(
            ErrorKind.MODEL_LOAD,
            "openvino whisper could not open on any device (" + "; ".join(errors) + ")",
        )

    def unload(self) -> None:
        self._pipeline = None
        self._detector = None
        self._model_state = "unloaded"

    # -- language ------------------------------------------------------------

    def _ensure_detector(self) -> Any:
        """faster-whisper's large-v3 on the CPU, for the language only."""
        if self._detector is None:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]  # noqa: PLC0415

            from transcription.providers.local_whisper import resolve_cpu_threads  # noqa: PLC0415

            self._detector = WhisperModel(
                str(getattr(self.config, "model_path", "")),
                device="cpu",
                compute_type="int8",
                cpu_threads=resolve_cpu_threads(self.config),
                local_files_only=True,
            )
        return self._detector

    def _detect_language(self, speech: Any) -> tuple[str, float]:
        window = speech[: int(DETECTION_WINDOW_SEC * common.SAMPLE_RATE)]
        _, _, all_language_probs = self._ensure_detector().detect_language(audio=window)
        return common.constrain_language(all_language_probs)

    # -- decoding ------------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        cancel.raise_if_cancelled()
        pipeline = self._ensure_pipeline()
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
                decode_language, probability = self._detect_language(speech)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                ErrorKind.MODEL_LOAD, f"failed to prepare {audio_path.name}: {exc}"
            ) from exc

        raw_segments: list[dict[str, Any]] = []
        for index, batch in enumerate(batches):
            cancel.raise_if_cancelled()
            try:
                result = pipeline.generate(
                    batch.audio.astype("float32").tolist(),
                    language=f"<|{decode_language}|>",
                    task="transcribe",
                    return_timestamps=True,
                )
            except ServiceError:
                raise
            except Exception as exc:
                raise ServiceError(
                    ErrorKind.AUDIO_DECODE,
                    f"openvino whisper failed on {audio_path.name}: {exc}",
                ) from exc
            segments = self._segments_of(result, len(raw_segments))
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

    def _segments_of(self, result: Any, first_id: int) -> list[dict[str, Any]]:
        """`WhisperDecodedResults.chunks` as FR-6 segments; the chunk's
        own word list when the pipeline produced one."""
        chunks = getattr(result, "chunks", None) or []
        segments: list[dict[str, Any]] = []
        for chunk in chunks:
            text = str(getattr(chunk, "text", ""))
            if not text.strip():
                continue
            words: list[dict[str, Any]] | None = None
            raw_words = getattr(chunk, "words", None)
            if self._word_timestamps and raw_words:
                words = [
                    {
                        "word": str(getattr(word, "text", getattr(word, "word", ""))),
                        "start": float(getattr(word, "start_ts", getattr(word, "start", 0.0))),
                        "end": float(getattr(word, "end_ts", getattr(word, "end", 0.0))),
                        "probability": getattr(word, "probability", None),
                    }
                    for word in raw_words
                ]
            segments.append(
                common.map_segment(
                    new_id=first_id + len(segments),
                    start=float(getattr(chunk, "start_ts", 0.0)),
                    end=float(getattr(chunk, "end_ts", 0.0)),
                    text=text,
                    words=words,
                )
            )
        return segments
