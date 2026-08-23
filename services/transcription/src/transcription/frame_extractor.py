"""Video frame extraction through PyAV (the ``diarizer.py`` seam pattern).

PyAV ships with faster-whisper's dependency tree and bundles the ffmpeg
libraries, so frames come out of the same wheels that already decode audio --
no external ``ffmpeg`` binary (the spec's FR-7 rule). ``av`` is imported
lazily inside the method so this module imports freely everywhere.

An audio-only recording is not an error: ``extract`` returns ``[]`` and the
caller writes its items without screenshots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from transcription.errors import ErrorKind, ServiceError, redact
from transcription.providers.base import CancelToken


class FrameExtractorProtocol(Protocol):
    """The only interface ``jobs.py`` may depend on for screenshots."""

    def extract(
        self,
        video_path: Path,
        timestamps: list[float],
        *,
        cancel: CancelToken,
    ) -> list[tuple[float, bytes]]:
        """PNG bytes for the first frame at/after each timestamp, in order.

        Returns ``[]`` when the file has no video stream. Raises
        ``ServiceError(AUDIO_DECODE)`` when the file cannot be decoded at
        all, and propagates cancellation.
        """
        ...


class PyAvFrameExtractor:
    """Frame extraction with PyAV's bundled ffmpeg libraries."""

    def extract(
        self,
        video_path: Path,
        timestamps: list[float],
        *,
        cancel: CancelToken,
    ) -> list[tuple[float, bytes]]:
        cancel.raise_if_cancelled()
        if not timestamps:
            return []

        import av  # noqa: PLC0415 - deliberate lazy import (NFR-1)

        results: list[tuple[float, bytes]] = []
        try:
            with av.open(str(video_path)) as container:
                video_streams = container.streams.video
                if not video_streams:
                    return []
                stream = video_streams[0]

                for stamp in sorted(timestamps):
                    cancel.raise_if_cancelled()
                    frame = self._frame_at(container, stream, stamp)
                    if frame is None:
                        continue
                    results.append((stamp, _encode_png(av, frame)))
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                ErrorKind.AUDIO_DECODE,
                f"failed to decode video for screenshots: {redact(str(exc))}",
            ) from exc
        return results

    @staticmethod
    def _frame_at(container: Any, stream: Any, timestamp_sec: float) -> Any | None:
        """Seek to the keyframe before ``timestamp_sec`` and decode forward
        to the first frame at or after it."""
        seek_target = int(timestamp_sec / float(stream.time_base))
        container.seek(seek_target, backward=True, stream=stream)
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            if frame.time >= timestamp_sec:
                return frame
        return None


def _encode_png(av: Any, frame: Any) -> bytes:
    """Encode one decoded video frame as PNG using PyAV's own codecs."""
    rgb = frame.reformat(format="rgb24")
    codec = av.CodecContext.create("png", "w")
    codec.width = rgb.width
    codec.height = rgb.height
    codec.pix_fmt = "rgb24"
    packets = list(codec.encode(rgb))
    packets.extend(codec.encode(None))
    return b"".join(bytes(packet) for packet in packets)
