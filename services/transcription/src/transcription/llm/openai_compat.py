"""OpenAI-compatible external LLM engine (LM Studio, Ollama, llama.cpp server).

The secondary runtime: the operator runs the model in an external server and
points ``llm_base_url`` at it. Calls go through litellm (already a hard
dependency for cloud STT), so this module and ``llama_cpp_local.py`` are the
only places an LLM-library name may appear outside ``config.py`` (FR-4
isolation, enforced by the grep test in ``test_attribution.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import litellm

from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError, classify_http_status, redact
from transcription.llm.base import LlmCompletion, LlmInfo, Message
from transcription.providers.base import CancelToken

# Same rule as litellm_cloud.py: litellm must never print anything of its
# own (the ready-line contract and "no secrets in logs" both depend on it).
litellm.suppress_debug_info = True

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    """Chat completions against an external OpenAI-compatible server."""

    name = "openai_compat"

    def __init__(self, config: Config) -> None:
        self.config = config

    def describe(self) -> LlmInfo:
        return LlmInfo(
            name=self.name,
            model=self.config.llm_model,
            device="external",
            # The model lives in the external server's process; from this
            # service's perspective there is nothing to load.
            model_state="loaded",
        )

    def complete(
        self,
        messages: list[Message],
        *,
        json_schema: dict[str, object] | None,
        max_tokens: int,
        temperature: float,
        on_progress: Callable[[float], None],
        cancel: CancelToken,
    ) -> LlmCompletion:
        cancel.raise_if_cancelled()

        base_url = self.config.llm_base_url
        if not base_url:
            raise ServiceError(
                ErrorKind.MODEL_LOAD,
                "llm_base_url must be configured for the openai_compat llm provider "
                "(e.g. http://127.0.0.1:1234/v1 for LM Studio)",
            )

        kwargs: dict[str, Any] = {
            # The "openai/" prefix routes litellm to its generic
            # OpenAI-protocol client rather than a named cloud provider.
            "model": f"openai/{self.config.llm_model}",
            "api_base": base_url,
            # Local servers rarely validate the key but the OpenAI client
            # requires one to be present.
            "api_key": self.config.llm_api_key or "sk-local",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Streaming so cancellation is honoured mid-generation.
            "stream": True,
        }
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": json_schema, "strict": True},
            }

        pieces: list[str] = []
        chunks_seen = 0
        try:
            for chunk in litellm.completion(**kwargs):
                cancel.raise_if_cancelled()
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    pieces.append(piece)
                    chunks_seen += 1
                    on_progress(min(0.95, chunks_seen / max(1, max_tokens)))
        except ServiceError:
            raise
        except Exception as exc:
            raise _classify_llm_exception(exc) from None

        on_progress(1.0)
        return LlmCompletion(text="".join(pieces), completion_tokens=chunks_seen)

    def unload(self) -> None:
        """Nothing resident in this process; the external server owns the model."""


def _classify_llm_exception(exc: Exception) -> ServiceError:
    """Map a raw litellm/connection exception onto the taxonomy, secrets redacted."""
    if isinstance(exc, ServiceError):
        return exc

    detail = redact(str(exc))
    status = getattr(exc, "status_code", None)
    if status is None:
        # Connection refused / DNS / socket errors: the server isn't there.
        return ServiceError(
            ErrorKind.PROVIDER_UNAVAILABLE,
            "LLM server is unreachable; is it running at the configured llm_base_url?",
            retryable=True,
            detail=detail,
        )
    return classify_http_status(status, detail)
