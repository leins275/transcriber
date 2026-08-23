"""Built-in llama.cpp LLM engine (the primary runtime).

This module and ``openai_compat.py`` are the only places an LLM-library name
(``llama_cpp``, ``litellm``) may appear outside ``config.py`` (the FR-4
isolation rule, enforced by the grep test in ``test_attribution.py``).

``llama_cpp`` is imported lazily inside :meth:`LlamaCppProvider._load` (the
``diarizer.py`` pattern), so constructing the provider -- and importing this
module -- costs nothing; the multi-second model load happens on the worker
thread of the first job that actually completes something.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError, redact
from transcription.llm.base import LlmCompletion, LlmInfo, Message, ModelState
from transcription.providers.base import CancelToken

logger = logging.getLogger(__name__)

# How often the streaming loop checks the cancel token, in decoded chunks.
# llama.cpp yields one chunk per token, so this is "every token" -- cheap,
# and it keeps cancellation latency at a single token's decode time.
_CANCEL_CHECK_EVERY = 1


class LlamaCppProvider:
    """Chat completions through a local GGUF model via llama-cpp-python."""

    name = "llama_cpp"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._llama: Any | None = None
        self._state: ModelState = "unloaded"
        # complete() runs on the JobManager's single worker thread, but
        # unload() can be called from elsewhere; keep the handle swap safe.
        self._lock = threading.Lock()

    def describe(self) -> LlmInfo:
        return LlmInfo(
            name=self.name,
            model=self.config.llm_model,
            device="cuda" if self.config.llm_gpu_layers > 0 else "cpu",
            model_state=self._state,
        )

    def model_file(self) -> Path:
        """The GGUF file this provider will load (may not exist yet)."""
        return Path(self.config.llm_model_path) / self.config.llm_model_file

    def _load(self) -> Any:
        with self._lock:
            if self._llama is not None:
                return self._llama

            model_file = self.model_file()
            if not model_file.is_file():
                raise ServiceError(
                    ErrorKind.MODEL_LOAD,
                    f"LLM model file not found: {model_file.name}; "
                    "download it first (POST /v1/llm-model/download)",
                )

            self._state = "loading"
            try:
                import llama_cpp  # noqa: PLC0415 - deliberate lazy import (NFR-1)

                kwargs: dict[str, Any] = {
                    "model_path": str(model_file),
                    "n_ctx": self.config.llm_ctx,
                    "n_gpu_layers": self.config.llm_gpu_layers,
                    "verbose": False,
                }
                if self.config.llm_threads is not None:
                    kwargs["n_threads"] = self.config.llm_threads
                self._llama = llama_cpp.Llama(**kwargs)
            except ServiceError:
                self._state = "unloaded"
                raise
            except Exception as exc:
                self._state = "unloaded"
                raise ServiceError(
                    ErrorKind.MODEL_LOAD,
                    f"failed to load LLM model {model_file.name}: {redact(str(exc))}",
                ) from exc
            self._state = "loaded"
            return self._llama

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
        llama = self._load()

        kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if json_schema is not None:
            # llama-cpp-python compiles the schema to a GBNF grammar, so the
            # output is *grammatically guaranteed* to parse -- the main
            # reliability mechanism for structured output on a local model.
            kwargs["response_format"] = {"type": "json_object", "schema": json_schema}

        pieces: list[str] = []
        completion_tokens = 0
        try:
            for chunk in llama.create_chat_completion(**kwargs):
                if completion_tokens % _CANCEL_CHECK_EVERY == 0:
                    cancel.raise_if_cancelled()
                delta = chunk["choices"][0].get("delta", {})
                piece = delta.get("content")
                if piece:
                    pieces.append(piece)
                    completion_tokens += 1
                    on_progress(min(0.95, completion_tokens / max(1, max_tokens)))
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                ErrorKind.INTERNAL,
                f"llama.cpp completion failed: {redact(str(exc))}",
            ) from exc

        on_progress(1.0)
        return LlmCompletion(text="".join(pieces), completion_tokens=completion_tokens)

    def unload(self) -> None:
        """Drop the loaded model so its (mmap-backed) memory is released."""
        with self._lock:
            if self._llama is not None:
                logger.info("unloading LLM model %s", self.config.llm_model)
            self._llama = None
            self._state = "unloaded"
