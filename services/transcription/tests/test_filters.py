"""Tests for silence and hallucination filters (FR-12)."""

from transcription.filters import apply_filters, is_low_confidence, looks_like_silence


def test_is_low_confidence_true_when_both_thresholds_exceeded() -> None:
    assert is_low_confidence({"no_speech_prob": 0.7, "avg_logprob": -1.2}) is True


def test_is_low_confidence_false_when_only_no_speech_exceeded() -> None:
    assert is_low_confidence({"no_speech_prob": 0.5, "avg_logprob": -1.2}) is False


def test_is_low_confidence_true_on_compression_ratio_alone_strict_gt() -> None:
    assert is_low_confidence({"compression_ratio": 2.5}) is True


def test_is_low_confidence_false_on_compression_ratio_boundary() -> None:
    assert is_low_confidence({"compression_ratio": 2.4}) is False


def test_is_low_confidence_true_on_hard_logprob_threshold_alone() -> None:
    assert is_low_confidence({"avg_logprob": -1.35}) is True


def test_is_low_confidence_false_on_benign_logprob_alone() -> None:
    assert is_low_confidence({"avg_logprob": -1.25, "no_speech_prob": 0.1}) is False


def test_looks_like_silence_empty_list_is_true() -> None:
    assert looks_like_silence([]) is True


def test_looks_like_silence_true_when_every_segment_qualifies() -> None:
    segments = [
        {"no_speech_prob": 0.7, "avg_logprob": -1.2},
        {"no_speech_prob": 0.9, "avg_logprob": -2.0},
    ]
    assert looks_like_silence(segments) is True


def test_looks_like_silence_false_when_any_segment_does_not_qualify() -> None:
    segments = [
        {"no_speech_prob": 0.7, "avg_logprob": -1.2},
        {"no_speech_prob": 0.1, "avg_logprob": -0.1},
    ]
    assert looks_like_silence(segments) is False


def test_apply_filters_enabled_drops_all_silence_segments() -> None:
    segments = [
        {"id": 0, "text": "junk one", "no_speech_prob": 0.7, "avg_logprob": -1.2},
        {"id": 1, "text": "junk two", "no_speech_prob": 0.9, "avg_logprob": -2.0},
    ]
    kept, n_filtered = apply_filters(segments, enabled=True)
    assert kept == []
    assert n_filtered == len(segments)
    assert "".join(seg["text"] for seg in kept) == ""


def test_apply_filters_disabled_is_a_live_noop() -> None:
    segments = [
        {"id": 0, "text": "junk", "no_speech_prob": 0.7, "avg_logprob": -1.2},
    ]
    kept, n_filtered = apply_filters(segments, enabled=False)
    assert kept == segments
    assert n_filtered == 0


def test_apply_filters_keeps_order_and_renumbers_ids_contiguously() -> None:
    segments = [
        {"id": 0, "text": "good one", "no_speech_prob": 0.1, "avg_logprob": -0.1},
        {"id": 1, "text": "bad", "no_speech_prob": 0.9, "avg_logprob": -2.0},
        {"id": 2, "text": "good two", "no_speech_prob": 0.1, "avg_logprob": -0.1},
    ]
    kept, n_filtered = apply_filters(segments, enabled=True)
    assert n_filtered == 1
    assert [seg["text"] for seg in kept] == ["good one", "good two"]
    assert [seg["id"] for seg in kept] == [0, 1]
