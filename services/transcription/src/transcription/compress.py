"""Video compression for a filed recording -- the drop-to-insights chain's last stage.

Re-encodes ``<meeting>/source.<ext>`` to a smaller HEVC mp4 -- x265 at
CRF 26, capped at 1080p -- and *replaces* the original with it, so the
vault stops filling up with multi-GB originals. Everything runs in-process
through PyAV (FFmpeg's libraries bundled in the wheel, the same decoder the
transcription used): no external ``ffmpeg`` binary, ever (FR-7).

Why these settings (measured on the operator's own recordings, 4K screen
shares at a fixed 2.7 Mbps): re-encoding at the *same* resolution with a
"visually lossless" H.264 index does not shrink them at all -- x264 CRF 23
landed at the source's bitrate and NVENC's constant-quality H.264 at nearly
twice it -- because the recorder already starves them. Halving the pixels
is what pays, and on this mostly-static screen-share content HEVC pays
again: at 1080p, x265 CRF 26 came out at a third of x264 CRF 23's size
(~334 vs ~995 kbps, 87 % under the source) at the *same* speed, while the
GPU encoders were 2.5x larger for a quarter more speed -- so x265 is the
one encoder and there is no NVENC path. Area resampling keeps screen text
crisper than bilinear at no cost (Lanczos was seven times slower). The
mp4 carries the ``hvc1`` tag so QuickTime and Safari recognise it.

Degradation over failure, like every other stage: an audio-only recording,
one that is already small, an encoder that will not open, a result that is
not worth keeping or a verification that fails all leave the original
exactly as it was and report *why* as a job warning. Only a cancellation
propagates. The original is never touched before the output has been
verified, and the meeting never holds two ``source.*`` files: the output
is encoded under a name whose stem is not ``source`` (every scanner --
``artifacts.find_source_recording``, the app's ``source_file_in`` --
matches on that stem), then swapped in with renames.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from transcription.errors import ServiceError

if TYPE_CHECKING:  # the real import is lazy, inside the functions that need it
    import av

_logger = logging.getLogger("transcription")

# Extensions the vault accepts that can only ever hold audio: nothing to
# compress, and not worth opening.
AUDIO_ONLY_EXTENSIONS = frozenset({"m4a", "mp3", "wav", "flac", "ogg"})

# Audio codecs an mp4 can carry as-is; anything else is re-encoded to AAC.
MP4_AUDIO_CODECS = frozenset({"aac", "mp3", "opus", "ac3", "eac3", "alac"})

# Work files live beside the recording under a `compress.` prefix: their
# stem is never `source`, so a crash mid-way leaves a file no scanner reads
# as the recording, and `sweep_partials` clears it on the next run.
PARTIAL_PREFIX = "compress."
PARTIAL_NAME = "compress.partial.mp4"
REPLACED_PREFIX = "compress.replaced."

# A source at or under this average bitrate is already small: re-encoding
# it would cost minutes to save nearly nothing.
MIN_SOURCE_KBPS = 2000.0
# The output replaces the original only when it is at least this much smaller.
MIN_GAIN = 0.15
# The output's duration must match the source's within this many seconds.
DURATION_TOLERANCE_SEC = 1.0

AAC_BIT_RATE = 160_000
PROGRESS_STEP = 0.02
CANCEL_EVERY_PACKETS = 50

OnProgress = Callable[[float], None]
Cancel = Callable[[], None]


# The output's shorter side is capped here: a 4K (3840x2160) recording
# becomes 1920x1080, a portrait 2160x3840 becomes 1080x1920, and anything
# already at or under it keeps its size.
MAX_SHORT_SIDE = 1080
# Downscale filter: area averaging is the fastest of swscale's filters and
# keeps screen text sharper than the bilinear default.
RESAMPLE = "AREA"


@dataclass(frozen=True)
class EncoderSpec:
    """One video encoder to try, with its FFmpeg private options."""

    name: str
    options: dict[str, str]


X265 = EncoderSpec(
    "libx265",
    # x265 logs its whole configuration to stderr per encode unless muted.
    {"crf": "26", "preset": "medium", "x265-params": "log-level=none"},
)

# Tried in order; a spec that fails to open hands over to the next one.
DEFAULT_ENCODERS: tuple[EncoderSpec, ...] = (X265,)

# HEVC in mp4 needs this four-character tag for QuickTime and Safari;
# FFmpeg's default `hev1` plays in VLC and Windows but not there.
HEVC_MP4_TAG = "hvc1"


@dataclass(frozen=True)
class CompressOutcome:
    """What happened to the recording."""

    replaced: bool
    # The recording as it stands afterwards (the new mp4, or the original).
    path: Path
    # Why the original was kept, when it was; `None` on a replace.
    warning: str | None
    # The encoder that produced the kept output (`None` when nothing was kept).
    encoder: str | None
    # `"copy"` / `"aac"` / `None` (no audio stream, or nothing encoded).
    audio: str | None
    before_bytes: int
    after_bytes: int

    def as_manifest(self) -> dict[str, Any]:
        return {
            "replaced": self.replaced,
            "path": str(self.path),
            "encoder": self.encoder,
            "audio": self.audio,
            "before_bytes": self.before_bytes,
            "after_bytes": self.after_bytes,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class _Probe:
    has_video: bool
    duration_sec: float | None
    kbps: float | None


def target_size(width: int, height: int) -> tuple[int, int]:
    """The output size for a ``width`` x ``height`` source: the shorter side
    capped at ``MAX_SHORT_SIDE`` with the aspect kept, both sides even
    (yuv420p h264 refuses odd sizes; at most one pixel is cropped)."""
    short = min(width, height)
    if short > MAX_SHORT_SIDE:
        scale = MAX_SHORT_SIDE / short
        width, height = round(width * scale), round(height * scale)
    return width // 2 * 2, height // 2 * 2


def sweep_partials(meeting_dir: Path) -> None:
    """Delete the work files a crashed run may have left beside the recording."""
    try:
        entries = list(meeting_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_file() and entry.name.startswith(PARTIAL_PREFIX):
            try:
                entry.unlink()
            except OSError as exc:
                _logger.warning("could not remove stale %s: %s", entry.name, exc)


def compress_recording(
    source: Path,
    *,
    on_progress: OnProgress,
    cancel: Cancel | None = None,
    encoders: Sequence[EncoderSpec] = DEFAULT_ENCODERS,
    min_source_kbps: float = MIN_SOURCE_KBPS,
    min_gain: float = MIN_GAIN,
) -> CompressOutcome:
    """Replace ``source`` with a smaller H.264 mp4, or leave it alone and say why.

    ``on_progress`` receives the decoded fraction of the source (0..1,
    throttled) while encoding; ``cancel`` is polled between packets and is
    expected to raise ``ServiceError(CANCELLED)``, which propagates
    untouched. Every other failure is a warning on the outcome.
    """
    meeting_dir = source.parent
    before = source.stat().st_size
    kept = _kept(source, before)

    sweep_partials(meeting_dir)

    if source.suffix.lower().lstrip(".") in AUDIO_ONLY_EXTENSIONS:
        return kept("audio-only recording, nothing to compress")
    probe = _inspect(source)
    if not probe.has_video:
        return kept("no video stream in the recording, nothing to compress")
    if probe.kbps is not None and probe.kbps <= min_source_kbps:
        return kept(f"already {probe.kbps:.0f} kbps, kept as-is")

    partial = meeting_dir / PARTIAL_NAME
    last_error: str | None = None
    try:
        for spec in encoders:
            try:
                decoded_sec, audio = _encode(
                    source, partial, spec, on_progress=on_progress, cancel=cancel
                )
            except ServiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - the next encoder gets its turn
                last_error = f"{spec.name}: {exc}"
                _logger.info("compress: encoder %s failed (%s)", spec.name, exc)
                partial.unlink(missing_ok=True)
                continue
            expected_sec = probe.duration_sec if probe.duration_sec is not None else decoded_sec
            reason = _verify(partial, expected_sec, before, min_gain)
            if reason is not None:
                return kept(f"video kept as-is: {reason}")
            after = partial.stat().st_size
            final = _swap(source, partial)
            return CompressOutcome(
                replaced=True,
                path=final,
                warning=None,
                encoder=spec.name,
                audio=audio,
                before_bytes=before,
                after_bytes=after,
            )
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 - degraded, never job-fatal
        return kept(f"video kept as-is: {exc}")
    finally:
        partial.unlink(missing_ok=True)
    return kept(f"video kept as-is: no encoder could run ({last_error or 'none configured'})")


def _kept(source: Path, before: int) -> Callable[[str], CompressOutcome]:
    def outcome(warning: str) -> CompressOutcome:
        return CompressOutcome(
            replaced=False,
            path=source,
            warning=warning,
            encoder=None,
            audio=None,
            before_bytes=before,
            after_bytes=before,
        )

    return outcome


def _inspect(source: Path) -> _Probe:
    """The facts the skip rules and the verification need, from the container."""
    import av  # noqa: PLC0415 - lazy: nothing heavy at service start (NFR-1)

    with av.open(str(source)) as container:
        video = container.streams.video[0] if container.streams.video else None
        duration = _container_duration_sec(container)
        if (
            duration is None
            and video is not None
            and video.duration is not None
            and video.time_base is not None
        ):
            duration = float(video.duration * video.time_base)
        kbps: float | None = None
        if container.bit_rate and container.bit_rate > 0:
            kbps = container.bit_rate / 1000
        elif duration:
            kbps = source.stat().st_size * 8 / duration / 1000
    return _Probe(has_video=video is not None, duration_sec=duration, kbps=kbps)


def _container_duration_sec(container: Any) -> float | None:
    import av  # noqa: PLC0415

    if container.duration is None or container.duration <= 0:
        return None
    return float(container.duration / av.time_base)


def _encode(
    source: Path,
    target: Path,
    spec: EncoderSpec,
    *,
    on_progress: OnProgress,
    cancel: Cancel | None,
) -> tuple[float, str | None]:
    """Transcode ``source`` into ``target``; returns (decoded seconds, audio mode).

    The first video stream is re-encoded with ``spec`` at ``target_size``
    and its own frame rate, pts copied in the source time base (variable
    frame rates survive). The first audio stream is remuxed when an mp4 can
    hold its codec, else re-encoded to AAC. Everything else (subtitles,
    chapters, extra streams) is dropped.
    """
    import av  # noqa: PLC0415

    with av.open(str(source)) as inp:
        src_video = inp.streams.video[0]
        src_audio = inp.streams.audio[0] if inp.streams.audio else None
        duration = _container_duration_sec(inp)
        # FFmpeg decodes on one thread unless told otherwise; a 1080p HEVC
        # source then decodes slower than NVENC encodes. Frame+slice
        # threading is what the ffmpeg CLI does by default.
        src_video.thread_type = "AUTO"

        out = av.open(str(target), "w", format="mp4", options={"movflags": "faststart"})
        try:
            rate = src_video.average_rate or src_video.guessed_rate or Fraction(30, 1)
            video = cast(
                "av.VideoStream", out.add_stream(spec.name, rate=rate, options=dict(spec.options))
            )
            width, height = target_size(src_video.width, src_video.height)
            video.width, video.height = width, height
            video.pix_fmt = "yuv420p"
            video.time_base = src_video.time_base
            if video.codec_context.name in ("libx265", "hevc_nvenc", "hevc"):
                video.codec_context.codec_tag = HEVC_MP4_TAG

            audio_mode: str | None = None
            audio: av.AudioStream | None = None
            resampler: av.AudioResampler | None = None
            if src_audio is not None:
                if src_audio.codec_context.name in MP4_AUDIO_CODECS:
                    audio = out.add_stream_from_template(src_audio)
                    audio_mode = "copy"
                else:
                    audio = out.add_stream("aac", rate=src_audio.rate)
                    audio.bit_rate = AAC_BIT_RATE
                    resampler = av.AudioResampler(
                        format="fltp", layout=src_audio.layout, rate=src_audio.rate
                    )
                    audio_mode = "aac"

            decoded_sec = 0.0
            reported = 0.0
            packets = 0

            def emit_video(decoded: Any) -> None:
                nonlocal decoded_sec
                frame = cast("av.VideoFrame", decoded)
                if frame.pts is not None and src_video.time_base is not None:
                    decoded_sec = float(frame.pts * src_video.time_base)
                scaled = frame.reformat(width, height, "yuv420p", interpolation=RESAMPLE)
                for encoded in video.encode(scaled):
                    out.mux(encoded)

            def emit_audio(
                decoded: Any, audio_out: av.AudioStream, audio_resampler: av.AudioResampler
            ) -> None:
                for resampled in audio_resampler.resample(cast("av.AudioFrame", decoded)):
                    for encoded in audio_out.encode(resampled):
                        out.mux(encoded)

            # At end of file the demuxer yields one empty packet per stream
            # (no dts): decoding it flushes that decoder -- which a
            # frame-threaded decoder needs, or its last frames are lost.
            # Such a packet carries no data to remux.
            streams = [s for s in (src_video, src_audio) if s is not None]
            for packet in inp.demux(*streams):
                packets += 1
                if cancel is not None and packets % CANCEL_EVERY_PACKETS == 0:
                    cancel()
                if packet.stream is src_video:
                    for decoded in packet.decode():
                        emit_video(decoded)
                    if duration:
                        fraction = min(1.0, decoded_sec / duration)
                        if fraction - reported >= PROGRESS_STEP:
                            reported = fraction
                            on_progress(fraction)
                elif audio is not None:
                    if resampler is None:
                        if packet.dts is not None:
                            packet.stream = audio
                            out.mux(packet)
                    else:
                        for decoded in packet.decode():
                            emit_audio(decoded, audio, resampler)
            for encoded in video.encode(None):
                out.mux(encoded)
            if audio is not None and resampler is not None:
                for resampled in resampler.resample(None):
                    for encoded in audio.encode(resampled):
                        out.mux(encoded)
                for encoded in audio.encode(None):
                    out.mux(encoded)
        finally:
            # `faststart` rewrites the file on close, so this is where a
            # disk-full error surfaces; the caller treats it like any other.
            out.close()
    if reported < 1.0:
        on_progress(1.0)
    return decoded_sec, audio_mode


def _verify(target: Path, expected_sec: float, source_bytes: int, min_gain: float) -> str | None:
    """Why ``target`` must not replace the original, or ``None`` when it may."""
    import av  # noqa: PLC0415

    size = target.stat().st_size
    if size > (1.0 - min_gain) * source_bytes:
        gain = 1.0 - size / source_bytes if source_bytes else 0.0
        return f"gain {gain:.0%} is under {min_gain:.0%}"
    try:
        with av.open(str(target)) as container:
            if not container.streams.video:
                return "the output has no video stream"
            duration = _container_duration_sec(container)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return f"the output does not open ({exc})"
    if duration is None:
        return "the output reports no duration"
    if abs(duration - expected_sec) > DURATION_TOLERANCE_SEC:
        return f"duration {duration:.1f}s does not match the source's {expected_sec:.1f}s"
    return None


def _swap(source: Path, partial: Path) -> Path:
    """Put the verified output in the original's place; the only write to ``source.*``.

    A same-extension source is replaced in one rename. Otherwise the
    original is moved aside first (a locked file fails *here*, before
    anything is named ``source``), the output takes the ``source.mp4``
    name, and the moved-aside original is deleted -- a leftover there is a
    ``compress.*`` file, never a second recording.
    """
    if source.suffix.lower() == ".mp4":
        os.replace(partial, source)
        return source
    final = source.with_name("source.mp4")
    replaced = source.with_name(f"{REPLACED_PREFIX}{source.suffix.lstrip('.')}")
    os.replace(source, replaced)
    try:
        os.replace(partial, final)
    except OSError:
        os.replace(replaced, source)
        raise
    try:
        os.remove(replaced)
    except OSError as exc:
        _logger.warning(
            "compress: the replaced original %s could not be removed: %s", replaced, exc
        )
    return final
