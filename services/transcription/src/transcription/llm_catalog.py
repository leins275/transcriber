"""The curated LLM model catalog.

The service supports exactly the GGUF models listed here; the Settings UI
lists them and lets the operator download, delete and switch between them.
Each entry pins a Hugging Face repo + revision + one file out of that repo
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
    # Dense 9B: fully GPU-resident on a 12 GB card, which makes it several
    # times faster end-to-end than the (better, but partially CPU-offloaded)
    # 35B MoE below. Neither Qwen nor ggml-org publishes a GGUF conversion
    # of this one; unsloth's is the canonical community conversion.
    CatalogEntry(
        id="qwen3.5-9b",
        label="Qwen3.5 9B",
        repo="unsloth/Qwen3.5-9B-GGUF",
        revision="3885219b6810b007914f3a7950a8d1b469d598a5",
        file="Qwen3.5-9B-Q5_K_M.gguf",
        size_bytes=6_577_841_376,
    ),
    # MoE 35B (3B active): higher quality, but ~20 GB of weights means the
    # MoE-blind GPU auto-fit leaves most of it on the CPU on consumer cards.
    CatalogEntry(
        id="qwen3.6-35b-a3b",
        label="Qwen3.6 35B A3B",
        repo="ggml-org/Qwen3.6-35B-A3B-GGUF",
        revision="baec3ebee244827cda0f4557eafa8b28f7545fa6",
        file="Qwen3.6-35B-A3B-Q4_K_M.gguf",
        size_bytes=20_419_565_568,
    ),
)

DEFAULT_ENTRY = CATALOG[0]
DEFAULT_MODEL_ID = DEFAULT_ENTRY.id
# Installs from before the catalog existed have this model on disk and no
# `llm_model` key in config.json; `load_config` keeps them on it (see the
# migration probe there) instead of surprise-downloading the new default.
LEGACY_ENTRY = CATALOG[1]
LEGACY_MODEL_ID = LEGACY_ENTRY.id


def get(model_id: str) -> CatalogEntry | None:
    """The catalog entry for ``model_id``, or ``None`` if not curated."""
    for entry in CATALOG:
        if entry.id == model_id:
            return entry
    return None


def known_ids() -> tuple[str, ...]:
    return tuple(entry.id for entry in CATALOG)
