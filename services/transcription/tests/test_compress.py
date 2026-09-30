"""Tests for `compress.py`: the chain's final, video-compressing stage.

Everything runs on tiny synthetic recordings PyAV writes into a tempdir
(two seconds of noise at 128x128, lossless so it is comfortably above the
"already small" line), always through the CPU encoder -- no GPU, no model,
no network, well under a few seconds.
"""

from __future__ import annotations

import math
import os
import struct
from fractions import Fraction
from pathlib import Path

import av
import pytest

from transcription import compress
from transcription.compress import (
    DEFAULT_ENCODERS,
    NVENC_H264,
    X264,
    CompressOutcome,
    EncoderSpec,
    compress_recording,
)
from transcription.errors import ErrorKind, ServiceError

MEETING_NAME = "260101 - Planning"
FPS = 30
SAMPLE_RATE = 48_000

# CRF 23 shrinks lossless noise several times over; `ultrafast` keeps the
# suite quick. Never NVENC here: the suite must pass on any machine.
FAST_X264 = EncoderSpec("libx264", {"crf": "23", "preset": "ultrafast"})
# An x264 that refuses to open (an unknown preset), standing in for an
# NVENC whose runtime is missing.
BROKEN_X264 = EncoderSpec("libx264", {"preset": "no_such_preset"})


def make_recording(
    tmp_path: Path,
    *,
    ext: str = "mkv",
    audio: str | None = "aac",
    video: bool = True,
    seconds: float = 2.0,
) -> Path:
    """`<tmp>/ELS/<meeting>/source.<ext>`: noisy video and/or a sine tone."""
    meeting = tmp_path / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True, exist_ok=True)
    path = meeting / f"source.{ext}"
    with av.open(str(path), "w") as out:
        video_stream = None
        if video:
            video_stream = out.add_stream(
                "libx264", rate=FPS, options={"crf": "0", "preset": "ultrafast"}
            )
            video_stream.width = video_stream.height = 128
            video_stream.pix_fmt = "yuv420p"
            video_stream.time_base = Fraction(1, FPS)
        audio_stream = None
        if audio:
            audio_stream = out.add_stream(audio, rate=SAMPLE_RATE)
        if video_stream is not None:
            for index in range(int(seconds * FPS)):
                frame = av.VideoFrame(128, 128, "yuv420p")
                for plane in frame.planes:
                    plane.update(os.urandom(plane.buffer_size))
                frame.pts = index
                frame.time_base = Fraction(1, FPS)
                for packet in video_stream.encode(frame):
                    out.mux(packet)
            for packet in video_stream.encode(None):
                out.mux(packet)
        if audio_stream is not None:
            _write_tone(out, audio_stream, seconds)
    return path


def _write_tone(out: av.container.OutputContainer, stream: av.AudioStream, seconds: float) -> None:
    fmt = "s16" if stream.codec_context.name.startswith("pcm") else "fltp"
    total = int(seconds * SAMPLE_RATE)
    written = 0
    while written < total:
        count = min(1024, total - written)
        frame = av.AudioFrame(format=fmt, layout="mono", samples=count)
        frame.sample_rate = SAMPLE_RATE
        samples = (math.sin(2 * math.pi * 440 * (written + k) / SAMPLE_RATE) for k in range(count))
        if fmt == "s16":
            data = b"".join(struct.pack("<h", int(8000 * s)) for s in samples)
        else:
            data = b"".join(struct.pack("<f", 0.3 * s) for s in samples)
        plane = frame.planes[0]
        plane.update(data + bytes(plane.buffer_size - len(data)))
        frame.pts = written
        frame.time_base = Fraction(1, SAMPLE_RATE)
        for packet in stream.encode(frame):
            out.mux(packet)
        written += count
    for packet in stream.encode(None):
        out.mux(packet)


def _duration(path: Path) -> float:
    with av.open(str(path)) as container:
        assert container.duration is not None
        return container.duration / av.time_base


def _audio_codec(path: Path) -> str | None:
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return None
        return container.streams.audio[0].codec_context.name


def _source_files(meeting: Path) -> list[str]:
    return sorted(p.name for p in meeting.iterdir() if p.stem == "source")


def _leftovers(meeting: Path) -> list[str]:
    return sorted(p.name for p in meeting.iterdir() if p.name.startswith("compress."))


