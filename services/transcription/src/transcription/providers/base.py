"""The provider seam (FR-4): the only interface anyone outside `providers/` uses.

No module outside `providers/` and `config.py` may import a provider library
directly -- everything transcribes through :class:`TranscriptionProvider`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from transcription.errors import ErrorKind, ServiceError

ModelState = Literal["unloaded", "loading", "loaded"]


@dataclass(frozen=True, kw_only=True)
class ProviderInfo:
    """What `/health` and job status report about the active provider."""

    name: str
    model: str
    device: str
    compute_type: str | None
    model_state: ModelState


@dataclass(frozen=True, kw_only=True)
class TranscriptResult:
    """The result of one `transcribe` call, provider-agnostic."""

    segments: list[dict[str, Any]]
    text: str
    language: str | None
    language_probability: float | None
    duration_sec: float
    model: str
    device: str
    compute_type: str | None
    cost_usd: float | None = None
    currency: str | None = None
    filtered_segment_count: int = 0

    def __post_init__(self) -> None:
        if self.duration_sec < 0:
            raise ValueError(f"duration_sec must be >= 0, got {self.duration_sec!r}")


class CancelToken:
    """A thin `threading.Event` wrapper cooperative providers poll between chunks."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise ServiceError(ErrorKind.CANCELLED, "operation was cancelled")


class TranscriptionProvider(Protocol):
    """The only interface `jobs.py` (and everything above it) may depend on."""

    name: str

    def describe(self) -> ProviderInfo: ...

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult: ...
