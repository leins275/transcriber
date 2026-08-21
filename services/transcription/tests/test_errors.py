"""Tests for the error taxonomy and provider-fault classifier (FR-8)."""

from __future__ import annotations

import pytest

from transcription.errors import ErrorKind, ServiceError, classify_http_status, redact

ALL_KINDS = [
    "invalid_request",
    "unsupported_input",
    "audio_decode",
    "model_load",
    "provider_auth",
    "provider_payment_required",
    "provider_rate_limited",
    "provider_unavailable",
    "timeout",
    "cancelled",
    "internal",
]


@pytest.mark.parametrize("value", ALL_KINDS)
def test_error_kind_round_trips(value: str) -> None:
    assert ErrorKind(value).value == value


def test_error_kind_has_exactly_the_taxonomy_values() -> None:
    assert {kind.value for kind in ErrorKind} == set(ALL_KINDS)


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (402, ErrorKind.PROVIDER_PAYMENT_REQUIRED, False),
        (401, ErrorKind.PROVIDER_AUTH, False),
        (403, ErrorKind.PROVIDER_AUTH, False),
        (429, ErrorKind.PROVIDER_RATE_LIMITED, True),
        (500, ErrorKind.PROVIDER_UNAVAILABLE, True),
        (503, ErrorKind.PROVIDER_UNAVAILABLE, True),
        (400, ErrorKind.INVALID_REQUEST, False),
        (404, ErrorKind.INVALID_REQUEST, False),
        (422, ErrorKind.INVALID_REQUEST, False),
        (200, ErrorKind.INTERNAL, False),
    ],
)
def test_classify_http_status(status: int, kind: ErrorKind, retryable: bool) -> None:
    err = classify_http_status(status)
    assert err.kind == kind
    assert err.retryable is retryable


def test_service_error_carries_fields() -> None:
    err = ServiceError(
        kind=ErrorKind.PROVIDER_AUTH,
        message="auth failed",
        status=401,
        retryable=False,
        detail="upstream said no",
    )
    assert err.kind == ErrorKind.PROVIDER_AUTH
    assert err.message == "auth failed"
    assert err.status == 401
    assert err.retryable is False
    assert err.detail == "upstream said no"


def test_service_error_str_never_contains_a_secret_passed_in() -> None:
    secret = "sk-abc123deadbeef"  # noqa: S105 -- test fixture, not a real credential
    err = ServiceError(
        kind=ErrorKind.PROVIDER_AUTH,
        message=f"provider rejected key {redact(secret)}",
        status=401,
    )
    assert secret not in str(err)


def test_redact_strips_bearer_tokens_and_api_keys() -> None:
    text = "Bearer sk-abc123deadbeef and key=sk-live-xyz"
    redacted = redact(text)
    assert "sk-abc123deadbeef" not in redacted
    assert "sk-live-xyz" not in redacted


def test_service_error_to_dict_shape() -> None:
    err = ServiceError(
        kind=ErrorKind.PROVIDER_RATE_LIMITED,
        message="rate limited",
        status=429,
        retryable=True,
    )
    assert err.to_dict() == {
        "error_kind": "provider_rate_limited",
        "error_message": "rate limited",
        "provider_status": 429,
    }
