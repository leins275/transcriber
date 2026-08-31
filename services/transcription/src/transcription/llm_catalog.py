"""The curated LLM model catalog.

The service supports exactly the GGUF models listed here -- deliberately just
one: there is no model switching, the Settings UI only downloads it. Each
entry pins a Hugging Face repo + revision + one file out of that repo
(GGUF repos carry many quants; downloading all of them would be hundreds of
GB), so verification always has a concrete digest set to compare against --
the same discipline as the whisper snapshot pin in ``model_download.py``.

``config.load_config`` resolves ``llm_model`` against this catalog and the
explicit ``llm_model_repo``/``llm_model_file`` escape hatch keeps working: an
operator can still point the config at any hand-picked GGUF, catalog or not.

Lives at the package top level (not under ``llm/``) so ``config.py`` and the
API routes can import it without touching the deliberately-lazy ``llm/``
package.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class CatalogEntry:
    """One curated GGUF model the in-app download knows how to fetch."""

    id: str
    label: str
    repo: str
    revision: str
    file: str
    # Exact GGUF byte size at the pinned revision (from the HF tree API);
    # display-only -- the download itself trusts the revision's metadata.
    size_bytes: int


CATALOG: tuple[CatalogEntry, ...] = (
    # Dense 9B: fully GPU-resident on a 12 GB card. Neither Qwen nor
    # ggml-org publishes a GGUF conversion of this one; unsloth's is the
    # canonical community conversion.
    CatalogEntry(
        id="qwen3.5-9b",
        label="Qwen3.5 9B",
        repo="unsloth/Qwen3.5-9B-GGUF",
        revision="3885219b6810b007914f3a7950a8d1b469d598a5",
        file="Qwen3.5-9B-Q5_K_M.gguf",
        size_bytes=6_577_841_376,
    ),
)

DEFAULT_ENTRY = CATALOG[0]
DEFAULT_MODEL_ID = DEFAULT_ENTRY.id

# The embedding model behind hybrid search -- deliberately NOT in `CATALOG`
# (that tuple is the LLM download UI; embeddings are infrastructure, not a
# choice). BGE-M3: multilingual (top-tier Russian+English retrieval), needs
# no query/passage instruction prefixes, XLM-RoBERTa architecture with
# long-standing llama.cpp support under the pinned llama-cpp-python.
EMBEDDING_ENTRY = CatalogEntry(
    id="bge-m3",
    label="BGE-M3 (search embeddings)",
    repo="gpustack/bge-m3-GGUF",
    revision="2d48f1737679ad900d5c26c5aad5410e9c70fdca",
    file="bge-m3-Q8_0.gguf",
    size_bytes=634_553_760,
)
# bge-m3's embedding width; `chunks_vec`'s vec0 column is declared with it
# and a dimension change drops + recreates that table.
EMBEDDING_DIM = 1024
# Ids the catalog used to carry (the 35B MoE, retired when model switching
# was removed). A config.json that still names one -- written by the old
# Settings switcher -- migrates to the default instead of failing to load.
RETIRED_MODEL_IDS: frozenset[str] = frozenset({"qwen3.6-35b-a3b"})


def get(model_id: str) -> CatalogEntry | None:
    """The catalog entry for ``model_id``, or ``None`` if not curated."""
    for entry in CATALOG:
        if entry.id == model_id:
            return entry
    return None


def known_ids() -> tuple[str, ...]:
    return tuple(entry.id for entry in CATALOG)
