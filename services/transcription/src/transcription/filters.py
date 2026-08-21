# Copyright and license: Apache License, Version 2.0
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Adapted from Vexa (Vexa-ai/vexa), Apache-2.0 — origin:
# core/meetings/services/transcription/src/transcription/main.py:99-118 and
# core/meetings/modules/whisper/src/confidence.ts:8-13
"""Silence and hallucination filtering heuristics (FR-12).

Pure functions over segment-like mappings; no imports from the rest of the
package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

NO_SPEECH_THRESHOLD = 0.6
LOG_PROB_THRESHOLD = -1.0
LOG_PROB_HARD_THRESHOLD = -1.3
COMPRESSION_RATIO_THRESHOLD = 2.4


def is_low_confidence(segment: Mapping[str, Any]) -> bool:
    """Return True if a single segment looks like silence or a hallucination."""
    no_speech_prob = segment.get("no_speech_prob")
    avg_logprob = segment.get("avg_logprob")
    compression_ratio = segment.get("compression_ratio")

    if (
        no_speech_prob is not None
        and avg_logprob is not None
        and no_speech_prob > NO_SPEECH_THRESHOLD
        and avg_logprob < LOG_PROB_THRESHOLD
    ):
        return True

    if compression_ratio is not None and compression_ratio > COMPRESSION_RATIO_THRESHOLD:
        return True

    if avg_logprob is not None and avg_logprob < LOG_PROB_HARD_THRESHOLD:
        return True

    return False


def looks_like_silence(segments: Sequence[Mapping[str, Any]]) -> bool:
    """Return True when every segment is a silence-shaped segment (or there are none)."""
    if not segments:
        return True

    def _is_silence(segment: Mapping[str, Any]) -> bool:
        no_speech_prob = segment.get("no_speech_prob")
        avg_logprob = segment.get("avg_logprob")
        return (
            no_speech_prob is not None
            and avg_logprob is not None
            and no_speech_prob > NO_SPEECH_THRESHOLD
            and avg_logprob < LOG_PROB_THRESHOLD
        )

    return all(_is_silence(segment) for segment in segments)


def apply_filters(
    segments: Sequence[Mapping[str, Any]], *, enabled: bool
) -> tuple[list[Mapping[str, Any]], int]:
    """Drop low-confidence segments (when enabled) and renumber the survivors.

    Returns (kept_segments, n_filtered). When disabled, the input is returned
    unchanged and n_filtered is 0 (the toggle is live).
    """
    if not enabled:
        return list(segments), 0

    kept: list[Mapping[str, Any]] = []
    n_filtered = 0
    for segment in segments:
        if is_low_confidence(segment):
            n_filtered += 1
            continue
        kept.append(segment)

    renumbered: list[Mapping[str, Any]] = []
    for new_id, segment in enumerate(kept):
        updated = dict(segment)
        updated["id"] = new_id
        renumbered.append(updated)

    return renumbered, n_filtered
