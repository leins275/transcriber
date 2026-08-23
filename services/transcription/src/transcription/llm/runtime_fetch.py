"""First-run acquisition of the CUDA build of llama.cpp (GPU offload).

The installer bakes the CPU-only llama-cpp-python wheel (the `llm-cpu`
extra) because the CUDA build is ~480 MB of GPU-specific payload most
machines cannot use -- the same reasoning that keeps the CUDA STT runtime
out of the bake. On a machine with an NVIDIA GPU, this module's pinned
package list rides the existing :class:`~transcription.cuda_runtime.
CudaRuntimeDownload` machinery (resume, digest verification, cancel,
``.ready`` marker) to fetch, at the operator's request:

- the ``cu124`` build of llama-cpp-python (a GitHub release wheel; only its
  ``llama_cpp/`` package tree is extracted, into
  ``<app_dir>/runtime/llama-cuda/``), and
- the ``nvidia-cuda-runtime-cu12`` wheel (``cudart64_12.dll`` -- the one
  CUDA DLL the STT runtime download does not already provide; extracted
  into the shared ``runtime/nvidia/`` tree ``runtime_dlls`` registers).

:mod:`transcription.llm.llama_cpp_local` prefers ``runtime/llama-cuda`` on
``sys.path`` over the baked CPU build when the directory exists, falling
back to the CPU build if the CUDA one cannot load.

This module lives under ``llm/`` (not beside ``cuda_runtime.py``) because
it must name the llama.cpp wheel, and LLM-library names are confined to
this package by the attribution grep test.
"""

from __future__ import annotations

from pathlib import Path

from transcription.config import Config
from transcription.cuda_runtime import RUNTIME_DIRNAME, CudaPackage, CudaRuntimeDownload
from transcription.model_download import Transport

# The subdirectory of `<app_dir>/runtime/` the CUDA llama.cpp package tree
# lands in. Deliberately spelled with a dash: it is a directory name, not
# an importable package path -- imports resolve `llama_cpp` *inside* it.
LLAMA_CUDA_SUBDIR = "llama-cuda"

_MARKER_RELPATH = f"{LLAMA_CUDA_SUBDIR}/.ready"

# Pinned to the same version the `llm-cpu`/`llm-cuda` extras pin, with the
# size/sha256 the GitHub release and PyPI report for these exact artifacts
# (the download verifies both, so a drifted pin fails loudly, never
# silently).
LLAMA_CUDA_PACKAGES: tuple[CudaPackage, ...] = (
    CudaPackage(
        name="llama-cpp-python-cu124",
        version="0.3.35",
        filename="llama_cpp_python-0.3.35-py3-none-win_amd64.whl",
        url=(
            "https://github.com/abetlen/llama-cpp-python/releases/download/"
            "v0.3.35-cu124/llama_cpp_python-0.3.35-py3-none-win_amd64.whl"
        ),
        size=482736710,
        sha256="84f7218c1e9cf21014b9770c65064c5a3674dd2f4fbc312c5d6bb40cec0fb269",
        extract_prefix="llama_cpp/",
        dest_subdir=LLAMA_CUDA_SUBDIR,
    ),
    CudaPackage(
        name="nvidia-cuda-runtime-cu12",
        version="12.9.79",
        filename="nvidia_cuda_runtime_cu12-12.9.79-py3-none-win_amd64.whl",
        url=(
            "https://files.pythonhosted.org/packages/59/df/"
            "e7c3a360be4f7b93cee39271b792669baeb3846c58a4df6dfcf187a7ffab/"
            "nvidia_cuda_runtime_cu12-12.9.79-py3-none-win_amd64.whl"
        ),
        size=3591604,
        sha256="8e018af8fa02363876860388bd10ccb89eb9ab8fb0aa749aaf58430a9f7c4891",
    ),
)


def llama_cuda_dir(app_dir: str | Path) -> Path:
    """Where the CUDA llama.cpp package tree lives once fetched."""
    return Path(app_dir) / RUNTIME_DIRNAME / LLAMA_CUDA_SUBDIR


def is_llama_cuda_present(app_dir: str | Path) -> bool:
    """Whether a prior fetch's ``.ready`` marker exists."""
    return (Path(app_dir) / RUNTIME_DIRNAME / _MARKER_RELPATH).exists()


def build_llama_cuda_download(
    config: Config, *, transport: Transport | None = None
) -> CudaRuntimeDownload:
    """The GPU-build fetch, on the same machinery as the STT CUDA runtime."""
    return CudaRuntimeDownload(
        app_dir=config.app_dir,
        allowed_roots=(Path(config.app_dir),),
        packages=LLAMA_CUDA_PACKAGES,
        transport=transport,
        marker_relpath=_MARKER_RELPATH,
    )
