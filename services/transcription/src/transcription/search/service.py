"""The search service: one query in, fused results out.

Synchronous by design -- the query embedding is model inference, so the
caller (`api/search_routes.py`) runs `search()` on the job manager's single
serial executor. Missing pieces degrade, never 500: no index yet means no
results, no embedder means text-only channels.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from transcription.errors import ErrorKind, ServiceError
from transcription.llm.base import EmbeddingProvider
from transcription.llm.prompts import format_timestamp
from transcription.search.hybrid import build_fts_match, make_snippet, rrf_fuse
from transcription.search.index_db import IndexDb

logger = logging.getLogger("transcription")

MAX_TOP_K = 50


@dataclass(frozen=True, kw_only=True)
class SearchResult:
    kind: str
    project: str
    meeting_dir: str  # vault-root-relative, forward slashes
    meeting_title: str
    meeting_date: str | None
    snippet: str
    score: float
    start_sec: float | None
    timestamp: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "project": self.project,
            "meeting_dir": self.meeting_dir,
            "meeting_title": self.meeting_title,
            "meeting_date": self.meeting_date,
            "snippet": self.snippet,
            "score": self.score,
            "start_sec": self.start_sec,
            "timestamp": self.timestamp,
        }


class SearchService:
    """Fuses the four retrieval channels over one `IndexDb`."""

    def __init__(
        self,
        db_factory: Callable[[], IndexDb],
        embedder_factory: Callable[[], EmbeddingProvider],
        *,
        top_k_default: int = 10,
    ) -> None:
        self._db_factory = db_factory
        self._embedder_factory = embedder_factory
        self._top_k_default = top_k_default
        self._embedder_broken = False

    def _query_vector(self, query: str) -> list[float] | None:
        """The query's embedding, or ``None`` when the embedder cannot run
        (model absent) -- logged once, then text-only until restart."""
        if self._embedder_broken:
            return None
        try:
            embedder = self._embedder_factory()
            return embedder.embed([query])[0]
        except ServiceError as exc:
            if exc.kind is ErrorKind.CANCELLED:
                raise
            self._embedder_broken = True
            logger.warning(
                "query embedding unavailable; search runs text-only: %s",
                exc.message,
                extra={"event": "search_text_only"},
            )
            return None
        except Exception:
            self._embedder_broken = True
            logger.warning(
                "query embedding failed; search runs text-only",
                exc_info=True,
                extra={"event": "search_text_only"},
            )
            return None

    def _ranked(
        self,
        query: str,
        *,
        project: str | None,
        top_k: int | None,
        dates: set[str] | None = None,
    ) -> list[tuple[SearchResult, str]]:
        """The fused ranking with each hit's best chunk text (the full text,
        breadcrumb included -- retrieval wants substance, `search` snips)."""
        query = query.strip()
        if not query:
            raise ServiceError(ErrorKind.INVALID_REQUEST, "search query must not be empty")
        limit = min(top_k if top_k and top_k > 0 else self._top_k_default, MAX_TOP_K)

        db = self._db_factory()
        match = build_fts_match(query)
        channels: dict[str, list[int]] = {}
        best_chunk_by_doc: dict[int, int] = {}

        vector = self._query_vector(query)
        if vector is not None:
            pairs = db.vec_query(vector, k=limit, dates=dates)
            channels["vector"] = [doc_id for doc_id, _chunk_id in pairs]
            best_chunk_by_doc.update({doc_id: chunk_id for doc_id, chunk_id in pairs})
        if match:
            channels["bm25"] = db.fts_query(match, limit * 3, project=project, dates=dates)
            channels["trigram"] = db.title_trigram_query(query, limit, project=project, dates=dates)
        channels["exact_title"] = db.exact_title_docs(query, project=project, dates=dates)

        fused = rrf_fuse(channels)
        doc_rows = db.get_docs([doc_id for doc_id, _score in fused])

        ranked: list[tuple[SearchResult, str]] = []
        for doc_id, score in fused:
            row = doc_rows.get(doc_id)
            if row is None:
                continue
            if project is not None and row.project != project:
                continue
            if dates and row.meeting_date not in dates:
                continue
            chunk: tuple[str, float | None] | None = None
            chunk_id = best_chunk_by_doc.get(doc_id)
            if chunk_id is not None:
                chunk = db.get_chunk(chunk_id)
            if chunk is None and match:
                chunk = db.best_chunk_for(doc_id, match)
            if chunk is None:
                chunk = db.best_chunk_for(doc_id, "")
            text, start_sec = chunk if chunk is not None else ("", None)
            ranked.append(
                (
                    SearchResult(
                        kind=row.kind,
                        project=row.project,
                        meeting_dir=row.meeting_dir,
                        meeting_title=row.meeting_title,
                        meeting_date=row.meeting_date,
                        snippet=make_snippet(text, query),
                        score=round(score, 6),
                        start_sec=start_sec,
                        timestamp=format_timestamp(start_sec) if start_sec is not None else None,
                    ),
                    text,
                )
            )
            if len(ranked) >= limit:
                break
        return ranked

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        top_k: int | None = None,
        dates: set[str] | None = None,
    ) -> list[SearchResult]:
        """SYNCHRONOUS -- run on the serial executor (query embedding is
        inference and must never overlap whisper/LLM work). ``dates`` (ISO)
        hard-filters every channel to those meeting days."""
        return [
            result
            for result, _text in self._ranked(query, project=project, top_k=top_k, dates=dates)
        ]

    def retrieve(
        self,
        query: str,
        *,
        project: str | None = None,
        top_k: int | None = None,
        dates: set[str] | None = None,
    ) -> list[tuple[SearchResult, str]]:
        """The chat's retrieval: ``(result, full chunk text)`` pairs in
        fused order. Same synchronous/serial-executor rule as `search`."""
        return self._ranked(query, project=project, top_k=top_k, dates=dates)