def _run(source: Path, **overrides: object) -> CompressOutcome:
    kwargs: dict[str, object] = {"on_progress": lambda _fraction: None, "encoders": (FAST_X264,)}
    kwargs.update(overrides)
    return compress_recording(source, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------- replacing


def test_a_video_is_replaced_by_a_smaller_mp4_with_the_same_duration(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")
    before = source.stat().st_size
    duration = _duration(source)

    outcome = _run(source)

    meeting = source.parent
    assert outcome.replaced is True
    assert outcome.encoder == "libx264"
    assert outcome.warning is None
    assert _source_files(meeting) == ["source.mp4"]
    assert _leftovers(meeting) == []
    assert outcome.path == meeting / "source.mp4"
    assert outcome.before_bytes == before
    assert outcome.after_bytes == outcome.path.stat().st_size < before * 0.85
    assert abs(_duration(outcome.path) - duration) <= 1.0


def test_an_mp4_source_is_replaced_in_place(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mp4")
    before = source.stat().st_size

    outcome = _run(source)

    assert outcome.replaced is True
    assert outcome.path == source
    assert _source_files(source.parent) == ["source.mp4"]
    assert source.stat().st_size < before


def test_the_manifest_carries_the_outcome(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")

    manifest = _run(source).as_manifest()

    assert manifest["replaced"] is True
    assert manifest["encoder"] == "libx264"
    assert manifest["audio"] == "copy"
    assert manifest["path"].endswith("source.mp4")
    assert manifest["before_bytes"] > manifest["after_bytes"] > 0
    assert manifest["warning"] is None


# ---------------------------------------------------------------- audio


def test_aac_audio_is_copied(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv", audio="aac")

    outcome = _run(source)

    assert outcome.audio == "copy"
    assert _audio_codec(outcome.path) == "aac"


def test_pcm_audio_is_re_encoded_to_aac(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mov", audio="pcm_s16le")

    outcome = _run(source)

    assert outcome.replaced is True
    assert outcome.audio == "aac"
    assert _audio_codec(outcome.path) == "aac"


def test_a_silent_video_is_compressed_without_an_audio_stream(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv", audio=None)

    outcome = _run(source)

    assert outcome.replaced is True
    assert outcome.audio is None
    assert _audio_codec(outcome.path) is None


# ------------------------------------------------------------- skipping


def test_an_audio_only_extension_is_skipped_without_opening_the_file(tmp_path: Path) -> None:
    meeting = tmp_path / "ELS" / MEETING_NAME
    meeting.mkdir(parents=True)
    source = meeting / "source.m4a"
    source.write_bytes(b"not even a real container")

    outcome = _run(source)

    assert outcome.replaced is False
    assert outcome.path == source
    assert outcome.warning == "audio-only recording, nothing to compress"
    assert source.read_bytes() == b"not even a real container"


def test_a_container_without_a_video_stream_is_skipped(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv", video=False)

    outcome = _run(source)

    assert outcome.replaced is False
    assert outcome.warning is not None and "no video stream" in outcome.warning
    assert _source_files(source.parent) == ["source.mkv"]


def test_a_low_bitrate_source_is_kept(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")
    before = source.read_bytes()

    outcome = _run(source, min_source_kbps=10**9)

    assert outcome.replaced is False
    assert outcome.warning is not None and outcome.warning.startswith("already ")
    assert source.read_bytes() == before


# --------------------------------------------------- keeping the original


def test_an_insufficient_gain_keeps_the_original_and_removes_the_temp(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")
    before = source.read_bytes()

    outcome = _run(source, min_gain=0.99)

    assert outcome.replaced is False
    assert outcome.warning is not None and "under 99%" in outcome.warning
    assert source.read_bytes() == before
    assert _source_files(source.parent) == ["source.mkv"]
    assert _leftovers(source.parent) == []


def test_a_failed_verification_keeps_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_recording(tmp_path, ext="mkv")
    before = source.read_bytes()
    monkeypatch.setattr(compress, "_verify", lambda *_args, **_kw: "the output is suspect")

    outcome = _run(source)

    assert outcome.replaced is False
    assert outcome.warning == "video kept as-is: the output is suspect"
    assert source.read_bytes() == before
    assert _leftovers(source.parent) == []


def test_an_unknown_encoder_keeps_the_original_and_sweeps_the_temp(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")
    before = source.read_bytes()

    outcome = _run(source, encoders=(EncoderSpec("no_such_codec", {}),))

    assert outcome.replaced is False
    assert outcome.warning is not None
    assert outcome.warning.startswith("video kept as-is: no encoder could run")
    assert "no_such_codec" in outcome.warning
    assert source.read_bytes() == before
    assert _leftovers(source.parent) == []


def test_an_encoder_that_will_not_open_falls_through_to_the_next(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")

    outcome = _run(source, encoders=(BROKEN_X264, FAST_X264))

    assert outcome.replaced is True
    assert outcome.encoder == "libx264"
    assert _source_files(source.parent) == ["source.mp4"]
    assert _leftovers(source.parent) == []


def test_the_default_order_is_nvenc_then_x264() -> None:
    assert DEFAULT_ENCODERS == (NVENC_H264, X264)
    assert NVENC_H264.zero_bit_rate is True
    assert X264.options == {"crf": "23", "preset": "medium"}


def test_stale_partials_are_swept_before_encoding(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")
    stale = source.with_name("compress.partial.mp4")
    stale.write_bytes(b"left over from a crash")
    replaced = source.with_name("compress.replaced.mkv")
    replaced.write_bytes(b"also left over")

    outcome = _run(source, min_source_kbps=10**9)

    assert outcome.replaced is False
    assert _leftovers(source.parent) == []


# --------------------------------------------------- progress and cancel


def test_progress_is_monotonic_throttled_and_ends_at_one(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")
    seen: list[float] = []

    outcome = _run(source, on_progress=seen.append)

    assert outcome.replaced is True
    assert seen, "the encode reported no progress"
    assert seen == sorted(seen)
    assert all(0.0 < value <= 1.0 for value in seen)
    assert seen[-1] == 1.0
    steps = [b - a for a, b in zip(seen, seen[1:], strict=False)]
    assert all(step >= compress.PROGRESS_STEP - 1e-9 for step in steps[:-1])


def test_cancel_propagates_and_leaves_the_original_untouched(tmp_path: Path) -> None:
    source = make_recording(tmp_path, ext="mkv")
    before = source.read_bytes()
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ServiceError(ErrorKind.CANCELLED, "operation was cancelled")

    with pytest.raises(ServiceError) as excinfo:
        _run(source, cancel=cancel)

    assert excinfo.value.kind is ErrorKind.CANCELLED
    assert source.read_bytes() == before
    assert _source_files(source.parent) == ["source.mkv"]
    assert _leftovers(source.parent) == []
