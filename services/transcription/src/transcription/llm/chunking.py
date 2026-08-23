"""Token-budget chunking for long transcripts (pure logic, zero package imports).

The budget is a crude character-count heuristic (``len(text) // 3`` -- safe
for both Latin and Cyrillic text, which BPE tokenizers split more finely
than English), deliberately conservative: a chunk that underfills the
context window costs one extra LLM call; a chunk that overflows it costs
the whole job.
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """A conservative token estimate: ~3 characters per token."""
    return max(1, len(text) // 3)


def chunk_lines(lines: list[str], budget_tokens: int) -> list[str]:
    """Group ``lines`` into newline-joined chunks of at most ``budget_tokens``.

    Boundaries always fall between lines (transcript segments), never inside
    one. A single line larger than the whole budget still becomes its own
    chunk -- the estimate is conservative enough that this only happens for
    degenerate input, and dropping text would be worse.
    """
    if budget_tokens <= 0:
        raise ValueError(f"budget_tokens must be positive, got {budget_tokens}")

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for line in lines:
        line_tokens = estimate_tokens(line)
        if current and current_tokens + line_tokens > budget_tokens:
            chunks.append("\n".join(current))
            current = []
            current_tokens = 0
        current.append(line)
        current_tokens += line_tokens
    if current:
        chunks.append("\n".join(current))
    return chunks
