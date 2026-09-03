"""Decoding a recording to samples, for consumers outside the provider.

The diarizer hands pyannote a waveform rather than a file path (torchaudio
on Windows cannot open the vault's mp4/m4a recordings), and it must be the
*same* audio the transcription saw -- so it decodes through the local
provider's own decoder (PyAV, FFmpeg bundled in the wheel). Provider
library names stay confined to this package (FR-4, `test_attribution.py`);
this is the one door out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def decode_samples(audio_path: Path, *, sample_rate: int) -> Any:
    """The recording as a float32 mono numpy array at ``sample_rate``.

    Lazy import: the decoder pulls in the provider library, which the
    service never imports before a job actually needs it (NFR-1).
    """
    from faster_whisper.audio import (  # type: ignore[import-untyped]  # noqa: PLC0415
        decode_audio,
    )

    return decode_audio(str(audio_path), sampling_rate=sample_rate)
