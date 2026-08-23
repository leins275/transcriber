# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Adapted from Vexa (Vexa-ai/vexa), Apache-2.0 -- origin:
# core/meetings/modules/whisper/src/transcription-client.ts:57-88,
# reinforced by docs/docs/how-to/custom-stt.mdx:114-124
"""Error taxonomy and provider-fault classifier (FR-8).

Every failure in this service -- validation, path allowlist, decode
failures, provider faults -- is attributed to one of the ``ErrorKind``
values below and surfaced identically in the job record, the sqlite
ledger row and the HTTP body. Failures are never collapsed into a
generic "transcription failed" message, and the provider's raw HTTP
status plus a sanitized detail string are always preserved on the
record.
"""

from __future__ import annotations

import re
from enum import StrEnum


class ErrorKind(StrEnum):
    """The full taxonomy of failure kinds this service can attribute."""

    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_INPUT = "unsupported_input"
    AUDIO_DECODE = "audio_decode"
    MODEL_LOAD = "model_load"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_PAYMENT_REQUIRED = "provider_payment_required"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    # An LLM's structured output failed schema validation even after the one
    # bounded repair retry (the LLM job types' equivalent of a decode fault).
    LLM_OUTPUT = "llm_output"
    INTERNAL = "internal"


# Secrets that must never reach a log line or an HTTP response body (FR-9,
# FR-8). Matches "Bearer <token>", "sk-<...>" style API keys and
# "api_key=<...>" / "api-key=<...>" / "apikey=<...>" query-style params.
_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"api[-_]?key\s*=\s*[A-Za-z0-9._-]+", re.IGNORECASE),
]


def redact(text: str) -> str:
    """Replace anything that looks like a bearer token or API key with ``***``."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("***", redacted)
    return redacted


class ServiceError(Exception):
    """The one exception type every layer of this service raises and catches."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retryable = retryable
        self.detail = detail

    def to_dict(self) -> dict[str, str | int | None]:
        """The shape every failure body/record carries (FR-8)."""
        return {
            "error_kind": self.kind.value,
            "error_message": self.message,
            "provider_status": self.status,
        }


def classify_http_status(status: int, detail: str | None = None) -> ServiceError:
    """Reproduce vexa's fault ladder, renamed onto our ``ErrorKind`` values.

    402 -> payment_required; 401/403 -> unauthorized; 429 -> rate_limited
    (retryable); >=500 -> unavailable (retryable); >=400 -> bad_request;
    else -> unknown/internal.
    """
    sanitized = redact(detail) if detail is not None else None

    if status == 402:
        return ServiceError(
            ErrorKind.PROVIDER_PAYMENT_REQUIRED,
            "provider requires payment",
            status=status,
            retryable=False,
            detail=sanitized,
        )
    if status in (401, 403):
        return ServiceError(
            ErrorKind.PROVIDER_AUTH,
            "provider rejected credentials",
            status=status,
            retryable=False,
            detail=sanitized,
        )
    if status == 429:
        return ServiceError(
            ErrorKind.PROVIDER_RATE_LIMITED,
            "provider rate limited the request",
            status=status,
            retryable=True,
            detail=sanitized,
        )
    if status >= 500:
        return ServiceError(
            ErrorKind.PROVIDER_UNAVAILABLE,
            "provider is unavailable",
            status=status,
            retryable=True,
            detail=sanitized,
        )
    if status >= 400:
        return ServiceError(
            ErrorKind.INVALID_REQUEST,
            "provider rejected the request",
            status=status,
            retryable=False,
            detail=sanitized,
        )
    return ServiceError(
        ErrorKind.INTERNAL,
        "unclassified provider response",
        status=status,
        retryable=False,
        detail=sanitized,
    )
