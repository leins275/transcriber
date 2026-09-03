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

Pure functions over segment-like mappings; the only in-package import is
`segmentation.py`'s child-segment builder (itself pure), so the contract
`filters.py` and `segmentation.py` follow holds here too.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from transcription.segmentation import children_from_word_runs

# A segment (or word) that overlaps no turn at all is still attributed to
# the nearest turn when its midpoint lies within this many seconds of it --
# diarization trims silence harder than whisper does, so a short utterance
# can fall entirely into a gap between two turns.
NEAREST_TURN_TOLERANCE_SEC = 2.0

SPEAKER_LABEL_PREFIX = "Speaker "

# A run of words attributed to another voice inside a segment counts as a
# real change of speaker only when it is at least this long by *either*
# measure; anything shorter is diarization jitter at a turn's edge (the
# boundaries are ~100-200 ms imprecise) and stays with the surrounding
# voice.
SPLIT_MIN_WORDS = 2
SPLIT_MIN_SEC = 0.4


@dataclass(frozen=True, kw_only=True)
class SpeakerTurn:
    """One diarized interval: `speaker` held the floor from `start` to `end`."""

    start: float
    end: float
    speaker: str

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"turn end {self.end!r} precedes start {self.start!r}")


@dataclass(frozen=True, kw_only=True)
class DiarizationOutput:
    """Everything one diarization pass produced.

    ``embeddings`` maps a raw diarization label (``SPEAKER_00``, ...) to that
    speaker's voice-embedding vector, when the pipeline could produce them --
    the raw material for recognizing the same voice across meetings. ``None``
    when the engine (or a hand-picked pipeline) has no embedding support:
    embeddings are a bonus artifact and their absence is never an error.
    """

    turns: list[SpeakerTurn]
    embeddings: dict[str, list[float]] | None = None


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


def _word_speaker(word: Mapping[str, Any], turns: Sequence[SpeakerTurn]) -> str | None:
    start = float(word["start"])
    end = float(word["end"])
    votes: dict[str, float] = {}
    _vote_interval(start, end, turns, votes)
    if votes:
        return max(votes.items(), key=lambda item: item[1])[0]
    return _nearest_speaker((start + end) / 2.0, turns)


def _is_jitter(run: Sequence[Mapping[str, Any]]) -> bool:
    duration = float(run[-1]["end"]) - float(run[0]["start"])
    return len(run) < SPLIT_MIN_WORDS and duration < SPLIT_MIN_SEC


def _split_at_turns(segment: dict[str, Any], turns: Sequence[SpeakerTurn]) -> list[dict[str, Any]]:
    words: list[Mapping[str, Any]] = list(segment.get("words") or [])
    if len(words) < 2:
        return [segment]

    # Runs of consecutive words held by one voice. A word no turn claims
    # stays with the voice currently speaking rather than opening a gap.
    runs: list[tuple[str | None, list[Mapping[str, Any]]]] = []
    for word in words:
        speaker = _word_speaker(word, turns)
        if speaker is None and runs:
            speaker = runs[-1][0]
        if runs and runs[-1][0] == speaker:
            runs[-1][1].append(word)
        else:
            runs.append((speaker, [word]))
    if len(runs) <= 1:
        return [segment]

    # Fold jitter-sized runs into the voice around them, then re-join runs
    # of the same voice that the folding made adjacent.
    merged: list[tuple[str | None, list[Mapping[str, Any]]]] = [runs[0]]
    for speaker, run in runs[1:]:
        if _is_jitter(run):
            merged[-1][1].extend(run)
        elif merged[-1][0] == speaker:
            merged[-1][1].extend(run)
        else:
            merged.append((speaker, run))
    if len(merged) > 1 and _is_jitter(merged[0][1]):
        first = merged.pop(0)
        merged[0] = (merged[0][0], list(first[1]) + merged[0][1])
    if len(merged) <= 1:
        return [segment]
    return children_from_word_runs(segment, [run for _speaker, run in merged])


def split_segments_at_turns(
    segments: Sequence[Mapping[str, Any]], turns: Sequence[SpeakerTurn]
) -> list[dict[str, Any]]:
    """Cut every segment whose words fall into different speakers' turns,
    at the word where the voice changes, and renumber ids.

    Whisper's segments (even after `segmentation.resegment`) follow the
    text, not the voices: one sentence-sized segment can hold the end of
    one speaker's remark and the start of the next speaker's answer. With
    the turns known, such a segment becomes two, so each carries one
    speaker -- the transcript itself then reflects the change of voice,
    not just a majority-vote label over a mixed segment. Segments without
    word timestamps pass through unchanged. Only ever applied to a
    transcript being *created*: ids change, and an existing meeting's
    `speakers.json` is keyed by them.
    """
    out: list[dict[str, Any]] = []
    for segment in segments:
        out.extend(_split_at_turns(dict(segment), turns))
    for new_id, seg in enumerate(out):
        seg["id"] = new_id
    return out


def normalize_labels(segments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rename raw diarizer labels to ``Speaker 1..N`` in order of first speech.

    The diarizer's own labels (``SPEAKER_00``...) are cluster ids in an
    order the caller cannot predict; numbering by first appearance means
    "Speaker 1" is always the first voice heard, which is what a reader
    scanning the transcript expects.
    """
    out, _mapping = normalize_labels_with_mapping(segments)
    return out


def normalize_labels_with_mapping(
    segments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """:func:`normalize_labels`, also answering the ``raw -> display`` map --
    the join key for anything else the diarizer said about a raw label
    (its voice embedding, for one)."""
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
    return out, mapping


def label_segments(
    segments: Sequence[Mapping[str, Any]], turns: Sequence[SpeakerTurn]
) -> tuple[list[dict[str, Any]], int, dict[str, str]]:
    """Assign and normalize in one pass; returns
    ``(segments, speaker_count, raw -> display label map)``."""
    labelled, mapping = normalize_labels_with_mapping(assign_speakers(segments, turns))
    speakers = {seg["speaker"] for seg in labelled if seg.get("speaker") is not None}
    return labelled, len(speakers), mapping
