"""Tests for the provider protocol, lazy registry and cancellation (FR-4)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fakes import FakeProvider

from transcription.errors import ErrorKind, ServiceError
from transcription.providers import (
    get_provider,
    known_provider_names,
    register,
    validate_provider_name,
)
from transcription.providers.base import CancelToken, TranscriptResult


def test_importing_providers_package_does_not_import_provider_libraries() -> None:
    sys.modules.pop("faster_whisper", None)
    sys.modules.pop("litellm", None)

    import transcription.providers  # noqa: F401 (import is the point of the test)

    assert "faster_whisper" not in sys.modules
    assert "litellm" not in sys.modules


def test_known_provider_names_includes_local_and_cloud_without_importing_either() -> None:
    """E15: checking registry membership must never import a provider
    library -- callers on the event loop (`JobManager.submit`) rely on this
    to reject a bogus name cheaply, before deferring real resolution off
    the event loop."""
    sys.modules.pop("faster_whisper", None)
    sys.modules.pop("litellm", None)

    names = known_provider_names()

    assert {"local", "cloud"} <= names
    assert "faster_whisper" not in sys.modules
    assert "litellm" not in sys.modules


def test_validate_provider_name_accepts_known_and_rejects_unknown() -> None:
    validate_provider_name("local")  # does not raise

    with pytest.raises(ServiceError) as exc_info:
        validate_provider_name("nope")
    assert exc_info.value.kind == ErrorKind.INVALID_REQUEST


def test_get_provider_unknown_name_raises_invalid_request_naming_known_names() -> None:
    with pytest.raises(ServiceError) as exc_info:
        get_provider("nope", None)

    err = exc_info.value
    assert err.kind == ErrorKind.INVALID_REQUEST
    assert "local" in err.message
    assert "cloud" in err.message


def test_register_then_get_provider_returns_the_registered_fake() -> None:
    register("fake", FakeProvider)

    provider = get_provider("fake", None)

    assert isinstance(provider, FakeProvider)


def test_transcript_result_is_constructible_with_all_documented_fields() -> None:
    result = TranscriptResult(
        segments=[{"id": 0, "text": "hi"}],
        text="hi",
        language="en",
        language_probability=0.9,
        duration_sec=1.5,
        model="base",
        device="cpu",
        compute_type="int8",
        cost_usd=None,
        currency=None,
        filtered_segment_count=0,
    )

    assert result.text == "hi"
    assert result.cost_usd is None
    assert result.filtered_segment_count == 0


def test_transcript_result_rejects_negative_duration() -> None:
    with pytest.raises(ValueError):
        TranscriptResult(
            segments=[],
            text="",
            language=None,
            language_probability=None,
            duration_sec=-1.0,
            model="base",
            device="cpu",
            compute_type=None,
        )


def test_cancel_token_set_marks_cancelled_and_raise_if_cancelled_raises() -> None:
    token = CancelToken()
    assert token.cancelled is False

    token.set()

    assert token.cancelled is True
    with pytest.raises(ServiceError) as exc_info:
        token.raise_if_cancelled()
    assert exc_info.value.kind == ErrorKind.CANCELLED


def test_fake_provider_reports_progress_at_quarter_marks() -> None:
    provider = FakeProvider()
    progress: list[float] = []

    result = provider.transcribe(
        Path("audio.wav"), language=None, on_progress=progress.append, cancel=CancelToken()
    )

    assert progress == [0.25, 0.5, 0.75, 1.0]
    assert result.text == "hello world"


def test_fake_provider_honours_cancel_token_between_chunks() -> None:
    provider = FakeProvider()
    cancel = CancelToken()
    progress: list[float] = []

    def on_progress(fraction: float) -> None:
        progress.append(fraction)
        if fraction == 0.5:
            cancel.set()

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            Path("audio.wav"), language=None, on_progress=on_progress, cancel=cancel
        )

    assert exc_info.value.kind == ErrorKind.CANCELLED
    assert progress == [0.25, 0.5]


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_fake_provider_can_be_configured_to_raise_any_error_kind(kind: ErrorKind) -> None:
    provider = FakeProvider(raise_kind=kind)

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            Path("audio.wav"),
            language=None,
            on_progress=lambda _: None,
            cancel=CancelToken(),
        )

    assert exc_info.value.kind == kind
