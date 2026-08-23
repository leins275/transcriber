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
