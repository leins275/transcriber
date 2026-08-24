"""Map-reduce summarization orchestration (pure control flow).

The LLM call itself is injected as a callback so this module stays free of
provider imports and trivially testable: ``complete(messages) -> str``.
"""

from __future__ import annotations

from collections.abc import Callable

from transcription.llm.prompts import (
    Message,
    chunk_summary_messages,
    merge_summaries_messages,
    summary_messages,
)

CompleteFn = Callable[[list[Message]], str]


def summarize_chunks(chunks: list[str], complete: CompleteFn, language: str | None = None) -> str:
    """Summarize a transcript given as pre-budgeted chunks.

    One chunk goes straight to a single summary call; several go through
    map (one compact summary per chunk) then reduce (merge into one).
    ``language`` -- the transcript's own, threaded in by the caller -- pins
    every one of those calls, the reduce included, so a long meeting cannot
    end up merged into the wrong language.
    """
    if not chunks:
        raise ValueError("cannot summarize an empty transcript")
    if len(chunks) == 1:
        return complete(summary_messages(chunks[0], language=language)).strip()

    partials = [
        complete(chunk_summary_messages(chunk, i, len(chunks), language=language)).strip()
        for i, chunk in enumerate(chunks)
    ]
    return complete(merge_summaries_messages(partials, language=language)).strip()
