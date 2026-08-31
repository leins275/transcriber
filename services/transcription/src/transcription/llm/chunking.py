"""Token-budget chunking for long transcripts (pure logic, zero package imports).

Counting defaults to a crude character heuristic (``len(text) // 2``);
callers that have a loaded model inject its real tokenizer via the
``count_tokens`` callables instead. The heuristic is deliberately
conservative -- Qwen-family BPE tokenizes Cyrillic at roughly 2-3
characters per token, and Russian is a primary language here -- because a
chunk that underfills the context window costs one extra LLM call, while a
chunk that overflows it costs the whole job.
"""

from __future__ import annotations

from collections.abc import Callable

TokenCounter = Callable[[str], int]

# Chat-template scaffolding, system prompt and per-call instructions that
# share the context window with the transcript chunk itself.
PROMPT_OVERHEAD_TOKENS = 512

# Below this the transcript would be sliced into confetti; budgets are
# floored here even under a pathologically small configured context.
MIN_BUDGET_TOKENS = 1024


def estimate_tokens(text: str) -> int:
    """A conservative token estimate: ~2 characters per token."""
    return max(1, len(text) // 2)


def input_budget_tokens(n_ctx: int, max_output_tokens: int, think_headroom_tokens: int) -> int:
    """How many input tokens a single completion may spend on transcript text.

    Everything else that shares the window -- the answer, the reasoning
    model's ``<think>`` block, prompt scaffolding -- is subtracted up front,
    so a chunk that fits this budget cannot overflow ``n_ctx``.
    """
    return max(
        MIN_BUDGET_TOKENS,
        n_ctx - max_output_tokens - think_headroom_tokens - PROMPT_OVERHEAD_TOKENS,
    )


def split_oversized(
    line: str, budget_tokens: int, count_tokens: TokenCounter = estimate_tokens
) -> list[str]:
    """Split a single line that alone exceeds ``budget_tokens``.

    Pieces break at whitespace where possible; a whitespace-free monster
    string is halved recursively. Nothing is ever dropped: the pieces
    concatenate (modulo the split whitespace) back to the original line.
    """
    if count_tokens(line) <= budget_tokens:
        return [line]

    words = line.split(" ")
    if len(words) == 1:
        mid = len(line) // 2
        if mid == 0:
            return [line]  # a single huge token; nothing left to split
        return split_oversized(line[:mid], budget_tokens, count_tokens) + split_oversized(
            line[mid:], budget_tokens, count_tokens
        )

    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for word in words:
        word_tokens = count_tokens(word) + 1
        if current and current_tokens + word_tokens > budget_tokens:
            pieces.append(" ".join(current))
            current = []
            current_tokens = 0
        current.append(word)
        current_tokens += word_tokens
    if current:
        pieces.append(" ".join(current))
    # A single word can itself exceed the budget; recurse into those pieces.
    return [
        part for piece in pieces for part in split_oversized(piece, budget_tokens, count_tokens)
    ]


def chunk_line_ranges_with_overlap(
    lines: list[str],
    budget_tokens: int,
    overlap_tokens: int,
    count_tokens: TokenCounter = estimate_tokens,
) -> list[tuple[int, int]]:
    """Group ``lines`` into half-open ``(start, end)`` index ranges of at most
    ``budget_tokens``, with consecutive ranges sharing ~``overlap_tokens``
    worth of trailing lines.

    Index ranges rather than joined text, so the caller can map a chunk back
    to whatever its lines carry (segment timestamps, for the search index).
    Unlike :func:`chunk_lines` a single over-budget line is emitted as its
    own one-line range instead of being split -- splitting would break the
    line <-> range mapping, and the embedder truncates defensively anyway.
    """
    if budget_tokens <= 0:
        raise ValueError(f"budget_tokens must be positive, got {budget_tokens}")
    if overlap_tokens < 0 or overlap_tokens >= budget_tokens:
        raise ValueError(f"overlap_tokens must be in [0, budget_tokens), got {overlap_tokens}")

    ranges: list[tuple[int, int]] = []
    start = 0
    current_tokens = 0
    for index, line in enumerate(lines):
        line_tokens = count_tokens(line)
        if index > start and current_tokens + line_tokens > budget_tokens:
            ranges.append((start, index))
            # Back up over trailing lines worth ~overlap_tokens, but always
            # move forward past the previous range's start.
            overlap_start = index
            overlap_budget = overlap_tokens
            while overlap_start > start + 1:
                trailing = count_tokens(lines[overlap_start - 1])
                if trailing > overlap_budget:
                    break
                overlap_budget -= trailing
                overlap_start -= 1
            start = overlap_start
            current_tokens = sum(count_tokens(text) for text in lines[start:index])
        current_tokens += line_tokens
    if start < len(lines):
        ranges.append((start, len(lines)))
    return ranges


def chunk_lines(
    lines: list[str],
    budget_tokens: int,
    count_tokens: TokenCounter = estimate_tokens,
) -> list[str]:
    """Group ``lines`` into newline-joined chunks of at most ``budget_tokens``.

    Boundaries prefer falling between lines (transcript segments); a single
    line larger than the whole budget is split by ``split_oversized`` rather
    than emitted whole -- an over-budget chunk would overflow the context
    window, and dropping text would be worse.
    """
    if budget_tokens <= 0:
        raise ValueError(f"budget_tokens must be positive, got {budget_tokens}")

    fitted: list[str] = []
    for line in lines:
        if count_tokens(line) > budget_tokens:
            fitted.extend(split_oversized(line, budget_tokens, count_tokens))
        else:
            fitted.append(line)

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for line in fitted:
        line_tokens = count_tokens(line)
        if current and current_tokens + line_tokens > budget_tokens:
            chunks.append("\n".join(current))
            current = []
            current_tokens = 0
        current.append(line)
        current_tokens += line_tokens
    if current:
        chunks.append("\n".join(current))
    return chunks
