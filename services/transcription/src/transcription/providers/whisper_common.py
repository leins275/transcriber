"""What every whisper engine shares: the language rule, the VAD, the
segment shape and the finishing passes.

faster-whisper does its own silence skipping (`vad_filter=True`) and maps
the timestamps back itself; the other engines (mlx-whisper on Apple
Silicon, OpenVINO on an Intel NPU) decode whatever they are handed, so this
module reproduces that around them with faster-whisper's own Silero VAD,
which the base install carries on every platform:

1. :func:`speech_batches` finds the speech in the waveform and groups it
   into batches of at most ``max_sec`` of speech (one static 30 s window
   for the NPU, ten minutes for the Apple GPU) -- the silence between
   turns is never decoded, which is what keeps whisper from transcribing
   silence (the measured lesson of the whisper.cpp branch);
2. the engine decodes each batch's concatenated speech, reporting progress
   and checking the cancel token between batches;
3. :func:`restore_times` maps the batch-relative timestamps (segments and
   words) back onto the recording's timeline through faster-whisper's own
   `SpeechTimestampsMap`;
4. :func:`finish` runs the hallucination filters and the utterance
   re-segmentation every engine's transcript goes through.

The constrained language rule (`constrain_language`) is the one
`local_whisper.py` applies: ru/en/tr and nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
from faster_whisper.vad import (  # type: ignore[import-untyped]
    SpeechTimestampsMap,
    VadOptions,
    collect_chunks,
    get_speech_timestamps,
)

from transcription.filters import apply_filters
from transcription.segmentation import resegment

# `decode_audio` resamples to 16 kHz (whisper's fixed model rate).
SAMPLE_RATE = 16_000

# The operator's language universe; auto-detection chooses between these
# and nothing else, so a mis-detected outside language never reaches the
# decoder.
DECODE_LANGUAGES = ("ru", "en", "tr")
DEFAULT_LANGUAGE = "en"
NO_EVIDENCE_PROBABILITY = 0.0

# How much of the recording the detection pass VAD-scans (see
# `local_whisper._DETECTION_PREFIX_SEC` for the measured reasoning).
DETECTION_PREFIX_SEC = 600

# What the VAD pads around each speech chunk, as the faster-whisper
# provider sets it.
SPEECH_PAD_MS = 400


def vad_parameters(config: Any) -> dict[str, Any]:
    """The VAD settings every engine shares (the faster-whisper provider's)."""
    return {
        "min_silence_duration_ms": int(getattr(config, "vad_min_silence_ms", 500)),
        "speech_pad_ms": SPEECH_PAD_MS,
    }


def constrain_language(all_language_probs: Any) -> tuple[str, float]:
    """Argmax over `DECODE_LANGUAGES` alone, from the model's full language
    distribution -- a list of ``(code, probability)`` pairs or a mapping.
    Returns the chosen language and its probability (`0.0` when no target
    was reported at all)."""
    if isinstance(all_language_probs, Mapping):
        pairs: Any = all_language_probs.items()
    else:
        pairs = all_language_probs or []
    candidates = [
        (str(language), float(probability))
        for language, probability in pairs
        if language in DECODE_LANGUAGES
    ]
    if not candidates:
        return DEFAULT_LANGUAGE, NO_EVIDENCE_PROBABILITY
    return max(candidates, key=lambda item: item[1])


def detection_audio(waveform: Any, parameters: dict[str, Any]) -> Any:
    """The speech in the recording's first `DETECTION_PREFIX_SEC`, for the
    language detector; the raw prefix when the VAD hears nothing."""
    prefix = waveform[: DETECTION_PREFIX_SEC * SAMPLE_RATE]
    speech_chunks = get_speech_timestamps(prefix, VadOptions(**parameters))
    if not speech_chunks:
        return prefix
    audio_chunks, _ = collect_chunks(prefix, speech_chunks)
    return np.concatenate(audio_chunks, axis=0)


@dataclass(frozen=True)
class Batch:
    """One engine call's worth of speech: the concatenated chunks and the
    chunk list that maps its timeline back onto the recording's."""

    audio: Any
    chunks: list[dict[str, Any]]

    @property
    def seconds(self) -> float:
        return float(len(self.audio)) / SAMPLE_RATE


def speech_batches(waveform: Any, *, parameters: dict[str, Any], max_sec: float) -> list[Batch]:
    """The recording's speech, in batches of at most ``max_sec`` seconds.

    A single chunk longer than ``max_sec`` is split at ``max_sec`` (the
    VAD's own `max_speech_duration_s` would do the same, less predictably).
    An empty list means the VAD heard nothing: the caller returns an empty
    transcript rather than decoding silence.
    """
    speech_chunks = get_speech_timestamps(waveform, VadOptions(**parameters))
    if not speech_chunks:
        return []
    max_samples = max(int(max_sec * SAMPLE_RATE), 1)
    bounded: list[dict[str, Any]] = []
    for chunk in speech_chunks:
        start, end = int(chunk["start"]), int(chunk["end"])
        while end - start > max_samples:
            bounded.append({"start": start, "end": start + max_samples})
            start += max_samples
        if end > start:
            bounded.append({"start": start, "end": end})

    batches: list[Batch] = []
    current: list[dict[str, Any]] = []
    current_samples = 0
    for chunk in bounded:
        length = int(chunk["end"]) - int(chunk["start"])
        if current and current_samples + length > max_samples:
            batches.append(_batch(waveform, current))
            current, current_samples = [], 0
        current.append(chunk)
        current_samples += length
    if current:
        batches.append(_batch(waveform, current))
    return batches


def _batch(waveform: Any, chunks: list[dict[str, Any]]) -> Batch:
    audio_chunks, _ = collect_chunks(waveform, chunks)
    return Batch(audio=np.concatenate(audio_chunks, axis=0), chunks=list(chunks))


def restore_times(segments: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> None:
    """Map a batch's segment (and word) timestamps back onto the recording,
    in place."""
    ts_map = SpeechTimestampsMap(chunks, SAMPLE_RATE)
    for segment in segments:
        words = segment.get("words")
        if words:
            for word in words:
                word["start"] = ts_map.get_original_time(float(word["start"]))
                word["end"] = ts_map.get_original_time(float(word["end"]), is_end=True)
            segment["start"] = words[0]["start"]
            segment["end"] = words[-1]["end"]
        else:
            segment["start"] = ts_map.get_original_time(float(segment["start"]))
            segment["end"] = ts_map.get_original_time(float(segment["end"]), is_end=True)


def map_segment(
    *,
    new_id: int,
    start: float,
    end: float,
    text: str,
    avg_logprob: float | None = None,
    no_speech_prob: float | None = None,
    compression_ratio: float | None = None,
    words: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The FR-6 segment shape every engine writes."""
    mapped: dict[str, Any] = {
        "id": new_id,
        "start": float(start),
        "end": float(end),
        "text": text,
        "avg_logprob": avg_logprob,
        "no_speech_prob": no_speech_prob,
        "compression_ratio": compression_ratio,
    }
    if words is not None:
        mapped["words"] = [
            {
                "word": str(word.get("word", "")),
                "start": float(word["start"]),
                "end": float(word["end"]),
                "probability": word.get("probability"),
            }
            for word in words
        ]
    return mapped


def finish(
    raw_segments: list[dict[str, Any]],
    *,
    filter_hallucinations: bool,
    word_timestamps: bool,
    resegment_gap_sec: float,
) -> tuple[list[dict[str, Any]], str, int]:
    """The finishing passes: filters first (the confidence stats belong to
    the segment they were measured on), then the utterance split. Returns
    the segments, the joined text and how many segments the filters
    dropped."""
    renumbered = [dict(segment, id=index) for index, segment in enumerate(raw_segments)]
    kept, n_filtered = apply_filters(renumbered, enabled=filter_hallucinations)
    if word_timestamps:
        segments_out: list[dict[str, Any]] = resegment(kept, gap_sec=resegment_gap_sec)
    else:
        segments_out = [dict(segment) for segment in kept]
    text = "".join(str(segment["text"]) for segment in segments_out)
    return segments_out, text, n_filtered
