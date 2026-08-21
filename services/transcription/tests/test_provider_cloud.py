"""Tests for the litellm cloud provider (FR-4).

The module's ``litellm`` symbol is monkeypatched with a fake for every test
here -- no network access ever happens (FR-15).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from litellm.exceptions import Timeout as LiteLLMTimeout

from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError
from transcription.providers import litellm_cloud
from transcription.providers.base import CancelToken


def _make_config(tmp_path: Path, **overrides: Any) -> Config:
    defaults: dict[str, Any] = {
        "app_dir": tmp_path,
        "config_path": tmp_path / "config.json",
        "provider": "cloud",
        "cloud_model": "whisper-1",
        "provider_api_key": "sk-test-key-abcdef",
        "max_cloud_upload_mb": 25,
    }
    defaults.update(overrides)
    return Config(**defaults)


def _write_audio(tmp_path: Path, size_bytes: int = 1024) -> Path:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"\x00" * size_bytes)
    return path


@dataclass
class FakeResponse:
    text: str = "hello world"
    language: str | None = "en"
    duration: float | None = 1.0
    segments: list[Any] = field(default_factory=list)
    _hidden_params: dict[str, Any] = field(default_factory=dict)


class FakeProviderError(Exception):
    """A generic provider fault carrying an HTTP-shaped status code."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeLiteLLM:
    """Stands in for the ``litellm`` module -- never touches the network."""

    def __init__(
        self,
        *,
        responses: list[Any] | None = None,
        exceptions: list[Exception] | None = None,
        cost: Any = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or [])
        self._exceptions = list(exceptions or [])
        self.completion_cost_calls: list[Any] = []
        self._cost = cost
        self.suppress_debug_info = False

    def transcription(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._exceptions:
            raise self._exceptions.pop(0)
        return self._responses.pop(0)

    def completion_cost(self, response: Any) -> Any:
        self.completion_cost_calls.append(response)
        if isinstance(self._cost, Exception):
            raise self._cost
        return self._cost


def _provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeLiteLLM, **cfg: Any):
    monkeypatch.setattr(litellm_cloud, "litellm", fake)
    config = _make_config(tmp_path, **cfg)
    return litellm_cloud.LiteLLMProvider(config)


