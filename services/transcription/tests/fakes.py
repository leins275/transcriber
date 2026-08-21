"""Test doubles for the provider protocol (FR-4).

Owned by T8; imported by T10-T15's tests as the hook that stands in for a
real model or cloud call, so the default test suite stays model-free,
GPU-free and network-free (FR-15).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transcription.errors import ErrorKind, ServiceError
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptResult


@dataclass(frozen=True, kw_only=True)
class FakeSegment:
    """A minimal stand-in for one transcribed segment."""

    id: int
    start: float
    end: float
    text: str
    avg_logprob: float = -0.1
    no_speech_prob: float = 0.05
    compression_ratio: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "avg_logprob": self.avg_logprob,
            "no_speech_prob": self.no_speech_prob,
            "compression_ratio": self.compression_ratio,
        }


def _default_segments() -> list[FakeSegment]:
    return [
        FakeSegment(id=0, start=0.0, end=0.5, text="hello "),
        FakeSegment(id=1, start=0.5, end=1.0, text="world"),
    ]


class FakeProvider:
    """A configurable, network-free stand-in for a real provider.

    Reports progress at 0.25/0.5/0.75/1.0 across four fake "chunks",
    checking the cancel token between each one, and can be configured to
    raise a given :class:`ErrorKind` instead of succeeding.
    """

    name = "fake"

    def __init__(
        self,
        config: Any = None,
        *,
        segments: list[FakeSegment] | None = None,
        raise_kind: ErrorKind | None = None,
        language: str | None = "en",
        model: str = "fake-model",
        device: str = "cpu",
        compute_type: str | None = "int8",
    ) -> None:
        self.config = config
        self._segments = segments if segments is not None else _default_segments()
        self.raise_kind = raise_kind
        self.language = language
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.model_state: str = "unloaded"

    def describe(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            model=self.model,
            device=self.device,
            compute_type=self.compute_type,
            model_state=self.model_state,  # type: ignore[arg-type]
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        self.model_state = "loading"
        self.model_state = "loaded"

        if self.raise_kind is not None:
            raise ServiceError(self.raise_kind, f"fake provider raised {self.raise_kind.value}")

        for fraction in (0.25, 0.5, 0.75, 1.0):
            cancel.raise_if_cancelled()
            on_progress(fraction)

        text = "".join(seg.text for seg in self._segments)
        return TranscriptResult(
            segments=[seg.as_dict() for seg in self._segments],
            text=text,
            language=language or self.language,
            language_probability=0.99,
            duration_sec=1.0,
            model=self.model,
            device=self.device,
            compute_type=self.compute_type,
        )
