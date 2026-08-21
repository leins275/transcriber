"""Tests for structured JSON-lines logging and the ready-line emitter (FR-14, FR-8)."""

from __future__ import annotations

import io
import json
import logging

import pytest

import transcription.logging_setup as logging_setup
from transcription.logging_setup import configure_logging, emit_ready_line


@pytest.fixture(autouse=True)
def _reset_ready_line_guard() -> None:
    """Each test gets a fresh ready-line guard; the "called twice" test drives its own state."""
    logging_setup._ready_emitted = False  # noqa: SLF001 -- test-only reset of module guard


def _make_stderr_logger(level: int = logging.INFO) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = configure_logging(level, stream=stream)
    return logger, stream


def test_configure_logging_attaches_exactly_one_handler() -> None:
    logger, _ = _make_stderr_logger()
    assert len(logger.handlers) == 1


def test_configure_logging_writes_to_the_given_stream_only() -> None:
    logger, stream = _make_stderr_logger()
    logger.info("hello", extra={"event": "test_event"})
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["msg"] == "hello"
    assert record["event"] == "test_event"
    assert "ts" in record
    assert "level" in record


def test_nothing_is_ever_written_to_stdout_by_the_logger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger, _ = _make_stderr_logger()
    logger.warning("careful", extra={"event": "warn_event"})
    captured = capsys.readouterr()
    assert captured.out == ""


def test_log_record_has_required_fields() -> None:
    logger, stream = _make_stderr_logger()
    logger.info("plain message", extra={"event": "plain"})
    record = json.loads(stream.getvalue().splitlines()[0])
    assert record["msg"] == "plain message"
    assert record["event"] == "plain"
    assert "ts" in record
    assert "level" in record


def test_exception_logged_with_exc_info_puts_traceback_in_one_json_line() -> None:
    logger, stream = _make_stderr_logger()
    try:
        raise ValueError("boom")
    except ValueError:
        logger.error("failed", exc_info=True, extra={"event": "error_event"})
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "traceback" in record
    assert "ValueError: boom" in record["traceback"]


@pytest.mark.parametrize("secret_key", ["token", "api_key", "authorization"])
def test_secret_extras_are_redacted(secret_key: str) -> None:
    logger, stream = _make_stderr_logger()
    logger.info("secret log", extra={"event": "secret_event", secret_key: "sk-abc123deadbeef"})
    record = json.loads(stream.getvalue().splitlines()[0])
    assert record[secret_key] == "***"
    assert "sk-abc123deadbeef" not in stream.getvalue()


def test_emit_ready_line_writes_exactly_one_parseable_json_line() -> None:
    buf = io.StringIO()
    emit_ready_line(port=51234, token="t", pid=42, stream=buf)  # noqa: S106 -- test fixture
    lines = buf.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "event": "listening",
        "port": 51234,
        "token": "t",
        "pid": 42,
    }


def test_emit_ready_line_flushes_the_stream() -> None:
    class TrackingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushed = False

        def flush(self) -> None:
            self.flushed = True
            super().flush()

    stream = TrackingStream()
    emit_ready_line(port=1, token="t", pid=1, stream=stream)  # noqa: S106 -- test fixture
    assert stream.flushed is True


def test_emit_ready_line_called_twice_raises() -> None:
    buf = io.StringIO()
    emit_ready_line(port=1, token="t", pid=1, stream=buf)  # noqa: S106 -- test fixture
    with pytest.raises(RuntimeError):
        emit_ready_line(port=2, token="t2", pid=2, stream=buf)  # noqa: S106 -- test fixture


def test_repeated_configure_logging_calls_do_not_duplicate_handlers() -> None:
    stream = io.StringIO()
    logger = configure_logging(logging.INFO, stream=stream)
    logger = configure_logging(logging.INFO, stream=stream)
    assert len(logger.handlers) == 1
