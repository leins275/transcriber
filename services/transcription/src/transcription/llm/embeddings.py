"""The llama.cpp embedding engine behind hybrid search.

The only new module that touches ``llama_cpp`` (the attribution test keeps
LLM libraries confined to ``llm/``). Deliberately CPU-only
(``n_gpu_layers=0``): a ~600 MB Q8 XLM-RoBERTa embeds a 512-token chunk in
tens of milliseconds on CPU, and pinning it off the GPU means indexing never
competes with whisper or the LLM for VRAM.

Loading is lazy and lock-guarded like ``llama_cpp_local.py``; the weights
are the pinned ``llm_catalog.EMBEDDING_ENTRY`` GGUF living in
``config.llm_model_path`` beside the LLM's GGUF.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
from pathlib import Path
from typing import Any

from transcription import llm_catalog
from transcription.errors import ErrorKind, ServiceError, redact
from transcription.llm.runtime_fetch import llama_cuda_dir

logger = logging.getLogger("transcription")

# llama.h's LLAMA_POOLING_TYPE_CLS; bge-m3 is CLS-pooled and GGUF metadata
# on pooling is not trustworthy across converters, so it is pinned here.
_POOLING_TYPE_CLS = 2

# Room left below n_ctx when defensively truncating an over-long input.
_TRUNCATION_MARGIN_TOKENS = 8

_EMBED_CTX_TOKENS = 2048


class LlamaCppEmbedder:
    """Lazy-loaded llama.cpp embedding engine (bge-m3 by default)."""

    name = "llama_cpp_embedding"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._llama: Any = None
        self._lock = threading.Lock()

    def model_file(self) -> Path:
        """The GGUF file this embedder will load (may not exist yet)."""
        return Path(str(self.config.llm_model_path)) / str(self.config.embedding_model_file)

    def dim(self) -> int:
        return int(llm_catalog.EMBEDDING_DIM)

    def _import_llama_cpp(self) -> Any:
        """Import llama_cpp; whichever build (CPU or first-run CUDA) the LLM
        provider already put in ``sys.modules`` is reused -- the embedder
        runs with ``n_gpu_layers=0`` either way."""
        if "llama_cpp" in sys.modules:
            return sys.modules["llama_cpp"]
        runtime_dir = llama_cuda_dir(self.config.app_dir)
        if (runtime_dir / "llama_cpp").is_dir():
            sys.path.insert(0, str(runtime_dir))
            try:
                import llama_cpp  # noqa: PLC0415

                return llama_cpp
            except Exception:
                sys.path.remove(str(runtime_dir))
                sys.modules.pop("llama_cpp", None)
        import llama_cpp  # noqa: PLC0415

        return llama_cpp

    def _load(self) -> Any:
        with self._lock:
            if self._llama is not None:
                return self._llama

            model_file = self.model_file()
            if not model_file.is_file():
                raise ServiceError(
                    ErrorKind.MODEL_LOAD,
                    f"embedding model file not found: {model_file.name}; "
                    "download it first (POST /v1/embedding-model/download)",
                )
            try:
                llama_cpp = self._import_llama_cpp()
                self._llama = llama_cpp.Llama(
                    model_path=str(model_file),
                    n_ctx=_EMBED_CTX_TOKENS,
                    n_batch=_EMBED_CTX_TOKENS,
                    n_ubatch=_EMBED_CTX_TOKENS,
                    n_gpu_layers=0,
                    embedding=True,
                    pooling_type=getattr(llama_cpp, "LLAMA_POOLING_TYPE_CLS", _POOLING_TYPE_CLS),
                    verbose=False,
                )
            except ServiceError:
                raise
            except Exception as exc:
                raise ServiceError(
                    ErrorKind.MODEL_LOAD,
                    f"failed to load embedding model {model_file.name}: {redact(str(exc))}",
                ) from exc
            logger.info(
                "embedding model loaded",
                extra={"event": "embedder_loaded", "model": model_file.name},
            )
            return self._llama

    def _truncate(self, llama: Any, text: str) -> str:
        """Defensively cap ``text`` below the embed context. Index chunks are
        budgeted well under it; this guards the pathological monster line."""
        limit = _EMBED_CTX_TOKENS - _TRUNCATION_MARGIN_TOKENS
        try:
            tokens = llama.tokenize(text.encode("utf-8"), add_bos=False, special=False)
            if len(tokens) <= limit:
                return text
            clipped: bytes = llama.detokenize(tokens[:limit])
            return str(clipped.decode("utf-8", errors="ignore"))
        except Exception:
            # Character fallback, deliberately harsher than any tokenizer.
            return text[: limit * 2]

    def embed(self, texts: list[str]) -> list[list[float]]:
        llama = self._load()
        vectors: list[list[float]] = []
        for text in texts:
            raw = llama.embed(self._truncate(llama, text))
            vector = [float(value) for value in raw]
            norm = math.sqrt(sum(value * value for value in vector))
            if norm > 0.0 and math.isfinite(norm):
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors

    def unload(self) -> None:
        with self._lock:
            if self._llama is None:
                return
            llama, self._llama = self._llama, None
        close = getattr(llama, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.warning("embedding model close() failed", exc_info=True)
        logger.info("embedding model unloaded", extra={"event": "embedder_unloaded"})
