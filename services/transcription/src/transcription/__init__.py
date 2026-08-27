"""Transcription microservice.

A standalone, self-contained package: turns a meeting recording file into a
``transcript.json`` using a local speech-to-text model. Imports nothing from
the rest of the repository. Provider library names are confined to
``providers/`` and ``config.py`` (FR-4) -- this module never names one.

Nothing heavy (the transcription backends, the web framework) is imported
here, and no filesystem or network access happens at import time (NFR-1).
"""

from __future__ import annotations

__version__ = "0.13.1"

__all__ = ["__version__"]
