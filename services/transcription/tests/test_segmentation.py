"""Tests for utterance-level re-segmentation (segmentation.py)."""

from __future__ import annotations

from typing import Any

from transcription.segmentation import resegment


def _word(text: str, start: float, end: float) -> dict[str, Any]:
    return {"word": text, "start": start, "end": end, "probability": 0.9}


def _segment(
    *,
    seg_id: int = 0,
    start: float,
    end: float,
    text: str,
    words: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": seg_id,
        "start": start,
        "end": end,
        "text": text,
        "avg_logprob": -0.2,
        "no_speech_prob": 0.05,
        "compression_ratio": 1.1,
        "words": words,
    }


def test_splits_at_sentence_ending_punctuation() -> None:
    words = [
        _word(" Привет.", 0.0, 0.4),
        _word(" Как", 0.5, 0.7),
        _word(" дела?", 0.7, 1.0),
        _word(" Нормально.", 1.1, 1.6),
    ]
    segment = _segment(start=0.0, end=1.6, text=" Привет. Как дела? Нормально.", words=words)

    result = resegment([segment])

    assert [seg["text"] for seg in result] == [" Привет.", " Как дела?", " Нормально."]
    assert [seg["id"] for seg in result] == [0, 1, 2]
    assert result[0]["start"] == 0.0
    assert result[0]["end"] == 0.4
    assert result[1]["start"] == 0.5
    assert result[2]["end"] == 1.6


def test_splits_at_long_word_gap_without_punctuation() -> None:
    words = [
        _word(" ну", 0.0, 0.2),
        _word(" да", 0.2, 0.4),
        _word(" хорошо", 1.5, 2.0),  # 1.1 s pause before this word
    ]
    segment = _segment(start=0.0, end=2.0, text=" ну да хорошо", words=words)

    result = resegment([segment], gap_sec=0.6)

    assert [seg["text"] for seg in result] == [" ну да", " хорошо"]
    assert result[0]["end"] == 0.4
    assert result[1]["start"] == 1.5


def test_gap_below_threshold_does_not_split() -> None:
    words = [
        _word(" ну", 0.0, 0.2),
        _word(" да", 0.5, 0.7),  # 0.3 s pause: below the threshold
    ]
    segment = _segment(start=0.0, end=0.7, text=" ну да", words=words)

    result = resegment([segment], gap_sec=0.6)

    assert len(result) == 1
    # An unsplit segment is the original object, text and timestamps intact.
    assert result[0]["text"] == " ну да"
    assert result[0]["start"] == 0.0
    assert result[0]["end"] == 0.7


def test_segment_without_words_passes_through() -> None:
    segment = _segment(start=0.0, end=3.0, text=" hello there", words=None)

    result = resegment([segment])

    assert len(result) == 1
    assert result[0]["text"] == " hello there"


def test_children_inherit_confidence_fields() -> None:
    words = [
        _word(" One.", 0.0, 0.5),
        _word(" Two.", 0.6, 1.0),
    ]
    segment = _segment(start=0.0, end=1.0, text=" One. Two.", words=words)

    result = resegment([segment])

    assert len(result) == 2
    for child in result:
        assert child["avg_logprob"] == -0.2
        assert child["no_speech_prob"] == 0.05
        assert child["compression_ratio"] == 1.1
        assert child["words"]


def test_ids_renumbered_across_multiple_segments() -> None:
    first = _segment(
        seg_id=0,
        start=0.0,
        end=1.0,
        text=" One. Two.",
        words=[_word(" One.", 0.0, 0.5), _word(" Two.", 0.6, 1.0)],
    )
    second = _segment(seg_id=1, start=1.5, end=2.0, text=" three", words=None)

    result = resegment([first, second])

    assert [seg["id"] for seg in result] == [0, 1, 2]
    assert result[2]["text"] == " three"


def test_trailing_punctuation_on_last_word_yields_no_empty_piece() -> None:
    words = [
        _word(" Done.", 0.0, 0.5),
    ]
    segment = _segment(start=0.0, end=0.5, text=" Done.", words=words)

    result = resegment([segment])

    assert len(result) == 1
    assert result[0]["text"] == " Done."