def test_successful_response_maps_segments_to_local_shaped_schema(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    response = FakeResponse(
        segments=[
            {"start": 0.0, "end": 0.5, "text": "hi ", "avg_logprob": -0.1},
            {"start": 0.5, "end": 1.0, "text": "there"},
        ],
    )
    fake = FakeLiteLLM(responses=[response])
    provider = _provider(tmp_path, monkeypatch, fake)

    result = provider.transcribe(
        audio, language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert [set(seg) for seg in result.segments] == [
        {"id", "start", "end", "text", "avg_logprob", "no_speech_prob", "compression_ratio"},
        {"id", "start", "end", "text", "avg_logprob", "no_speech_prob", "compression_ratio"},
    ]
    assert result.segments[0]["id"] == 0
    assert result.segments[1]["id"] == 1
    assert result.segments[0]["avg_logprob"] == -0.1
    # missing fields default to None, never a fabricated number
    assert result.segments[1]["avg_logprob"] is None
    assert result.segments[1]["no_speech_prob"] is None
    assert result.segments[1]["compression_ratio"] is None
    assert "words" not in result.segments[0]


def test_cost_from_hidden_params_response_cost(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    response = FakeResponse(_hidden_params={"response_cost": 0.006})
    fake = FakeLiteLLM(responses=[response])
    provider = _provider(tmp_path, monkeypatch, fake)

    result = provider.transcribe(
        audio, language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.cost_usd == pytest.approx(0.006)
    assert result.currency == "USD"
    assert fake.completion_cost_calls == []


def test_cost_from_completion_cost_when_hidden_params_missing(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    response = FakeResponse(_hidden_params={})
    fake = FakeLiteLLM(responses=[response], cost=0.02)
    provider = _provider(tmp_path, monkeypatch, fake)

    result = provider.transcribe(
        audio, language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.cost_usd == pytest.approx(0.02)
    assert result.currency == "USD"
    assert fake.completion_cost_calls == [response]


def test_cost_is_none_and_warns_when_both_hooks_fail(tmp_path, monkeypatch, caplog):
    audio = _write_audio(tmp_path)
    response = FakeResponse(_hidden_params={})
    fake = FakeLiteLLM(responses=[response], cost=None)
    provider = _provider(tmp_path, monkeypatch, fake)

    with caplog.at_level(logging.WARNING):
        result = provider.transcribe(
            audio, language=None, on_progress=lambda _: None, cancel=CancelToken()
        )

    assert result.cost_usd is None
    assert result.currency is None
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.parametrize(
    ("status_code", "expected_kind", "expected_retryable"),
    [
        (401, ErrorKind.PROVIDER_AUTH, False),
        (402, ErrorKind.PROVIDER_PAYMENT_REQUIRED, False),
        (429, ErrorKind.PROVIDER_RATE_LIMITED, True),
        (503, ErrorKind.PROVIDER_UNAVAILABLE, True),
    ],
)
def test_provider_faults_classify_through_taxonomy(
    tmp_path, monkeypatch, status_code, expected_kind, expected_retryable
):
    audio = _write_audio(tmp_path)
    # Give a retryable fault a second response to succeed into, so we can
    # observe the classification of the *first* raised exception either way.
    fake = FakeLiteLLM(
        exceptions=[FakeProviderError("provider fault", status_code=status_code)],
        responses=[FakeResponse()],
    )
    provider = _provider(tmp_path, monkeypatch, fake)

    if expected_retryable:
        # succeeds on the bounded retry -- no exception should escape
        provider.transcribe(audio, language=None, on_progress=lambda _: None, cancel=CancelToken())
        assert len(fake.calls) == 2
    else:
        with pytest.raises(ServiceError) as exc_info:
            provider.transcribe(
                audio, language=None, on_progress=lambda _: None, cancel=CancelToken()
            )
        assert exc_info.value.kind == expected_kind
        assert exc_info.value.status == status_code
        assert len(fake.calls) == 1


def test_timeout_exception_maps_to_timeout_kind(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    timeout_exc = LiteLLMTimeout(message="timed out", model="whisper-1", llm_provider="openai")
    fake = FakeLiteLLM(exceptions=[timeout_exc, timeout_exc])
    provider = _provider(tmp_path, monkeypatch, fake)

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(audio, language=None, on_progress=lambda _: None, cancel=CancelToken())

    assert exc_info.value.kind == ErrorKind.TIMEOUT
    # timeout is retryable, so the bounded retry should have fired once
    assert len(fake.calls) == 2


def test_retryable_fault_retries_exactly_once(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    fake = FakeLiteLLM(
        exceptions=[FakeProviderError("rate limited", status_code=429)],
        responses=[FakeResponse()],
    )
    provider = _provider(tmp_path, monkeypatch, fake)

    result = provider.transcribe(
        audio, language=None, on_progress=lambda _: None, cancel=CancelToken()
    )

    assert result.text == "hello world"
    assert len(fake.calls) == 2


def test_non_retryable_fault_does_not_retry(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    fake = FakeLiteLLM(exceptions=[FakeProviderError("bad creds", status_code=401)])
    provider = _provider(tmp_path, monkeypatch, fake)

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(audio, language=None, on_progress=lambda _: None, cancel=CancelToken())

    assert exc_info.value.kind == ErrorKind.PROVIDER_AUTH
    assert len(fake.calls) == 1


def test_oversize_file_fails_fast_before_any_call(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path, size_bytes=2 * 1024 * 1024)
    fake = FakeLiteLLM()
    provider = _provider(tmp_path, monkeypatch, fake, max_cloud_upload_mb=1)

    progress_calls: list[float] = []
    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(
            audio, language=None, on_progress=progress_calls.append, cancel=CancelToken()
        )

    assert exc_info.value.kind == ErrorKind.INVALID_REQUEST
    assert "1" in exc_info.value.message
    assert "2.0" in exc_info.value.message
    assert fake.calls == []
    assert progress_calls == []


def test_api_key_read_from_config_and_passed_as_keyword(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    fake = FakeLiteLLM(responses=[FakeResponse()])
    provider = _provider(tmp_path, monkeypatch, fake, provider_api_key="sk-secret-value-999")

    provider.transcribe(audio, language=None, on_progress=lambda _: None, cancel=CancelToken())

    assert fake.calls[0]["api_key"] == "sk-secret-value-999"


def test_api_key_never_appears_in_service_error(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    secret = "sk-secret-value-999"  # noqa: S105 -- fake credential, not a real secret
    fake = FakeLiteLLM(
        exceptions=[FakeProviderError(f"auth denied for key {secret}", status_code=401)]
    )
    provider = _provider(tmp_path, monkeypatch, fake, provider_api_key=secret)

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(audio, language=None, on_progress=lambda _: None, cancel=CancelToken())

    assert secret not in exc_info.value.message
    assert secret not in (exc_info.value.detail or "")
    assert secret not in str(exc_info.value)


def test_progress_calls_are_only_zero_and_one(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    fake = FakeLiteLLM(responses=[FakeResponse()])
    provider = _provider(tmp_path, monkeypatch, fake)

    progress_calls: list[float] = []
    provider.transcribe(
        audio, language=None, on_progress=progress_calls.append, cancel=CancelToken()
    )

    assert progress_calls == [0.0, 1.0]


def test_cancel_before_call_raises_cancelled_without_calling_litellm(tmp_path, monkeypatch):
    audio = _write_audio(tmp_path)
    fake = FakeLiteLLM(responses=[FakeResponse()])
    provider = _provider(tmp_path, monkeypatch, fake)

    cancel = CancelToken()
    cancel.set()

    with pytest.raises(ServiceError) as exc_info:
        provider.transcribe(audio, language=None, on_progress=lambda _: None, cancel=cancel)

    assert exc_info.value.kind == ErrorKind.CANCELLED
    assert fake.calls == []
