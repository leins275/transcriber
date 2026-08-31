"""Incremental vault walk: files on disk -> rows in the search index.

Best-effort like everything vault-facing: an unreadable meeting contributes
a warning, never a failed job. The two-tier skip (mtime, then content hash)
makes a re-run over an unchanged vault a stat-walk; a changed file rebuilds
its whole document (chunks + embeddings) -- doc-granular replace, KISS.

Chunk text carries a breadcrumb line (project / meeting / time range /
speakers) so both BM25 and the embedding see the context a bare excerpt
lacks.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from transcription.artifacts import (
    NOTE_FILE_NAME,
    SUMMARY_FILE_NAME,
    TRANSCRIPT_FILE_NAME,
    UNSORTED_DIR_NAME,
    source_date_from_meeting_name,
)
from transcription.errors import ErrorKind, ServiceError
from transcription.exporting import load_speaker_overrides, load_transcript
from transcription.llm.base import EmbeddingProvider
from transcription.llm.chunking import (
    TokenCounter,
    chunk_line_ranges_with_overlap,
    estimate_tokens,
)
from transcription.llm.prompts import format_timestamp, render_transcript_lines
from transcription.providers.base import CancelToken
from transcription.search.index_db import ChunkRecord, DocRecord, IndexDb

logger = logging.getLogger("transcription")

# Project-level directories that are not projects (mirrors the vault
# crate's RESERVED_PROJECT_DIR_NAMES, plus the legacy trees).
_RESERVED_PROJECT_DIRS = frozenset({"reports", "action items", "facts", "exports", "chats"})

CHUNK_BUDGET_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
# A chunk whose body (sans breadcrumb) is shorter than this is noise.
MIN_CHUNK_BODY_CHARS = 50
# Texts per embed() call; cancellation is checked between batches.
EMBED_BATCH_SIZE = 16

_MAX_TEXT_FILE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, kw_only=True)
class IndexStats:
    scanned: int
    indexed: int
    skipped: int
    removed: int
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "removed": self.removed,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, kw_only=True)
class _Line:
    text: str
    start_sec: float | None = None
    end_sec: float | None = None


def _meeting_title(meeting_name: str) -> str:
    """The folder name minus its leading ``YYMMDD - `` date prefix."""
    if len(meeting_name) >= 6 and meeting_name[:6].isdigit():
        rest = meeting_name[6:].lstrip(" -")
        return rest or meeting_name
    return meeting_name


def _read_text_file(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_TEXT_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _transcript_lines(meeting_dir: Path, transcript: dict[str, Any]) -> tuple[list[_Line], str]:
    """Per-segment lines with their timestamps, plus the distinct speaker
    names (overrides applied) space-joined for the trigram channel."""
    segments_raw = transcript.get("segments")
    segments = [seg for seg in segments_raw if isinstance(seg, dict)] if segments_raw else []
    overrides = load_speaker_overrides(meeting_dir)
    lines: list[_Line] = []
    names: list[str] = []
    for segment in segments:
        # One segment at a time through the shared renderer, so the line
        # format stays single-sourced with the LLM prompts.
        rendered = render_transcript_lines([segment], overrides)
        if not rendered:
            continue
        lines.append(
            _Line(
                text=rendered[0],
                start_sec=float(segment.get("start", 0.0)),
                end_sec=float(segment.get("end", 0.0)),
            )
        )
        name = overrides.get(str(segment.get("id", ""))) or segment.get("speaker")
        if name and str(name).strip() and str(name) not in names:
            names.append(str(name))
    return lines, " ".join(names)


def _chunks_from_lines(
    lines: list[_Line],
    breadcrumb_of: Callable[[_Line, _Line], str],
    count_tokens: TokenCounter,
) -> list[ChunkRecord]:
    texts = [line.text for line in lines]
    ranges = chunk_line_ranges_with_overlap(
        texts, CHUNK_BUDGET_TOKENS, CHUNK_OVERLAP_TOKENS, count_tokens
    )
    chunks: list[ChunkRecord] = []
    for start, end in ranges:
        body = "\n".join(texts[start:end])
        if len(body.strip()) < MIN_CHUNK_BODY_CHARS:
            continue
        first, last = lines[start], lines[end - 1]
        chunks.append(
            ChunkRecord(
                text=f"{breadcrumb_of(first, last)}\n{body}",
                start_sec=first.start_sec,
                end_sec=last.end_sec,
            )
        )
    return chunks


def _embed_chunks(
    chunks: list[ChunkRecord],
    embedder: EmbeddingProvider | None,
    cancel: CancelToken,
    warnings: list[str],
) -> list[ChunkRecord]:
    if embedder is None or not chunks:
        return chunks
    out: list[ChunkRecord] = []
    for offset in range(0, len(chunks), EMBED_BATCH_SIZE):
        cancel.raise_if_cancelled()
        batch = chunks[offset : offset + EMBED_BATCH_SIZE]
        try:
            vectors = embedder.embed([chunk.text for chunk in batch])
        except ServiceError as exc:
            if exc.kind is ErrorKind.CANCELLED:
                raise
            # Text-only rows still index; the warning names the degradation.
            if not warnings or "embedding" not in warnings[-1]:
                warnings.append(f"embedding unavailable: {exc.message}")
            out.extend(batch)
            continue
        for chunk, vector in zip(batch, vectors, strict=True):
            out.append(
                ChunkRecord(
                    text=chunk.text,
                    start_sec=chunk.start_sec,
                    end_sec=chunk.end_sec,
                    embedding=vector,
                )
            )
    return out


def index_vault(
    vault_root: Path,
    db: IndexDb,
    embedder: EmbeddingProvider | None,
    *,
    count_tokens: TokenCounter = estimate_tokens,
    on_progress: Callable[[float], None] = lambda _fraction: None,
    cancel: CancelToken | None = None,
) -> IndexStats:
    """Walk the vault into ``db``. ``embedder=None`` (or a failing one)
    indexes text-only; the BLOB-less rows still serve BM25/trigram search."""
    cancel = cancel or CancelToken()
    warnings: list[str] = []
    scanned = indexed = skipped = 0
    live: set[tuple[str, str]] = set()

    candidates: list[tuple[str, Path, str]] = []  # (project, meeting_dir, kind)
    try:
        top_level = sorted(entry for entry in vault_root.iterdir() if entry.is_dir())
    except OSError:
        top_level = []
    for project_dir in top_level:
        if project_dir.name.lower() in _RESERVED_PROJECT_DIRS:
            continue
        project = (
            UNSORTED_DIR_NAME if project_dir.name.lower() == UNSORTED_DIR_NAME else project_dir.name
        )
        try:
            meeting_dirs = sorted(entry for entry in project_dir.iterdir() if entry.is_dir())
        except OSError:
            continue
        for meeting_dir in meeting_dirs:
            for kind, file_name in (
                ("transcript", TRANSCRIPT_FILE_NAME),
                ("summary", SUMMARY_FILE_NAME),
                ("note", NOTE_FILE_NAME),
            ):
                if (meeting_dir / file_name).is_file():
                    candidates.append((project, meeting_dir, kind))

    for position, (project, meeting_dir, kind) in enumerate(candidates):
        cancel.raise_if_cancelled()
        on_progress(position / len(candidates) if candidates else 1.0)
        scanned += 1
        file_name = {
            "transcript": TRANSCRIPT_FILE_NAME,
            "summary": SUMMARY_FILE_NAME,
            "note": NOTE_FILE_NAME,
        }[kind]
        path = meeting_dir / file_name
        rel_dir = f"{project}/{meeting_dir.name}"
        live.add((rel_dir, kind))

        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            live.discard((rel_dir, kind))
            continue
        stored = db.doc_fingerprint(rel_dir, kind)
        if stored is not None and stored[0] == mtime_ns:
            skipped += 1
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            warnings.append(f"unreadable {rel_dir}/{file_name}: {exc}")
            continue
        content_hash = hashlib.sha256(raw).hexdigest()
        if stored is not None and stored[1] == content_hash:
            db.touch_mtime(rel_dir, kind, mtime_ns)
            skipped += 1
            continue

        title = _meeting_title(meeting_dir.name)
        date = source_date_from_meeting_name(meeting_dir.name)
        speakers = ""
        if kind == "transcript":
            transcript = load_transcript(meeting_dir)
            if transcript is None:
                warnings.append(f"unparseable transcript in {rel_dir}")
                continue
            lines, speakers = _transcript_lines(meeting_dir, transcript)

            def transcript_breadcrumb(first: _Line, last: _Line) -> str:
                window = (
                    f"{format_timestamp(first.start_sec or 0.0)}"
                    f"–{format_timestamp(last.end_sec or 0.0)}"
                )
                return f"[{project} / {meeting_dir.name} / {window}]"  # noqa: B023

            chunks = _chunks_from_lines(lines, transcript_breadcrumb, count_tokens)
        else:
            text = raw.decode("utf-8", errors="replace")
            lines = [_Line(text=line) for line in text.splitlines() if line.strip()]

            def flat_breadcrumb(_first: _Line, _last: _Line) -> str:
                return f"[{project} / {meeting_dir.name} / {kind}]"  # noqa: B023

            chunks = _chunks_from_lines(lines, flat_breadcrumb, count_tokens)

        chunks = _embed_chunks(chunks, embedder, cancel, warnings)
        db.upsert_doc(
            DocRecord(
                kind=kind,
                project=project,
                meeting_dir=rel_dir,
                meeting_title=title,
                meeting_date=date,
                speakers=speakers,
                mtime_ns=mtime_ns,
                content_hash=content_hash,
            ),
            chunks,
        )
        indexed += 1

    removed = db.delete_docs_not_in(live)
    on_progress(1.0)
    if warnings:
        logger.warning(
            "index pass finished with warnings",
            extra={"event": "index_warnings", "count": len(warnings)},
        )
    return IndexStats(
        scanned=scanned, indexed=indexed, skipped=skipped, removed=removed, warnings=warnings
    )
