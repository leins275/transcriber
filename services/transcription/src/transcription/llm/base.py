"""The LLM seam: the only interface anyone outside ``llm/`` uses.

Mirrors ``providers/base.py`` (FR-4's provider-isolation rule, applied to
LLM engines): no module outside ``llm/`` and ``config.py`` may import an LLM
library directly -- every completion goes through :class:`LlmProvider`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from transcription.providers.base import CancelToken

ModelState = Literal["unloaded", "loading", "loaded"]

# One chat message on its way to the model: {"role": ..., "content": ...}.
Message = dict[str, str]


@dataclass(frozen=True, kw_only=True)
class LlmInfo:
    """What ``/health`` and job status report about the active LLM engine."""

    name: str
    model: str
    device: str
    model_state: ModelState


@dataclass(frozen=True, kw_only=True)
class LlmCompletion:
    """The result of one ``complete`` call, engine-agnostic."""

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    currency: str | None = None
    # Why generation stopped: "stop" (natural), "length" (hit max_tokens),
    # or None when the engine did not say. "length" means ``text`` is an
    # incomplete prefix -- callers must never treat it as a finished answer.
    finish_reason: str | None = None


class LlmTruncatedError(Exception):
    """A completion stopped at ``max_tokens`` (``finish_reason == "length"``).

    The text is a valid *prefix* of an answer, not an answer: a truncated
    summary is cut mid-sentence and truncated grammar-constrained JSON does
    not parse. Callers recover by splitting the input and retrying, never by
    using the text as-is.
    """


class LlmProvider(Protocol):
    """The only interface ``jobs.py`` (and everything above it) may depend on.

    ``complete`` runs one chat completion. When ``json_schema`` is given the
    engine constrains (llama.cpp: grammar-compiled) or requests (OpenAI
    protocol: ``response_format``) schema-conforming JSON output.
    Implementations stream internally so ``cancel`` is honoured between
    token batches and ``on_progress`` can advance during generation.
    """

    name: str

    def describe(self) -> LlmInfo: ...

    def count_tokens(self, text: str) -> int:
        """How many tokens ``text`` costs under this engine's tokenizer.

        Used for chunk budgeting; implementations fall back to the character
        heuristic in ``chunking.py`` when the real tokenizer is unavailable.
        """
        ...

    def complete(
        self,
        messages: list[Message],
        *,
        json_schema: dict[str, object] | None,
        max_tokens: int,
        temperature: float,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> LlmCompletion: ...

    def unload(self) -> None:
        """Release the loaded model's memory; a later call reloads lazily."""
        ...


class EmbeddingProvider(Protocol):
    """The embedding seam behind hybrid search -- same isolation rule as
    :class:`LlmProvider`: nothing outside ``llm/`` imports the engine
    library, everything embeds through this."""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One unit-normalized vector per input text, in order."""
        ...

    def dim(self) -> int:
        """The embedding width every :meth:`embed` vector has."""
        ...

    def unload(self) -> None:
        """Release the loaded model's memory; a later call reloads lazily."""
        ...
