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
from pathlib import Path
from typing import Any, Protocol

from transcription.diarization import SpeakerTurn
from transcription.errors import ErrorKind, ServiceError
from transcription.providers.base import CancelToken

_logger = logging.getLogger("transcription")

DEFAULT_DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"

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

    def diarize(self, audio_path: Path, *, cancel: CancelToken) -> list[SpeakerTurn]: ...


def _classify_diarize_failure(exc: Exception) -> ErrorKind:
    message = str(exc).lower()
    if any(marker in message for marker in _MODEL_LOAD_ERROR_MARKERS):
        return ErrorKind.MODEL_LOAD
    return ErrorKind.AUDIO_DECODE


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
        # `auto` resolves against torch's own CUDA probe at load time, not
        # here -- resolving it now would force the torch import this class
        # exists to defer.
        self.device: str = getattr(config, "device", "auto") or "auto"

    def _import_pyannote(self) -> Any:
        """The import seam tests monkeypatch; returns the `Pipeline` class."""
        try:
            from pyannote.audio import (  # type: ignore[import-not-found]  # noqa: PLC0415
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
            if self._hf_token:
                kwargs["use_auth_token"] = self._hf_token
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

    def diarize(self, audio_path: Path, *, cancel: CancelToken) -> list[SpeakerTurn]:
        """Run diarization over the whole file; returns speaker turns.

        The pyannote pipeline is not cooperatively cancellable mid-run, so
        the token is honoured at the boundaries: before the (possibly
        multi-second) pipeline load, before inference, and before the result
        is handed back.
        """
        cancel.raise_if_cancelled()
        pipeline = self._ensure_pipeline()
        cancel.raise_if_cancelled()

        call_kwargs: dict[str, Any] = {}
        if self._min_speakers is not None:
            call_kwargs["min_speakers"] = self._min_speakers
        if self._max_speakers is not None:
            call_kwargs["max_speakers"] = self._max_speakers

        try:
            annotation = pipeline(str(audio_path), **call_kwargs)
        except Exception as exc:
            kind = _classify_diarize_failure(exc)
            raise ServiceError(kind, f"diarization failed on {audio_path.name}: {exc}") from exc

        cancel.raise_if_cancelled()

        turns = [
            SpeakerTurn(start=float(turn.start), end=float(turn.end), speaker=str(label))
            for turn, _track, label in annotation.itertracks(yield_label=True)
        ]
        turns.sort(key=lambda t: (t.start, t.end))
        return turns
