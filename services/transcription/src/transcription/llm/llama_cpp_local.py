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

import importlib
import logging
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from transcription.config import Config
from transcription.errors import ErrorKind, ServiceError, redact
from transcription.llm.base import LlmCompletion, LlmInfo, Message, ModelState
from transcription.llm.gguf_meta import fit_gpu_layers, read_block_count
from transcription.llm.runtime_fetch import llama_cuda_dir
from transcription.providers.base import CancelToken
from transcription.runtime_dlls import register_cuda_dll_dirs

logger = logging.getLogger(__name__)


def _free_vram_bytes() -> int | None:
    """Free memory on the best NVIDIA GPU, via NVML (pure ctypes, no
    subprocess). ``None`` on machines with no NVIDIA driver -- the auto
    offload path degrades to CPU."""
    try:
        import pynvml  # noqa: PLC0415 - deliberate lazy import (NFR-1)

        pynvml.nvmlInit()
        try:
            best = 0
            for index in range(pynvml.nvmlDeviceGetCount()):
                handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                best = max(best, int(info.free))
            return best or None
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # noqa: BLE001 - "no NVML" is an answer, not an error
        return None


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
        # The offload actually applied by the last load; `None` until then.
        self._resolved_gpu_layers: int | None = None
        # complete() runs on the JobManager's single worker thread, but
        # unload() can be called from elsewhere; keep the handle swap safe.
        self._lock = threading.Lock()

    def describe(self) -> LlmInfo:
        resolved = self._resolved_gpu_layers
        if resolved is not None:
            device = "cuda" if resolved != 0 else "cpu"
        elif self.config.llm_gpu_layers == 0:
            device = "cpu"
        else:
            # Auto (-1) or a pinned positive count, not resolved yet.
            device = "auto"
        return LlmInfo(
            name=self.name,
            model=self.config.llm_model,
            device=device,
            model_state=self._state,
        )

    def _resolve_gpu_layers(self, model_file: Path, llama_cpp: Any) -> int:
        """The `n_gpu_layers` this load actually uses.

        A non-negative configured value is pinned verbatim; the `-1` auto
        default fits as many whole layers as the free VRAM holds (measured
        via NVML, layer count from the GGUF header), degrading to 0 --
        never an error -- whenever any signal is missing: a CPU-only
        llama.cpp build, no NVIDIA driver, an unreadable header.
        """
        configured = self.config.llm_gpu_layers
        if configured >= 0:
            return configured

        try:
            if not llama_cpp.llama_supports_gpu_offload():
                logger.info("llm offload: llama.cpp build has no GPU support; running on CPU")
                return 0
        except Exception:  # noqa: BLE001 - probe failure degrades to CPU
            return 0

        free_vram = _free_vram_bytes()
        if free_vram is None:
            logger.info("llm offload: no NVIDIA GPU visible via NVML; running on CPU")
            return 0
        block_count = read_block_count(model_file)
        if block_count is None:
            logger.warning(
                "llm offload: could not read the layer count from %s; running on CPU",
                model_file.name,
            )
            return 0

        layers = fit_gpu_layers(free_vram, model_file.stat().st_size, block_count)
        logger.info(
            "llm offload: %s of %d layers on GPU (%.1f GB VRAM free, %.1f GB model)",
            "all" if layers == -1 else layers,
            block_count,
            free_vram / 1e9,
            model_file.stat().st_size / 1e9,
        )
        return layers

    def model_file(self) -> Path:
        """The GGUF file this provider will load (may not exist yet)."""
        return Path(self.config.llm_model_path) / self.config.llm_model_file

    def _import_llama_cpp(self) -> Any:
        """Import llama_cpp, preferring the first-run-fetched CUDA build
        (``<app_dir>/runtime/llama-cuda``) over the baked CPU wheel.

        Falls back to the baked build when the CUDA one cannot load (driver
        removed, corrupt extraction) -- a slower summary beats a failed one.
        """
        if "llama_cpp" in sys.modules:
            return sys.modules["llama_cpp"]

        runtime_dir = llama_cuda_dir(self.config.app_dir)
        if (runtime_dir / "llama_cpp").is_dir():
            # cudart/cublas may have been extracted after this process's
            # startup registration ran; re-registering is idempotent.
            register_cuda_dll_dirs()
            sys.path.insert(0, str(runtime_dir))
            try:
                module = importlib.import_module("llama_cpp")
                logger.info("using the CUDA llama.cpp build from %s", runtime_dir)
                return module
            except Exception as exc:  # noqa: BLE001 - fall back, never fail the load here
                logger.warning(
                    "CUDA llama.cpp build failed to load (%s); "
                    "falling back to the built-in CPU build",
                    redact(str(exc)),
                )
                with suppress(ValueError):
                    sys.path.remove(str(runtime_dir))
                for name in [
                    loaded
                    for loaded in sys.modules
                    if loaded == "llama_cpp" or loaded.startswith("llama_cpp.")
                ]:
                    sys.modules.pop(name, None)

        return importlib.import_module("llama_cpp")

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
                # Deliberately lazy (NFR-1), routed through the CUDA-build
                # preference above.
                llama_cpp = self._import_llama_cpp()

                gpu_layers = self._resolve_gpu_layers(model_file, llama_cpp)
                kwargs: dict[str, Any] = {
                    "model_path": str(model_file),
                    "n_ctx": self.config.llm_ctx,
                    "n_gpu_layers": gpu_layers,
                    "verbose": False,
                }
                if self.config.llm_threads is not None:
                    kwargs["n_threads"] = self.config.llm_threads
                self._llama = llama_cpp.Llama(**kwargs)
                self._resolved_gpu_layers = gpu_layers
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
