"""litellm-backed cloud transcription provider (FR-4).

This provider covers every cloud STT provider litellm supports through one
call site and its cost-extraction hooks (NFR-8). Unlike ``local_whisper.py``,
none of this is adapted from vexa: vexa never uses litellm on the STT path
(see spec.md's provenance notes), so this module and its tests are original.

This module and ``local_whisper.py`` are the only places a provider-library
name (``litellm``, ``faster_whisper``, ``openai``, ``groq``) may appear
outside ``config.py`` (FR-4 acceptance, enforced by a grep test in T16).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import litellm
from litellm.exceptions import Timeout as _LiteLLMTimeout

from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError, classify_http_status, redact
from transcription.providers.base import CancelToken, ProviderInfo, TranscriptResult

# Never let litellm print anything of its own to stdout/stderr (FR-14's
# ready-line contract and FR-9's "no secrets in logs" both depend on this
# service being the only thing writing structured log lines).
litellm.suppress_debug_info = True

logger = logging.getLogger(__name__)

# One initial attempt plus exactly one bounded retry for a retryable fault.
_MAX_ATTEMPTS = 2


class LiteLLMProvider:
    """Cloud transcription through litellm's ``transcription`` surface."""

    name = "cloud"

    def __init__(self, config: Config) -> None:
        self.config = config

    def describe(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            model=self.config.cloud_model or "",
            device="cloud",
            compute_type=None,
            model_state="loaded",
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> TranscriptResult:
        cancel.raise_if_cancelled()

        max_mb = self.config.max_cloud_upload_mb
        size_mb = audio_path.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            raise ServiceError(
                ErrorKind.INVALID_REQUEST,
                f"audio file is {size_mb:.1f} MB, exceeding the {max_mb} MB cloud upload limit",
            )

        on_progress(0.0)
        response = self._call_with_retry(audio_path, language)
        on_progress(1.0)

        cost_usd, currency = _extract_cost(response)
        return TranscriptResult(
            segments=_normalize_segments(getattr(response, "segments", None) or []),
            text=getattr(response, "text", None) or "",
            language=getattr(response, "language", None) or language,
            language_probability=getattr(response, "language_probability", None),
            duration_sec=float(getattr(response, "duration", None) or 0.0),
            model=self.config.cloud_model or "",
            device="cloud",
            compute_type=None,
            cost_usd=cost_usd,
            currency=currency,
        )

    def _attempt_call(self, audio_path: Path, language: str | None) -> Any:
        try:
            with audio_path.open("rb") as handle:
                return litellm.transcription(
                    model=self.config.cloud_model,
                    file=handle,
                    language=language,
                    response_format="verbose_json",
                    api_key=self.config.provider_api_key,
                )
        except Exception as exc:
            raise _classify_provider_exception(exc) from None

    def _call_with_retry(self, audio_path: Path, language: str | None) -> Any:
        try:
            return self._attempt_call(audio_path, language)
        except ServiceError as error:
            if not error.retryable:
                raise
        # Exactly one bounded retry for a retryable fault (FR-8). A second
        # failure -- of any kind -- propagates straight out.
        return self._attempt_call(audio_path, language)


def _classify_provider_exception(exc: Exception) -> ServiceError:
    """Map a raw litellm exception onto our taxonomy, never leaking secrets."""
    if isinstance(exc, ServiceError):
        return exc

    detail = redact(str(exc))

    if isinstance(exc, _LiteLLMTimeout):
        return ServiceError(
            ErrorKind.TIMEOUT,
            "provider request timed out",
            status=getattr(exc, "status_code", None),
            retryable=True,
            detail=detail,
        )

    status = getattr(exc, "status_code", None)
    if status is None:
        return ServiceError(
            ErrorKind.PROVIDER_UNAVAILABLE,
            "provider call failed",
            retryable=True,
            detail=detail,
        )
    return classify_http_status(status, detail)


def _seg_get(segment: Any, key: str, default: Any = None) -> Any:
    if isinstance(segment, Mapping):
        return segment.get(key, default)
    return getattr(segment, key, default)


def _normalize_segments(raw_segments: Iterable[Any]) -> list[dict[str, Any]]:
    """Map litellm's ``verbose_json`` segments onto our schema (FR-6).

    Field names match ``local_whisper.py``'s mapping exactly, so
    ``transcript.json`` is schema-identical regardless of provider.
    """
    normalized: list[dict[str, Any]] = []
    for index, segment in enumerate(raw_segments):
        entry: dict[str, Any] = {
            "id": index,
            "start": _seg_get(segment, "start", 0.0),
            "end": _seg_get(segment, "end", 0.0),
            "text": _seg_get(segment, "text", ""),
            "avg_logprob": _seg_get(segment, "avg_logprob"),
            "no_speech_prob": _seg_get(segment, "no_speech_prob"),
            "compression_ratio": _seg_get(segment, "compression_ratio"),
        }
        words = _seg_get(segment, "words")
        if words is not None:
            entry["words"] = words
        normalized.append(entry)
    return normalized


def _coerce_positive_cost(value: Any) -> float | None:
    if value is None:
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    return cost if cost > 0 else None


def _extract_cost(response: Any) -> tuple[float | None, str | None]:
    """Cost from ``_hidden_params["response_cost"]``, else ``completion_cost`` (NFR-8).

    Never lies with ``0.0``: an unpriced job reports ``cost_usd = None`` and
    logs a warning instead.
    """
    hidden_params = getattr(response, "_hidden_params", None) or {}
    cost = _coerce_positive_cost(hidden_params.get("response_cost"))
    if cost is not None:
        return cost, "USD"

    try:
        computed = litellm.completion_cost(response)
    except Exception:  # noqa: BLE001 -- any pricing-hook failure just means "unpriced"
        computed = None
    cost = _coerce_positive_cost(computed)
    if cost is not None:
        return cost, "USD"

    logger.warning("cloud provider did not price this job; cost_usd will be null")
    return None, None
