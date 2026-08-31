"""RAG prompt assembly for the project chat (pure logic).

Turns retrieval output + chat history into one message list under the LLM's
input budget. The split: recent history gets at most a quarter of the
budget (oldest turns dropped first, never truncated mid-message), and the
retrieved chunks fill the rest in fused-rank order. Each included chunk is
labelled ``[S<n>]`` so the model can cite it; the caller reports exactly
the chunks that made the cut as the answer's sources.
"""

from __future__ import annotations

from dataclasses import dataclass

from transcription.llm.base import Message
from transcription.llm.chunking import TokenCounter, estimate_tokens
from transcription.llm.prompts import chat_system_prompt
from transcription.search.service import SearchResult

HISTORY_BUDGET_FRACTION = 0.25


@dataclass(frozen=True, kw_only=True)
class RetrievedChunk:
    """One retrieval hit plus the chunk text the prompt will carry."""

    result: SearchResult
    text: str


def _source_tag(index: int) -> str:
    return f"[S{index + 1}]"


def _chunk_block(index: int, chunk: RetrievedChunk) -> str:
    result = chunk.result
    where = f"{result.project}/{result.meeting_title}"
    stamp = f" @ {result.timestamp}" if result.timestamp else ""
    return f"{_source_tag(index)} [{where} / {result.kind}{stamp}]\n{chunk.text}"


def build_chat_messages(
    *,
    history: list[Message],
    question: str,
    chunks: list[RetrievedChunk],
    language_hint: str | None = None,
    budget_tokens: int,
    count_tokens: TokenCounter = estimate_tokens,
) -> tuple[list[Message], list[SearchResult]]:
    """``(messages, included_sources)`` under ``budget_tokens`` of input.

    ``history`` is the prior turns (user/assistant alternating, without the
    final question); ``question`` is the last user message. Chunks arrive
    in fused-rank order and are included until the remaining budget runs
    out; the sources answered are exactly the included ones, in order.
    """
    system = chat_system_prompt(language=language_hint)
    spent = count_tokens(system) + count_tokens(question)

    history_budget = int(budget_tokens * HISTORY_BUDGET_FRACTION)
    kept_history: list[Message] = []
    history_spent = 0
    for message in reversed(history):
        cost = count_tokens(message.get("content", ""))
        if history_spent + cost > history_budget:
            break
        kept_history.insert(0, message)
        history_spent += cost
    spent += history_spent

    blocks: list[str] = []
    included: list[SearchResult] = []
    for chunk in chunks:
        block = _chunk_block(len(included), chunk)
        cost = count_tokens(block)
        if spent + cost > budget_tokens:
            continue
        blocks.append(block)
        included.append(chunk.result)
        spent += cost

    context = (
        "Meeting materials relevant to the question:\n\n" + "\n\n".join(blocks)
        if blocks
        else "No meeting materials matched the question."
    )
    messages: list[Message] = [{"role": "system", "content": system}]
    messages.extend(kept_history)
    messages.append({"role": "user", "content": f"{context}\n\n---\n\nQuestion: {question}"})
    return messages, included
