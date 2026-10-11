"""Decoding a recording to samples, for the transcription and the diarizer.

Both consumers must see the *same* audio, so both decode through this one
door: PyAV, the FFmpeg bundled in faster-whisper's own wheel. Provider
library names stay confined to this package (FR-4, `test_attribution.py`).

Why not faster-whisper's own ``decode_audio``: it pulls frames from one
``container.decode()`` generator, and a generator that raised is finished
-- so the first packet FFmpeg's decoder rejects ends the whole decode
there, silently. A screen recording with a handful of damaged AAC packets
(measured on the operator's vault: 50 of 119,000 packets, the first 4.5 s
in) then "decodes" to 4.5 s of a 42-minute meeting, the transcript stops
there and the diarizer finds nobody at all. Here every packet is decoded on
its own: one the decoder refuses is skipped and its duration is kept as
silence, so what follows stays on the recording's timeline, and the skip is
logged. A file whose packets *all* fail is still a decode failure.
"""

from __future__ import annotations

import logging
from fractions import Fraction
from pathlib import Path
from typing import Any

logger = logging.getLogger("transcription")

_DEFAULT_SAMPLE_RATE = 16_000


def decode_audio(source: str | Path, sampling_rate: int = _DEFAULT_SAMPLE_RATE) -> Any:
    """The recording's first audio stream as a float32 mono numpy array at
    ``sampling_rate`` -- the shape faster-whisper and pyannote both take.

    Lazy imports: the decoder pulls in the provider library's own
    dependencies, which the service never imports before a job actually
    needs them (NFR-1).
    """
    import av  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    path = str(source)
    resampler = av.AudioResampler(format="flt", layout="mono", rate=sampling_rate)
    chunks: list[Any] = []
    decoded = 0
    skipped = 0
    skipped_sec = 0.0
    first_bad_sec: float | None = None
    first_error: Exception | None = None

    with av.open(path, mode="r", metadata_errors="ignore") as container:
        stream = container.streams.audio[0]
        time_base = stream.time_base or Fraction(1, stream.rate or sampling_rate)
        for packet in container.demux(stream):
            try:
                frames = packet.decode()
            except av.FFmpegError as exc:  # one packet's problem, not the file's
                skipped += 1
                first_error = first_error or exc
                lost = _packet_seconds(packet, stream, time_base)
                if first_bad_sec is None:
                    first_bad_sec = (
                        float(packet.pts * time_base) if packet.pts is not None else None
                    )
                if lost > 0:
                    skipped_sec += lost
                    chunks.append(np.zeros(int(round(lost * sampling_rate)), dtype=np.float32))
                continue
            for frame in frames:
                decoded += 1
                # The resampler would otherwise pad or drop samples to honour
                # timestamps; the gaps are already filled above.
                frame.pts = None
                for resampled in resampler.resample(frame):
                    chunks.append(resampled.to_ndarray().reshape(-1))
        for resampled in resampler.resample(None):
            chunks.append(resampled.to_ndarray().reshape(-1))

    if decoded == 0 and first_error is not None:
        raise first_error
    if skipped:
        logger.warning(
            "skipped undecodable audio packets",
            extra={
                "event": "audio_packets_skipped",
                "path": path,
                "packets": skipped,
                "seconds": round(skipped_sec, 3),
                "first_at_sec": first_bad_sec,
                "error": str(first_error),
            },
        )
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32, copy=False)


def _packet_seconds(packet: Any, stream: Any, time_base: Fraction) -> float:
    """How long a packet's audio lasts: its own duration when the container
    says, else one codec frame (AAC's 1024 samples), else nothing."""
    if packet.duration:
        return float(packet.duration * time_base)
    frame_size = int(getattr(stream.codec_context, "frame_size", 0) or 0)
    rate = int(stream.rate or 0)
    if frame_size and rate:
        return frame_size / rate
    return 0.0


def decode_samples(audio_path: Path, *, sample_rate: int) -> Any:
    """``decode_audio`` under the diarizer's keyword spelling."""
    return decode_audio(audio_path, sampling_rate=sample_rate)
