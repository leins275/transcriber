"""Separating a reasoning model's chain-of-thought from its answer (pure).

Qwen-family reasoning models think in ``<think>...</think>`` blocks. Two
shapes occur in practice:

- the completion carries the full pair, or
- the chat template put the opening ``<think>`` in the *prompt*, so the
  completion is ``<reasoning...></think><answer>`` -- a lone closer with
  everything before it being thought (this is what llama.cpp's Qwen
  template produces).

The operator never wants the thinking in an artifact: the caller writes the
answer into ``summary.md``/items/reports and may save the reasoning to a
sidecar file the UI does not show.
"""

from __future__ import annotations

import re

_THINK_PAIR = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSER = re.compile(r"</think(?:ing)?>", re.IGNORECASE)


def split_reasoning(text: str) -> tuple[str, str | None]:
    """``(answer, reasoning)`` -- reasoning is ``None`` when there was none."""
    reasoning_parts: list[str] = []

    def capture(match: re.Match[str]) -> str:
        content = match.group(1).strip()
        if content:
            reasoning_parts.append(content)
        return ""

    cleaned = _THINK_PAIR.sub(capture, text)

    if not reasoning_parts:
        # The lone-closer shape: the template opened the tag in the prompt.
        closer = _THINK_CLOSER.search(cleaned)
        if closer is not None:
            thought = cleaned[: closer.start()].strip()
            if thought:
                reasoning_parts.append(thought)
            cleaned = cleaned[closer.end() :]

    reasoning = "\n\n".join(reasoning_parts) if reasoning_parts else None
    return cleaned.strip(), reasoning


class ThinkStreamFilter:
    """Streaming counterpart of :func:`split_reasoning`.

    Feed it decoded pieces as they arrive; it answers the visible text that
    may be emitted so far. Everything up to (and including) a ``</think>``
    closer is held back; after the closer, pieces pass straight through.

    There is deliberately **no** "doesn't look like thinking" early flush:
    with llama.cpp's Qwen template the opener lives in the *prompt* (the
    lone-closer shape), so reasoning streams as plain prose and any
    heuristic flush would leak it. The cost is that a completion with no
    think block at all streams nothing until :meth:`flush`, which then
    resolves the whole buffer through :func:`split_reasoning` -- delayed,
    never leaked, never dropped.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._passthrough = False

    def feed(self, piece: str) -> str:
        if self._passthrough:
            return piece
        self._buffer += piece
        closer = _THINK_CLOSER.search(self._buffer)
        if closer is None:
            return ""
        visible = self._buffer[closer.end() :]
        self._buffer = ""
        self._passthrough = True
        return visible.lstrip("\n")

    def flush(self) -> str:
        """The visible remainder at end-of-stream: for a stream that never
        carried a closer, the batch splitter decides what (if anything) was
        answer rather than thought. An *unclosed* opener (generation cut
        off mid-thought) yields nothing -- that text was never an answer."""
        held, self._buffer = self._buffer, ""
        if self._passthrough:
            return held
        if held.lstrip().lower().startswith("<think"):
            return ""
        answer, _reasoning = split_reasoning(held)
        return answer
