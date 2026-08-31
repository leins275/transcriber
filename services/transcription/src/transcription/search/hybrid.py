"""Hybrid retrieval fusion: weighted Reciprocal Rank Fusion over channels.

Pure logic, no I/O. RRF fuses *ranks*, never raw scores -- BM25's rank
numbers and cosine distances are incomparable, and rank fusion is exactly
what sidesteps that. Channel weights follow the reference design
(obsidian-hybrid-search): identity signals (an exact title hit) outrank
relevance signals; trigram fuzz is nearly suppressed and only breaks ties.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

RRF_K = 60

CHANNEL_WEIGHTS: dict[str, float] = {
    "vector": 1.5,
    "bm25": 1.5,
    "exact_title": 2.0,
    "trigram": 0.25,
}


def rrf_fuse(
    channels: Mapping[str, Sequence[int]],
    weights: Mapping[str, float] | None = None,
    k: int = RRF_K,
) -> list[tuple[int, float]]:
    """Fuse per-channel ranked doc-id lists into one ``(doc_id, score)``
    ranking, best first.

    ``score = sum over channels of weight / (k + rank)`` with 1-based ranks.
    A channel that is absent (embedder unavailable, empty MATCH) simply
    contributes nothing -- no renormalization needed.
    """
    if weights is None:
        weights = CHANNEL_WEIGHTS
    scores: dict[int, float] = {}
    for name, ranked in channels.items():
        weight = weights.get(name, 1.0)
        for position, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + position + 1)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def build_fts_match(query: str) -> str:
    """An FTS5 MATCH expression from free text: each term quoted (so FTS5
    operators and stray punctuation in user input stay literal), joined with
    OR so documents matching more terms rank higher naturally."""
    terms = [term.replace('"', '""') for term in query.split() if term.strip('"')]
    return " OR ".join(f'"{term}"' for term in terms)


def make_snippet(chunk_text: str, query: str, max_chars: int = 240) -> str:
    """A short window of ``chunk_text`` around the first query-term hit.

    The chunk's first line is the indexer's breadcrumb -- dropped here, the
    caller already shows the meeting identity. Falls back to the body's
    head when no term matches (a vector-only hit).
    """
    lines = chunk_text.splitlines()
    body = "\n".join(lines[1:]) if len(lines) > 1 else chunk_text
    body = " ".join(body.split())
    if not body:
        return ""

    lowered = body.casefold()
    hit = -1
    for term in query.split():
        term = term.strip('"').casefold()
        if term:
            position = lowered.find(term)
            if position != -1 and (hit == -1 or position < hit):
                hit = position
    if hit == -1:
        return body[:max_chars] + ("…" if len(body) > max_chars else "")

    start = max(0, hit - max_chars // 3)
    end = min(len(body), start + max_chars)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet += "…"
    return snippet
