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
