"""Tests for the pure speaker-turn alignment (`diarization.py`).

Pure-function tests: no pyannote, no torch, no filesystem (FR-15).
"""

from __future__ import annotations

from typing import Any

import pytest

from transcription.diarization import (
    SpeakerTurn,
    assign_speakers,
    label_segments,
    normalize_labels,
)


def _seg(
    seg_id: int,
    start: float,
    end: float,
    text: str = "x",
    words: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    segment: dict[str, Any] = {"id": seg_id, "start": start, "end": end, "text": text}
    if words is not None:
        segment["words"] = words
    return segment


def test_a_turn_end_before_its_start_is_rejected() -> None:
    with pytest.raises(ValueError):
        SpeakerTurn(start=2.0, end=1.0, speaker="A")


def test_segments_are_attributed_by_temporal_overlap() -> None:
    turns = [
        SpeakerTurn(start=0.0, end=5.0, speaker="SPEAKER_00"),
        SpeakerTurn(start=5.0, end=10.0, speaker="SPEAKER_01"),
    ]
    segments = [_seg(0, 0.0, 4.0), _seg(1, 5.5, 9.0)]

    labelled = assign_speakers(segments, turns)

    assert labelled[0]["speaker"] == "SPEAKER_00"
    assert labelled[1]["speaker"] == "SPEAKER_01"


def test_a_segment_straddling_two_turns_goes_to_the_majority_holder() -> None:
    turns = [
        SpeakerTurn(start=0.0, end=3.0, speaker="A"),
        SpeakerTurn(start=3.0, end=10.0, speaker="B"),
    ]
    # 1 second inside A's turn, 4 seconds inside B's.
    segments = [_seg(0, 2.0, 7.0)]

    labelled = assign_speakers(segments, turns)

    assert labelled[0]["speaker"] == "B"


def test_word_timestamps_outvote_the_segment_envelope() -> None:
    # The segment envelope [0, 10] overlaps B's turn more, but every actual
    # word lies inside A's turn -- the words are where the speech is.
    turns = [
        SpeakerTurn(start=0.0, end=3.0, speaker="A"),
        SpeakerTurn(start=3.0, end=10.0, speaker="B"),
    ]
    words = [
        {"word": " one", "start": 0.5, "end": 1.0},
        {"word": " two", "start": 1.2, "end": 1.8},
        {"word": " three", "start": 2.0, "end": 2.8},
    ]
    segments = [_seg(0, 0.0, 10.0, words=words)]

    labelled = assign_speakers(segments, turns)

    assert labelled[0]["speaker"] == "A"


def test_a_segment_in_a_silence_gap_snaps_to_the_nearest_turn() -> None:
    # Diarization trims silence harder than whisper; a segment falling into
    # the gap still belongs to whoever spoke nearest (within tolerance).
    turns = [SpeakerTurn(start=0.0, end=4.0, speaker="A")]
    segments = [_seg(0, 4.5, 5.5)]  # midpoint 5.0, one second past A's end

    labelled = assign_speakers(segments, turns)

    assert labelled[0]["speaker"] == "A"


def test_a_segment_far_from_any_turn_stays_unattributed() -> None:
    turns = [SpeakerTurn(start=0.0, end=1.0, speaker="A")]
    segments = [_seg(0, 30.0, 31.0)]

    labelled = assign_speakers(segments, turns)

    assert labelled[0]["speaker"] is None


def test_no_turns_leaves_every_segment_unattributed() -> None:
    segments = [_seg(0, 0.0, 1.0), _seg(1, 1.0, 2.0)]

    labelled = assign_speakers(segments, [])

    assert [seg["speaker"] for seg in labelled] == [None, None]


def test_assign_speakers_copies_rather_than_mutating() -> None:
    segments = [_seg(0, 0.0, 1.0)]

    assign_speakers(segments, [SpeakerTurn(start=0.0, end=1.0, speaker="A")])

    assert "speaker" not in segments[0]


def test_labels_are_normalized_in_order_of_first_speech() -> None:
    # pyannote's cluster ids arrive in arbitrary order; "Speaker 1" must be
    # the first voice heard, not the lowest cluster id.
    segments = [
        {"id": 0, "speaker": "SPEAKER_01"},
        {"id": 1, "speaker": "SPEAKER_00"},
        {"id": 2, "speaker": "SPEAKER_01"},
        {"id": 3, "speaker": None},
    ]

    renamed = normalize_labels(segments)

    assert [seg["speaker"] for seg in renamed] == ["Speaker 1", "Speaker 2", "Speaker 1", None]


def test_label_segments_reports_the_distinct_speaker_count() -> None:
    turns = [
        SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_01"),
        SpeakerTurn(start=1.0, end=2.0, speaker="SPEAKER_00"),
    ]
    segments = [_seg(0, 0.0, 0.9), _seg(1, 1.1, 1.9), _seg(2, 50.0, 51.0)]

    labelled, speaker_count, mapping = label_segments(segments, turns)

    assert speaker_count == 2
    assert labelled[0]["speaker"] == "Speaker 1"
    assert labelled[1]["speaker"] == "Speaker 2"
    assert labelled[2]["speaker"] is None
    # The raw -> display map is the join key for per-label extras (voice
    # embeddings); first speech order, not cluster-id order.
    assert mapping == {"SPEAKER_01": "Speaker 1", "SPEAKER_00": "Speaker 2"}


def test_overlapping_turns_split_by_who_holds_more_of_the_segment() -> None:
    # Overlapped speech: both turns cover the segment, but B covers more.
    turns = [
        SpeakerTurn(start=0.0, end=2.0, speaker="A"),
        SpeakerTurn(start=1.0, end=6.0, speaker="B"),
    ]
    segments = [_seg(0, 0.5, 5.0)]

    labelled = assign_speakers(segments, turns)

    assert labelled[0]["speaker"] == "B"
