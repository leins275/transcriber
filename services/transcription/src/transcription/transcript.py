"""Build a ``TranscriptDoc`` and write it atomically into ``output_dir`` (FR-6, NFR-5)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from transcription.schema import ProviderInfo, Segment, Source, Stats, TranscriptDoc


def build_document(
    *,
    source_path: str | Path,
    duration_sec: float,
    provider_name: str,
    model: str,
    device: str,
    compute_type: str,
    language: str | None,
    language_probability: float | None,
    text: str,
    segments: list[Segment],
    elapsed_sec: float,
    realtime_factor: float,
    cost_usd: float | None,
    currency: str | None,
    created_at: str | None = None,
) -> TranscriptDoc:
    """Pure assembly of a ``TranscriptDoc``; no filesystem access."""
    path = Path(source_path)
    return TranscriptDoc(
        created_at=created_at if created_at is not None else datetime.now(UTC).isoformat(),
        source=Source(path=str(path), filename=path.name, duration_sec=duration_sec),
        provider=ProviderInfo(
            name=provider_name, model=model, device=device, compute_type=compute_type
        ),
        language=language,
        language_probability=language_probability,
        text=text,
        segments=segments,
        stats=Stats(
            elapsed_sec=elapsed_sec,
            realtime_factor=realtime_factor,
            cost_usd=cost_usd,
            currency=currency,
        ),
    )


def write_atomic(doc: TranscriptDoc, output_dir: str | Path) -> Path:
    """Write ``doc`` to ``output_dir/transcript.json`` atomically (NFR-5).

    Uses ``tempfile.mkstemp`` (never a predictable temp name) inside
    ``output_dir`` itself so ``os.replace`` is a same-filesystem rename, and
    unlinks the temp file if anything raises before the replace lands --
    a reader never observes a partial file.
    """
    out_dir = Path(output_dir)
    target = out_dir / "transcript.json"
    data = doc.model_dump(mode="json")

    fd, tmp_name = tempfile.mkstemp(dir=out_dir, prefix=".transcript-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target
