"""Map-reduce summarization orchestration (pure control flow).

The LLM call itself is injected as a callback so this module stays free of
provider imports and trivially testable: ``complete(messages) -> str``. Two
failure modes of a small local model are handled here:

- a call that hits the output-token cap raises
  :class:`~transcription.llm.base.LlmTruncatedError` -- the input is split
  (via the caller-supplied ``split_chunk``) and retried, because a truncated
  summary must never be used as-is;
- the reduce runs in rounds: partial summaries are packed into groups that
  fit ``reduce_budget_tokens`` and merged group by group until one summary
  remains, so an arbitrarily long transcript can never overflow the context
  window in a single unbounded merge prompt.
"""

from __future__ import annotations

from collections.abc import Callable

from transcription.llm.base import LlmTruncatedError
from transcription.llm.chunking import TokenCounter, estimate_tokens
from transcription.llm.prompts import (
    Message,
    chunk_summary_messages,
    merge_summaries_messages,
    summary_messages,
)

CompleteFn = Callable[[list[Message]], str]
# (chunk, depth) -> smaller pieces; depth counts how many splits deep we are.
SplitFn = Callable[[str, int], list[str]]

# How many times one chunk may be split in half before truncation becomes a
# hard error: 3 levels = an eighth of the original budget.
MAX_SPLIT_DEPTH = 3

# Per-partial framing ("--- Part N summary ---") counted against the reduce
# budget alongside the partial itself.
_PARTIAL_FRAMING_TOKENS = 12

# The reduce strictly shrinks the partials list every round, so this cap is
# unreachable; it exists so a logic bug can never spin forever.
_MAX_REDUCE_ROUNDS = 20


def summarize_chunks(
    chunks: list[str],
    complete: CompleteFn,
    language: str | None = None,
    *,
    reduce_budget_tokens: int | None = None,
    count_tokens: TokenCounter = estimate_tokens,
    split_chunk: SplitFn | None = None,
) -> str:
    """Summarize a transcript given as pre-budgeted chunks.

    One chunk goes straight to a single summary call; several go through
    map (one compact summary per chunk) then reduce (merge into one).
    ``language`` -- the transcript's own, threaded in by the caller -- pins
    every one of those calls, the reduce included, so a long meeting cannot
    end up merged into the wrong language.

    Without ``reduce_budget_tokens`` the reduce is a single merge call (the
    pure-unit contract); with it, merging happens in budget-fitted rounds.
    Without ``split_chunk``, a truncated call propagates.
    """
    if not chunks:
        raise ValueError("cannot summarize an empty transcript")
    if len(chunks) == 1:
        try:
            return complete(summary_messages(chunks[0], language=language)).strip()
        except LlmTruncatedError as truncated:
            chunks = _split(chunks[0], 0, split_chunk, truncated)
            # Fall through to map-reduce over the pieces.

    partials: list[str] = []
    for i, chunk in enumerate(chunks):
        partials.extend(_map_one(chunk, i, len(chunks), complete, language, split_chunk, 0))
    return _reduce(partials, complete, language, reduce_budget_tokens, count_tokens)


def _split(
    chunk: str, depth: int, split_chunk: SplitFn | None, truncated: LlmTruncatedError
) -> list[str]:
    """The pieces to retry a truncated call with; re-raises ``truncated``
    when splitting is unavailable or cannot make the input any smaller."""
    if split_chunk is None or depth >= MAX_SPLIT_DEPTH:
        raise truncated
    pieces = split_chunk(chunk, depth)
    if len(pieces) <= 1:
        raise truncated
    return pieces


def _map_one(
    chunk: str,
    index: int,
    total: int,
    complete: CompleteFn,
    language: str | None,
    split_chunk: SplitFn | None,
    depth: int,
) -> list[str]:
    """One map call, split-and-retried on truncation; returns the partial
    summaries this chunk contributed (several when it had to be split)."""
    try:
        return [complete(chunk_summary_messages(chunk, index, total, language=language)).strip()]
    except LlmTruncatedError as truncated:
        pieces = _split(chunk, depth, split_chunk, truncated)
        partials: list[str] = []
        for piece in pieces:
            partials.extend(
                _map_one(piece, index, total, complete, language, split_chunk, depth + 1)
            )
        return partials


def _group_partials(
    partials: list[str], budget_tokens: int, count_tokens: TokenCounter
) -> list[list[str]]:
    """Pack partials greedily into groups fitting the reduce budget.

    Every group except possibly the last holds at least two partials even
    when that nominally busts the budget -- each partial was itself produced
    under the output cap, and merging fewer than two makes no progress, so
    strict shrinkage outranks the budget here.
    """
    groups: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for partial in partials:
        partial_tokens = count_tokens(partial) + _PARTIAL_FRAMING_TOKENS
        if len(current) >= 2 and current_tokens + partial_tokens > budget_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(partial)
        current_tokens += partial_tokens
    if current:
        groups.append(current)
    return groups


def _merge_group(group: list[str], complete: CompleteFn, language: str | None) -> list[str]:
    """Merge one group of partials; on truncation, merge its halves instead
    (a two-partial group that still truncates propagates -- halving it would
    just return the inputs unchanged and never converge)."""
    if len(group) == 1:
        return [group[0]]
    try:
        return [complete(merge_summaries_messages(group, language=language)).strip()]
    except LlmTruncatedError:
        if len(group) < 3:
            raise
        mid = len(group) // 2
        return _merge_group(group[:mid], complete, language) + _merge_group(
            group[mid:], complete, language
        )


def _reduce(
    partials: list[str],
    complete: CompleteFn,
    language: str | None,
    budget_tokens: int | None,
    count_tokens: TokenCounter,
) -> str:
    if len(partials) == 1:
        return partials[0].strip()
    if budget_tokens is None:
        return complete(merge_summaries_messages(partials, language=language)).strip()

    rounds = 0
    while len(partials) > 1:
        rounds += 1
        if rounds > _MAX_REDUCE_ROUNDS:
            raise RuntimeError("summary reduce did not converge")
        next_partials: list[str] = []
        for group in _group_partials(partials, budget_tokens, count_tokens):
            next_partials.extend(_merge_group(group, complete, language))
        partials = next_partials
    return partials[0].strip()
