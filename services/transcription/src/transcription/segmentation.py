"""Utterance-level re-segmentation from word timestamps.

Whisper emits segments of up to ~30 seconds that freely span several
speakers' utterances -- the desktop UI groups segments into turns by the
pause *between* segments (`turns.ts`), so a multi-utterance segment can
never be split downstream and reads as several replicas concatenated
together. This pass cuts each segment at sentence-ending punctuation and at
word-level pauses, giving the turn grouping the granularity it needs.

Pure functions over segment-like mappings; no imports from the rest of the
package (same contract as `filters.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SENTENCE_ENDINGS = (".", "!", "?", "…")

# Inherited by every child segment so `filters.py`-style confidence data
# stays attached to the text it was measured on.
_INHERITED_KEYS = ("avg_logprob", "no_speech_prob", "compression_ratio")

DEFAULT_GAP_SEC = 0.6


def resegment(
    segments: Sequence[Mapping[str, Any]], *, gap_sec: float = DEFAULT_GAP_SEC
) -> list[dict[str, Any]]:
    """Split segments into utterance-sized pieces and renumber ids.

    A segment is cut after a word that ends with sentence punctuation, and
    before a word that starts at least ``gap_sec`` after the previous word
    ended. Segments without word timestamps (or with a single word) pass
    through unchanged. Ids are renumbered sequentially over the result --
    the same post-condition as ``filters.apply_filters``.
    """
    out: list[dict[str, Any]] = []
    for segment in segments:
        out.extend(_split_segment(dict(segment), gap_sec))
    for new_id, seg in enumerate(out):
        seg["id"] = new_id
    return out


def _split_segment(segment: dict[str, Any], gap_sec: float) -> list[dict[str, Any]]:
    words: list[Mapping[str, Any]] = list(segment.get("words") or [])
    if len(words) < 2:
        return [segment]

    pieces: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for index, word in enumerate(words):
        current.append(word)
        following = words[index + 1] if index + 1 < len(words) else None
        if following is None:
            break
        ends_sentence = str(word.get("word", "")).rstrip().endswith(SENTENCE_ENDINGS)
        long_pause = float(following["start"]) - float(word["end"]) >= gap_sec
        if ends_sentence or long_pause:
            pieces.append(current)
            current = []
    if current:
        pieces.append(current)

    if len(pieces) <= 1:
        # One piece == the whole segment: keep the original verbatim, so its
        # exact text and timestamps survive (word times can be slightly
        # narrower than the segment's own start/end).
        return [segment]

    children: list[dict[str, Any]] = []
    for piece in pieces:
        child: dict[str, Any] = {key: segment.get(key) for key in _INHERITED_KEYS}
        child["id"] = segment.get("id", 0)
        child["start"] = float(piece[0]["start"])
        child["end"] = float(piece[-1]["end"])
        # faster-whisper word texts carry their leading space, so a plain
        # join reconstructs the original spacing.
        child["text"] = "".join(str(word.get("word", "")) for word in piece)
        child["words"] = [dict(word) for word in piece]
        children.append(child)
    return children
