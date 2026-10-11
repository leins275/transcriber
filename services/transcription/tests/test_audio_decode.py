"""The service's own audio decoder (`providers/audio.py`): a damaged packet
is skipped as silence, the rest of the recording still arrives, and the
timeline survives. Model-free: PyAV writes the fixtures."""

from __future__ import annotations

import logging
import math
import struct
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest

from transcription.providers.audio import decode_audio, decode_samples

RATE = 48_000
SECONDS = 6.0
TONE_HZ = 440


def _write_tone_mp4(path: Path, seconds: float = SECONDS) -> None:
    """An AAC-in-mp4 recording of one steady tone (what the vault holds)."""
    with av.open(str(path), "w") as out:
        stream = out.add_stream("aac", rate=RATE)
        total = int(seconds * RATE)
        written = 0
        while written < total:
            count = min(1024, total - written)
            frame = av.AudioFrame(format="fltp", layout="mono", samples=count)
            frame.sample_rate = RATE
            data = b"".join(
                struct.pack("<f", 0.3 * math.sin(2 * math.pi * TONE_HZ * (written + k) / RATE))
                for k in range(count)
            )
            plane = frame.planes[0]
            plane.update(data + bytes(plane.buffer_size - len(data)))
            frame.pts = written
            frame.time_base = Fraction(1, RATE)
            for packet in stream.encode(frame):
                out.mux(packet)
            written += count
        for packet in stream.encode(None):
            out.mux(packet)


def _damage_packets(path: Path, *, start_sec: float, end_sec: float) -> int:
    """Overwrite every audio packet between the two times with garbage the
    AAC decoder refuses; returns how many."""
    spans: list[tuple[int, int]] = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        for packet in container.demux(stream):
            if packet.dts is None or packet.pos is None or packet.pos < 0 or packet.pts is None:
                continue
            at = float(packet.pts * stream.time_base)
            if start_sec <= at < end_sec:
                spans.append((packet.pos, packet.size))
    data = bytearray(path.read_bytes())
    for pos, size in spans:
        data[pos : pos + size] = b"\xff" * size
    path.write_bytes(bytes(data))
    return len(spans)


def _rms(samples: np.ndarray, start_sec: float, end_sec: float, rate: int = 16_000) -> float:
    window = samples[int(start_sec * rate) : int(end_sec * rate)]
    return float(np.sqrt(np.mean(window.astype(np.float64) ** 2))) if len(window) else 0.0


def test_a_clean_recording_decodes_whole_as_float32_mono(tmp_path: Path) -> None:
    path = tmp_path / "source.mp4"
    _write_tone_mp4(path)

    samples = decode_audio(path)

    assert samples.dtype == np.float32
    assert samples.ndim == 1
    assert abs(len(samples) / 16_000 - SECONDS) < 0.1
    assert _rms(samples, 1.0, 2.0) > 0.15


def test_damaged_packets_are_skipped_as_silence_and_the_rest_still_arrives(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The regression behind "0 speakers on a 42-minute meeting": the stock
    decoder ends at the first packet FFmpeg rejects, so a recording damaged
    4.5 s in "decoded" to 4.5 s. Every packet is decoded on its own now, and
    a refused one leaves its duration as silence so the audio after it keeps
    its timestamps."""
    path = tmp_path / "source.mp4"
    _write_tone_mp4(path)
    damaged = _damage_packets(path, start_sec=2.0, end_sec=2.2)
    assert damaged >= 5

    with caplog.at_level(logging.WARNING, logger="transcription"):
        samples = decode_audio(path)

    # Whole length kept...
    assert abs(len(samples) / 16_000 - SECONDS) < 0.1
    # ...the tone is still there on both sides of the damage, on time...
    assert _rms(samples, 1.0, 1.9) > 0.15
    assert _rms(samples, 3.0, 5.0) > 0.15
    # ...and the damaged stretch itself is (near) silence rather than noise.
    assert _rms(samples, 2.05, 2.15) < 0.05
    # The skip is on the record.
    events = [record for record in caplog.records if "undecodable" in record.getMessage()]
    assert len(events) == 1
    assert events[0].packets == damaged  # type: ignore[attr-defined]


def test_a_recording_no_packet_of_which_decodes_is_a_decode_failure(tmp_path: Path) -> None:
    """Tolerance is per packet; a file the decoder cannot read at all still
    fails, so the job is classified `audio_decode` rather than transcribed
    as silence."""
    path = tmp_path / "source.mp4"
    _write_tone_mp4(path)
    _damage_packets(path, start_sec=-1.0, end_sec=SECONDS + 1.0)

    with pytest.raises(av.FFmpegError):
        decode_audio(path)


def test_decode_samples_is_the_same_decoder_at_the_asked_rate(tmp_path: Path) -> None:
    path = tmp_path / "source.mp4"
    _write_tone_mp4(path, seconds=1.0)

    samples = decode_samples(path, sample_rate=8_000)

    assert samples.dtype == np.float32
    assert abs(len(samples) / 8_000 - 1.0) < 0.1
