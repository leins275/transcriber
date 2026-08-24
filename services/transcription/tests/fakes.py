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

from transcription.diarization import SpeakerTurn
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
        language_probability: float = 0.99,
        model: str = "fake-model",
        device: str = "cpu",
        compute_type: str | None = "int8",
    ) -> None:
        self.config = config
        self._segments = segments if segments is not None else _default_segments()
        self.raise_kind = raise_kind
        # The language this fake "decodes in" when the job asks for none --
        # i.e. what a real provider's constrained detection would pick.
        self.language = language
        self.language_probability = language_probability
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.model_state: str = "unloaded"
        # Spy: the `language` kwarg the last `transcribe` call received
        # (`None` = the caller asked for auto-detection).
        self.seen_language: str | None = None

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
        self.seen_language = language
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
            language_probability=self.language_probability,
            duration_sec=1.0,
            model=self.model,
            device=self.device,
            compute_type=self.compute_type,
        )


def _default_turns() -> list[SpeakerTurn]:
    """Turns matching `_default_segments`: one speaker per segment."""
    return [
        SpeakerTurn(start=0.0, end=0.5, speaker="SPEAKER_00"),
        SpeakerTurn(start=0.5, end=1.0, speaker="SPEAKER_01"),
    ]


class FakeDiarizer:
    """A network-free, torch-free stand-in for the pyannote engine.

    Satisfies `diarizer.DiarizerProtocol`; can be configured to raise a
    given :class:`ErrorKind` to exercise the degradation path.
    """

    name = "fake-diarizer"

    def __init__(
        self,
        config: Any = None,
        *,
        turns: list[SpeakerTurn] | None = None,
        raise_kind: ErrorKind | None = None,
        model: str = "fake-diarization-model",
        device: str = "cpu",
    ) -> None:
        self.config = config
        self._turns = turns if turns is not None else _default_turns()
        self.raise_kind = raise_kind
        self.model = model
        self.device = device
        self.calls: list[Path] = []

    def diarize(self, audio_path: Path, *, cancel: CancelToken) -> list[SpeakerTurn]:
        cancel.raise_if_cancelled()
        self.calls.append(audio_path)
        if self.raise_kind is not None:
            raise ServiceError(self.raise_kind, f"fake diarizer raised {self.raise_kind.value}")
        return list(self._turns)


class FakeLlm:
    """A network-free stand-in for an LLM engine (`llm.base.LlmProvider`).

    Scripted: each `complete()` call pops the next response from
    `responses` (the last one repeats when the script runs dry, so a test
    need not count map-reduce calls exactly). Records every messages list
    and json_schema it was handed. Can raise a given kind, or cancel
    cooperatively partway through a call.
    """

    name = "fake-llm"

    def __init__(
        self,
        config: Any = None,
        *,
        responses: list[str] | None = None,
        raise_kind: ErrorKind | None = None,
        model: str = "fake-llm-model",
    ) -> None:
        self.config = config
        self.responses = list(responses) if responses is not None else ["fake summary"]
        self.raise_kind = raise_kind
        self.model = model
        self.calls: list[list[dict[str, str]]] = []
        self.schemas: list[dict[str, Any] | None] = []
        self.unload_calls = 0

    def describe(self) -> Any:
        from transcription.llm.base import LlmInfo

        return LlmInfo(name=self.name, model=self.model, device="cpu", model_state="loaded")

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> Any:
        from transcription.llm.base import LlmCompletion

        cancel.raise_if_cancelled()
        if self.raise_kind is not None:
            raise ServiceError(self.raise_kind, f"fake llm raised {self.raise_kind.value}")

        self.calls.append(messages)
        self.schemas.append(json_schema)
        for fraction in (0.5, 1.0):
            cancel.raise_if_cancelled()
            on_progress(fraction)

        if len(self.responses) > 1:
            text = self.responses.pop(0)
        else:
            text = self.responses[0]
        return LlmCompletion(text=text, completion_tokens=len(text) // 3)

    def unload(self) -> None:
        self.unload_calls += 1


_FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"


class FakeFrameExtractor:
    """A decode-free stand-in for `frame_extractor.PyAvFrameExtractor`."""

    def __init__(
        self,
        *,
        no_video: bool = False,
        raise_kind: ErrorKind | None = None,
    ) -> None:
        self.no_video = no_video
        self.raise_kind = raise_kind
        self.calls: list[tuple[Path, list[float]]] = []

    def extract(
        self,
        video_path: Path,
        timestamps: list[float],
        *,
        cancel: CancelToken,
    ) -> list[tuple[float, bytes]]:
        cancel.raise_if_cancelled()
        self.calls.append((video_path, list(timestamps)))
        if self.raise_kind is not None:
            raise ServiceError(
                self.raise_kind, f"fake frame extractor raised {self.raise_kind.value}"
            )
        if self.no_video:
            return []
        return [(stamp, _FAKE_PNG) for stamp in sorted(timestamps)]
