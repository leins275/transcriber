"""The pyannote speaker-diarization engine, behind a lazy-import seam.

Mirrors the provider seam's discipline (FR-3, FR-4 spirit): ``pyannote.audio``
(and the torch stack underneath it) is imported only when the first diarized
job actually runs -- never at module import, never at engine construction --
so a build without the optional ``diarization`` extra installed still imports
this module freely and only fails, classified as ``model_load``, when someone
turns the feature on.

The pipeline is cached on the instance for the process lifetime, exactly like
the whisper model: the second diarized job pays nothing to load it.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any, Protocol

from transcription.diarization import DiarizationOutput, SpeakerTurn
from transcription.diarization_runtime import (
    activate_runtime,
    diarization_cache_dir,
    full_checkpoint_loads,
    hub_offline,
    install_hub_compat,
    is_diarization_model_present,
)
from transcription.errors import ErrorKind, ServiceError
from transcription.providers.base import CancelToken

_logger = logging.getLogger("transcription")

DEFAULT_DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"

# What the stock pipeline's models were trained on; the recording is
# resampled to it on decode.
_PIPELINE_SAMPLE_RATE = 16000

# Substrings of a raw exception message that indicate the model could not be
# fetched/loaded (gated repo, missing token, no network, CUDA runtime), as
# opposed to a genuine failure over the audio itself. Matched
# case-insensitively; same technique as `providers/local_whisper.py`.
_MODEL_LOAD_ERROR_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "gated",
    "authentication",
    "token",
    "could not download",
    "not found on the hugging face hub",
    "offline",
    "connection",
    "cuda",
    "cudnn",
    "cublas",
)


class DiarizerProtocol(Protocol):
    """What `jobs.py` may depend on -- the diarization counterpart of
    `TranscriptionProvider`."""

    name: str
    model: str
    device: str

    def diarize(self, audio_path: Path, *, cancel: CancelToken) -> DiarizationOutput: ...


def _classify_diarize_failure(exc: Exception) -> ErrorKind:
    message = str(exc).lower()
    if any(marker in message for marker in _MODEL_LOAD_ERROR_MARKERS):
        return ErrorKind.MODEL_LOAD
    return ErrorKind.AUDIO_DECODE


def _embeddings_by_label(annotation: Any, embedding_rows: Any) -> dict[str, list[float]] | None:
    """Map `annotation.labels()` onto the embedding matrix's rows.

    pyannote's ``return_embeddings=True`` answers one row per speaker, in
    ``annotation.labels()`` order. Any surprise here -- a missing method, a
    row count mismatch, a row of NaNs for a speaker with no clean speech --
    degrades to fewer (or no) embeddings with a warning, never an error:
    the transcript's speaker turns are the artifact, embeddings are a bonus.
    """
    if embedding_rows is None:
        return None
    try:
        labels = [str(label) for label in annotation.labels()]
        by_label: dict[str, list[float]] = {}
        for label, row in zip(labels, embedding_rows, strict=True):
            vector = [float(value) for value in row]
            if all(math.isfinite(value) for value in vector):
                by_label[label] = vector
        return by_label or None
    except Exception:
        _logger.warning(
            "could not map diarization embeddings to speaker labels",
            exc_info=True,
            extra={"event": "diarizer_embeddings_skipped"},
        )
        return None


class PyannoteDiarizer:
    """Lazy-loaded pyannote diarization engine."""

    name = "pyannote"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._pipeline: Any = None
        self.model: str = (
            getattr(config, "diarization_model", DEFAULT_DIARIZATION_MODEL)
            or DEFAULT_DIARIZATION_MODEL
        )
        self._model_path: str = getattr(config, "diarization_model_path", "") or ""
        self._hf_token: str | None = getattr(config, "hf_token", None) or None
        self._min_speakers: int | None = getattr(config, "diarization_min_speakers", None)
        self._max_speakers: int | None = getattr(config, "diarization_max_speakers", None)
        # The app folder anchors both first-run payloads: the fetched
        # runtime (`runtime/diarization`, put on sys.path at import time)
        # and the model cache (`models/diarization`, the hub-cache layout
        # pyannote reads). Absent (a bare test config), both fall back to
        # pyannote's own defaults.
        app_dir = getattr(config, "app_dir", None)
        self._app_dir: Path | None = Path(app_dir) if app_dir else None
        # `auto` resolves against torch's own CUDA probe at load time, not
        # here -- resolving it now would force the torch import this class
        # exists to defer.
        self.device: str = getattr(config, "device", "auto") or "auto"

    def _import_pyannote(self) -> Any:
        """The import seam tests monkeypatch; returns the `Pipeline` class."""
        if self._app_dir is not None:
            # pyannote reads `PYANNOTE_CACHE` when it is imported, so the
            # cache directory must be decided before the import below; and
            # a fetched runtime must be on `sys.path` for the import to
            # find pyannote at all.
            os.environ.setdefault("PYANNOTE_CACHE", str(diarization_cache_dir(self._app_dir)))
            activate_runtime(self._app_dir)
        # pyannote 3.x binds `hf_hub_download` at import; the wrapper that
        # translates its `use_auth_token=` for huggingface_hub 1.x must be
        # in place first.
        install_hub_compat()
        try:
            # Absent in CI (`import-not-found`), present but untyped in a dev
            # environment synced with the extra (`import-untyped`).
            from pyannote.audio import (  # type: ignore[import-not-found,import-untyped,unused-ignore]  # noqa: PLC0415,E501
                Pipeline,
            )
        except ImportError as exc:
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                "speaker diarization requires the optional 'pyannote.audio' package "
                "(install the service's 'diarization' extra)",
            ) from exc
        return Pipeline

    def _resolve_torch_device(self) -> Any:
        """A `torch.device` honouring an explicit config device; `auto` probes
        CUDA the same way the whisper provider does."""
        import torch  # type: ignore[import-not-found]  # noqa: PLC0415

        device = self.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        return torch.device(device)

    def _load_source(self) -> str:
        """What `Pipeline.from_pretrained` is given: a local snapshot when
        configured (offline-first, like the whisper model dir), else the hub
        id -- which for pyannote's gated models needs `hf_token`."""
        if self._model_path:
            candidate = Path(self._model_path)
            config_yaml = candidate / "config.yaml"
            if config_yaml.is_file():
                return str(config_yaml)
            return str(candidate)
        return self.model

    def _ensure_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline

        pipeline_cls = self._import_pyannote()
        source = self._load_source()
        try:
            kwargs: dict[str, Any] = {}
            # Once the pinned snapshots are in the app's cache, the load is
            # strictly offline: the token stays out of it and the hub is
            # never consulted, so the pin holds. Without them (a dev
            # environment that never ran the model fetch), the token lets
            # pyannote download on first use, as it always did.
            offline = False
            if self._app_dir is not None and not self._model_path:
                kwargs["cache_dir"] = str(diarization_cache_dir(self._app_dir))
                offline = is_diarization_model_present(self._app_dir)
            if self._hf_token and not offline:
                kwargs["use_auth_token"] = self._hf_token
            with hub_offline(offline), full_checkpoint_loads():
                pipeline = pipeline_cls.from_pretrained(source, **kwargs)
            if pipeline is None:
                # `from_pretrained` answers None (not an exception) for a
                # gated repo whose terms have not been accepted.
                raise ServiceError(
                    ErrorKind.MODEL_LOAD,
                    f"diarization model {self.model!r} is not available: the model is "
                    "gated on Hugging Face -- accept its terms and provide an access "
                    "token (TRANSCRIBER_HF_TOKEN or HF_TOKEN)",
                )
            pipeline.to(self._resolve_torch_device())
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                f"failed to load diarization model {self.model!r}: {exc}",
            ) from exc

        self._pipeline = pipeline
        _logger.info(
            "diarization pipeline loaded",
            extra={"event": "diarizer_loaded", "model": self.model, "device": self.device},
        )
        return pipeline

    def _decode(self, audio_path: Path) -> Any:
        """What the pipeline is handed: the recording decoded to a 16 kHz
        mono waveform, not the file path.

        pyannote's own reader is torchaudio, which on Windows has only the
        `soundfile` backend (wav/flac/ogg) -- it cannot open the mp4/m4a/
        mkv recordings the vault files. faster-whisper's decoder (PyAV, with
        FFmpeg bundled in its wheel) is what the transcription itself used
        on the same file, so the pass sees exactly the audio the words came
        from. A decode failure is the recording's problem (`audio_decode`),
        never the model's.
        """
        try:
            import torch  # type: ignore[import-not-found,unused-ignore]  # noqa: PLC0415

            from transcription.providers.audio import decode_samples  # noqa: PLC0415 - lazy

            samples = decode_samples(audio_path, sample_rate=_PIPELINE_SAMPLE_RATE)
        except Exception as exc:
            raise ServiceError(
                ErrorKind.AUDIO_DECODE, f"could not decode {audio_path.name}: {exc}"
            ) from exc
        waveform = torch.from_numpy(samples).reshape(1, -1)
        return {"waveform": waveform, "sample_rate": _PIPELINE_SAMPLE_RATE, "uri": audio_path.stem}

    def diarize(self, audio_path: Path, *, cancel: CancelToken) -> DiarizationOutput:
        """Run diarization over the whole file; returns speaker turns plus,
        when the pipeline supports it, one voice embedding per speaker.

        The pyannote pipeline is not cooperatively cancellable mid-run, so
        the token is honoured at the boundaries: before the (possibly
        multi-second) pipeline load, before inference, and before the result
        is handed back.
        """
        cancel.raise_if_cancelled()
        pipeline = self._ensure_pipeline()
        cancel.raise_if_cancelled()
        audio = self._decode(audio_path)
        cancel.raise_if_cancelled()

        call_kwargs: dict[str, Any] = {}
        if self._min_speakers is not None:
            call_kwargs["min_speakers"] = self._min_speakers
        if self._max_speakers is not None:
            call_kwargs["max_speakers"] = self._max_speakers

        try:
            # The stock speaker-diarization pipeline computes per-speaker
            # embeddings internally either way; asking for them back costs
            # nothing. A hand-picked pipeline without the kwarg gets one
            # retry without it -- embeddings are a bonus, never a failure.
            try:
                result = pipeline(audio, return_embeddings=True, **call_kwargs)
            except TypeError:
                result = pipeline(audio, **call_kwargs)
        except Exception as exc:
            kind = _classify_diarize_failure(exc)
            raise ServiceError(kind, f"diarization failed on {audio_path.name}: {exc}") from exc

        cancel.raise_if_cancelled()

        if isinstance(result, tuple):
            annotation, embedding_rows = result
        else:
            annotation, embedding_rows = result, None

        turns = [
            SpeakerTurn(start=float(turn.start), end=float(turn.end), speaker=str(label))
            for turn, _track, label in annotation.itertracks(yield_label=True)
        ]
        turns.sort(key=lambda t: (t.start, t.end))
        return DiarizationOutput(
            turns=turns, embeddings=_embeddings_by_label(annotation, embedding_rows)
        )
