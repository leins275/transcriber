"""Speaker-turn alignment: attach diarization output to transcript segments.

The diarizer (``diarizer.py``) answers "who spoke when" as a list of
:class:`SpeakerTurn` intervals; whisper answers "what was said when" as
segments. This module joins the two: each segment is attributed to the
speaker who owns most of its speech time, weighted by word timestamps when
the segment carries them (word-level voting survives a segment that brushes
against a neighbouring turn's edge far better than whole-segment overlap).

Raw diarization labels (``SPEAKER_00``, ``SPEAKER_01``, ...) are normalized
to human-facing ``"Speaker 1"``, ``"Speaker 2"``, ... in order of first
speech, matching how the desktop UI presents unnamed speakers for renaming.

Pure functions over segment-like mappings; no imports from the rest of the
package (same contract as `filters.py` and `segmentation.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# A segment (or word) that overlaps no turn at all is still attributed to
# the nearest turn when its midpoint lies within this many seconds of it --
# diarization trims silence harder than whisper does, so a short utterance
# can fall entirely into a gap between two turns.
NEAREST_TURN_TOLERANCE_SEC = 2.0

SPEAKER_LABEL_PREFIX = "Speaker "


@dataclass(frozen=True, kw_only=True)
class SpeakerTurn:
    """One diarized interval: `speaker` held the floor from `start` to `end`."""

    start: float
    end: float
    speaker: str

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"turn end {self.end!r} precedes start {self.start!r}")


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _vote_interval(
    start: float, end: float, turns: Sequence[SpeakerTurn], votes: dict[str, float]
) -> None:
    """Add `[start, end]`'s overlap with every turn to the per-speaker tally."""
    for turn in turns:
        shared = _overlap(start, end, turn.start, turn.end)
        if shared > 0.0:
            votes[turn.speaker] = votes.get(turn.speaker, 0.0) + shared


def _nearest_speaker(midpoint: float, turns: Sequence[SpeakerTurn]) -> str | None:
    """The speaker of the turn nearest to `midpoint`, within the tolerance."""
    best: str | None = None
    best_distance = NEAREST_TURN_TOLERANCE_SEC
    for turn in turns:
        if turn.start <= midpoint <= turn.end:
            return turn.speaker
        distance = turn.start - midpoint if midpoint < turn.start else midpoint - turn.end
        if distance <= best_distance:
            best = turn.speaker
            best_distance = distance
    return best


def _segment_speaker(segment: Mapping[str, Any], turns: Sequence[SpeakerTurn]) -> str | None:
    words: list[Mapping[str, Any]] = list(segment.get("words") or [])
    votes: dict[str, float] = {}

    if words:
        for word in words:
            _vote_interval(float(word["start"]), float(word["end"]), turns, votes)
    if not votes:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        _vote_interval(start, end, turns, votes)
        if not votes:
            return _nearest_speaker((start + end) / 2.0, turns)

    # Ties broken by first-vote order (dict preserves insertion order), i.e.
    # the speaker whose turn appears earliest -- deterministic either way.
    return max(votes.items(), key=lambda item: item[1])[0]


def assign_speakers(
    segments: Sequence[Mapping[str, Any]], turns: Sequence[SpeakerTurn]
) -> list[dict[str, Any]]:
    """Copy `segments`, setting each copy's ``speaker`` from `turns`.

    A segment no turn can claim (no overlap and nothing within the
    tolerance) gets ``speaker: None`` -- honest "don't know", which the UI
    renders as unattributed rather than fabricating a guess.
    """
    out: list[dict[str, Any]] = []
    for segment in segments:
        labelled = dict(segment)
        labelled["speaker"] = _segment_speaker(segment, turns)
        out.append(labelled)
    return out


def normalize_labels(segments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rename raw diarizer labels to ``Speaker 1..N`` in order of first speech.

    The diarizer's own labels (``SPEAKER_00``...) are cluster ids in an
    order the caller cannot predict; numbering by first appearance means
    "Speaker 1" is always the first voice heard, which is what a reader
    scanning the transcript expects.
    """
    mapping: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for segment in segments:
        renamed = dict(segment)
        raw = renamed.get("speaker")
        if raw is not None:
            raw_str = str(raw)
            if raw_str not in mapping:
                mapping[raw_str] = f"{SPEAKER_LABEL_PREFIX}{len(mapping) + 1}"
            renamed["speaker"] = mapping[raw_str]
        out.append(renamed)
    return out


def label_segments(
    segments: Sequence[Mapping[str, Any]], turns: Sequence[SpeakerTurn]
) -> tuple[list[dict[str, Any]], int]:
    """Assign and normalize in one pass; returns ``(segments, speaker_count)``."""
    labelled = normalize_labels(assign_speakers(segments, turns))
    speakers = {seg["speaker"] for seg in labelled if seg.get("speaker") is not None}
    return labelled, len(speakers)
