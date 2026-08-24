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
