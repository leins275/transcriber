"""Test doubles for the provider protocol (FR-4).

Owned by T8; imported by T10-T15's tests as the hook that stands in for a
real model call, so the default test suite stays model-free, GPU-free and
network-free (FR-15).
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transcription.diarization import DiarizationOutput, SpeakerTurn
from transcription.errors import ErrorKind, ServiceError
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptResult


class FakeEmbedder:
    """A deterministic, model-free `EmbeddingProvider` stand-in.

    Each text embeds to a unit-normalized 8-dim vector derived from its
    sha256, so identical texts always agree and different texts (almost)
    never do -- enough to exercise storage, rebuild and ranking paths.
    """

    name = "fake-embedder"
    DIM = 8

    def __init__(self, *, raise_kind: ErrorKind | None = None) -> None:
        self.raise_kind = raise_kind
        self.calls: list[list[str]] = []
        self.unload_calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.raise_kind is not None:
            raise ServiceError(self.raise_kind, f"fake embedder raised {self.raise_kind.value}")
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [float(byte) - 127.5 for byte in digest[: self.DIM]]
            norm = math.sqrt(sum(value * value for value in raw)) or 1.0
            vectors.append([value / norm for value in raw])
        return vectors

    def dim(self) -> int:
        return self.DIM

    def unload(self) -> None:
        self.unload_calls += 1


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


# How long a blocked fake waits to be released before giving up, so a test
# that forgets to release fails loudly instead of hanging the suite.
BLOCK_TIMEOUT_SEC = 10.0


class FakeDiarizer:
    """A network-free, torch-free stand-in for the pyannote engine.

    Satisfies `diarizer.DiarizerProtocol`; can be configured to raise a
    given :class:`ErrorKind` to exercise the degradation path.

    Progress and blocking (all optional, all off by default -- a
    `FakeDiarizer()` behaves exactly as it always has):

    * ``phases`` is a script of ``(phase, fraction)`` pairs replayed
      through the caller's ``on_progress`` before the pass returns.
    * ``blocked`` is set once the script has been replayed, so a test can
      wait for the pass to reach its pause point without sleeping.
    * ``release`` is waited on at that same point; the pass returns only
      once the test sets it, which keeps the mid-pass job state readable
      through ``JobManager.status()``. A cancellation requested while the
      pass is paused is honoured on wake, like a cooperative engine.
    """

    name = "fake-diarizer"

    def __init__(
        self,
        config: Any = None,
        *,
        turns: list[SpeakerTurn] | None = None,
        embeddings: dict[str, list[float]] | None = None,
        raise_kind: ErrorKind | None = None,
        model: str = "fake-diarization-model",
        device: str = "cpu",
        phases: list[tuple[str, float | None]] | None = None,
        blocked: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.config = config
        self._turns = turns if turns is not None else _default_turns()
        self._embeddings = embeddings
        self.raise_kind = raise_kind
        self.model = model
        self.device = device
        self.calls: list[Path] = []
        self._phases = list(phases) if phases is not None else []
        self._blocked = blocked
        self._release = release

    def diarize(
        self,
        audio_path: Path,
        *,
        cancel: CancelToken,
        on_progress: Callable[[str, float | None], None] | None = None,
    ) -> DiarizationOutput:
        cancel.raise_if_cancelled()
        self.calls.append(audio_path)
        if self.raise_kind is not None:
            raise ServiceError(self.raise_kind, f"fake diarizer raised {self.raise_kind.value}")
        for phase, fraction in self._phases:
            if on_progress is not None:
                on_progress(phase, fraction)
        if self._blocked is not None:
            self._blocked.set()
        if self._release is not None:
            self._release.wait(BLOCK_TIMEOUT_SEC)
        cancel.raise_if_cancelled()
        return DiarizationOutput(turns=list(self._turns), embeddings=self._embeddings)


class FakeLlm:
    """A network-free stand-in for an LLM engine (`llm.base.LlmProvider`).

    Scripted: each `complete()` call pops the next response from
    `responses` (the last one repeats when the script runs dry, so a test
    need not count map-reduce calls exactly). An entry is a plain string
    (finish_reason "stop") or a `(text, finish_reason)` tuple -- `("...",
    "length")` simulates a completion cut off at max_tokens. Records every
    messages list and json_schema it was handed. Can raise a given kind, or
    cancel cooperatively partway through a call.
    """

    name = "fake-llm"

    def __init__(
        self,
        config: Any = None,
        *,
        responses: list[str | tuple[str, str]] | None = None,
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
        on_token: Callable[[str], None] | None = None,
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
            entry = self.responses.pop(0)
        else:
            entry = self.responses[0]
        if isinstance(entry, tuple):
            text, finish_reason = entry
        else:
            text, finish_reason = entry, "stop"
        if on_token is not None and text:
            # Streamed in ~3 pieces so callers exercise real chunking.
            step = max(1, len(text) // 3)
            for offset in range(0, len(text), step):
                on_token(text[offset : offset + step])
        return LlmCompletion(
            text=text, completion_tokens=len(text) // 3, finish_reason=finish_reason
        )

    def count_tokens(self, text: str) -> int:
        # Deliberately distinct from chunking.estimate_tokens (len // 2) so
        # tests can prove the provider's tokenizer seam is actually used.
        return max(1, len(text) // 4)

    def unload(self) -> None:
        self.unload_calls += 1
