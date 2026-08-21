"""Structured JSON-lines logging (stderr) and the ready-line emitter (stdout).

FR-14: the service emits structured JSON-lines logs on stderr and, on startup,
prints exactly one machine-readable ready line to stdout so a supervising
parent (F3/Tauri sidecar) can discover the port and token.

FR-8: no log line may contain an API key or bearer token; well-known secret
field names are redacted before serialization.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import IO, Any

_LOGGER_NAME = "transcription"
_REDACTED = "***"
_SECRET_KEYS = {"token", "api_key", "authorization"}

# Standard LogRecord attributes -- anything else on the record is a caller-supplied
# "extra" field and gets folded into the JSON line verbatim (after redaction).
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)

_ready_emitted = False


class _JsonLinesFormatter(logging.Formatter):
    """Renders one JSON object per LogRecord, with secret fields redacted."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", record.name),
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key in payload:
                continue
            payload[key] = _REDACTED if key in _SECRET_KEYS else value

        if record.exc_info:
            payload["traceback"] = "".join(traceback.format_exception(*record.exc_info))

        return json.dumps(payload, default=str)


def configure_logging(level: int, *, stream: IO[str] | None = None) -> logging.Logger:
    """Attach exactly one stderr (or given stream) JSON-lines handler to the logger.

    Calling this repeatedly reconfigures the single handler instead of stacking
    duplicates.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    target_stream: IO[str] = stream if stream is not None else sys.stderr

    for existing in list(logger.handlers):
        logger.removeHandler(existing)

    handler = logging.StreamHandler(target_stream)
    handler.setFormatter(_JsonLinesFormatter())
    logger.addHandler(handler)

    return logger


def emit_ready_line(*, port: int, token: str, pid: int, stream: IO[str] | None = None) -> None:
    """Write the single FR-14 ready line: ``{"event":"listening",...}``.

    May only be called once per process; a second call raises RuntimeError so
    the "exactly one ready line" contract F3 codes against cannot be violated
    silently.
    """
    global _ready_emitted
    if _ready_emitted:
        raise RuntimeError("emit_ready_line() may only be called once per process")

    target_stream: IO[str] = stream if stream is not None else sys.stdout
    line = json.dumps({"event": "listening", "port": port, "token": token, "pid": pid})
    target_stream.write(line + "\n")
    target_stream.flush()
    _ready_emitted = True
