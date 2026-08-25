"""Merging and cleanup of per-chunk extraction results (pure logic).

A long transcript is extracted chunk by chunk; this module merges the
per-chunk item lists (deduplicating on normalized title) and snaps the
model's cited timestamps onto real segment starts so screenshots land on
the frames that were actually on screen when the item was discussed.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from transcription.llm.shapes import MAX_ITEM_SCREENSHOT_TIMESTAMPS

_WHITESPACE = re.compile(r"\s+")


def _normalized_title(item: BaseModel) -> str:
    title = getattr(item, "title", "")
    return _WHITESPACE.sub(" ", str(title)).strip().casefold()


def merge_items[ItemT: BaseModel](per_chunk: list[list[ItemT]]) -> list[ItemT]:
    """Concatenate per-chunk item lists, keeping the first of any duplicates.

    Duplicates are items whose normalized titles match -- the same action
    item restated in two chunks. The first occurrence wins; its timestamp
    lists (cited moments and nominated screenshot moments alike) absorb the
    duplicate's so no cited moment is lost. The screenshot union keeps the
    schema's per-item cap: capture plans at most that many frames anyway.
    """
    merged: list[ItemT] = []
    by_title: dict[str, ItemT] = {}
    for items in per_chunk:
        for item in items:
            key = _normalized_title(item)
            existing = by_title.get(key)
            if existing is None:
                by_title[key] = item
                merged.append(item)
                continue
            for field, cap in (
                ("timestamps", None),
                ("screenshot_timestamps", MAX_ITEM_SCREENSHOT_TIMESTAMPS),
            ):
                if not hasattr(existing, field):
                    continue
                existing_stamps = list(getattr(existing, field))
                for stamp in getattr(item, field, []):
                    if stamp not in existing_stamps:
                        existing_stamps.append(stamp)
                if cap is not None:
                    existing_stamps = existing_stamps[:cap]
                # pydantic models are mutable by default; both are plain list fields.
                setattr(existing, field, existing_stamps)
    return merged


def snap_timestamps(
    timestamps: list[float],
    segment_starts: list[float],
    duration_sec: float | None,
) -> list[float]:
    """Clamp cited timestamps into the recording and snap them to segment starts.

    A timestamp outside the recording (a hallucinated citation) is dropped;
    the rest snap to the nearest segment start, then deduplicate, preserving
    citation order.
    """
    snapped: list[float] = []
    for stamp in timestamps:
        if stamp < 0:
            continue
        if duration_sec is not None and stamp > duration_sec:
            continue
        target = stamp
        if segment_starts:
            target = min(segment_starts, key=lambda start: abs(start - stamp))
        if target not in snapped:
            snapped.append(target)
    return snapped
