"""First-run acquisition of the OpenVINO whisper runtime (Intel NPU / GPU).

The installer never bakes `openvino-genai`: it only helps on a Windows
machine with an Intel NPU or GPU and no NVIDIA GPU (where CUDA serves
better), so the service fetches it there on first run, the way it fetches
the CUDA STT runtime, the CUDA llama.cpp build and the speaker runtime.
Every archive pinned in `openvino_runtime_packages.py` (generated from
``uv.lock`` by ``scripts/gen_openvino_runtime.py``) rides
:class:`~transcription.cuda_runtime.CudaRuntimeDownload` (resume, digest
verification, cancel, ``.ready`` marker) into
``<app_dir>/runtime/whisper-openvino/``, which :func:`activate_runtime`
puts on ``sys.path`` before ``providers/openvino_whisper.py`` imports it.

The weights are a separate payload: a pre-exported OpenVINO IR from the
hub (`config.whisper_openvino_repo`), snapshotted by the ordinary
:class:`~transcription.model_download.ModelDownload` into
``<app_dir>/models/whisper-openvino/<repo basename>/``.
"""

from __future__ import annotations

import json
import logging
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

from transcription.cuda_runtime import RUNTIME_DIRNAME, CudaPackage, CudaRuntimeDownload
from transcription.openvino_runtime_packages import OPENVINO_WHEELS, TOTAL_BYTES

if TYPE_CHECKING:
    from transcription.config import Config
    from transcription.model_download import ModelDownload, Transport

logger = logging.getLogger("transcription")

# A directory name under `<app_dir>/runtime/`, not an importable path:
# imports resolve `openvino`, `openvino_genai`, ... *inside* it.
OPENVINO_SUBDIR = "whisper-openvino"
_MARKER_RELPATH = f"{OPENVINO_SUBDIR}/.ready"

OPENVINO_PACKAGES: tuple[CudaPackage, ...] = tuple(
    CudaPackage(
        name=wheel.name,
        version=wheel.version,
        filename=wheel.filename,
        url=wheel.url,
        size=wheel.size,
        sha256=wheel.sha256,
        extract_prefix=wheel.extract_prefix,
        dest_subdir=OPENVINO_SUBDIR,
        archive_root=wheel.archive_root,
    )
    for wheel in OPENVINO_WHEELS
)


def openvino_runtime_dir(app_dir: str | Path) -> Path:
    return Path(app_dir) / RUNTIME_DIRNAME / OPENVINO_SUBDIR


def runtime_total_bytes() -> int:
    return TOTAL_BYTES


def _manifest_versions() -> dict[str, str]:
    return {pkg.name: pkg.version for pkg in OPENVINO_PACKAGES}


def is_openvino_runtime_present(app_dir: str | Path) -> bool:
    """Whether a prior fetch landed *this* manifest (a re-pinned build
    reads an older tree as absent and replaces it, like the speaker
    runtime)."""
    marker = Path(app_dir) / RUNTIME_DIRNAME / _MARKER_RELPATH
    try:
        if not marker.is_file():
            return False
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("packages") == _manifest_versions()


def openvino_importable() -> bool:
    """A dev environment synced with ``--extra whisper-openvino``."""
    try:
        return find_spec("openvino_genai") is not None
    except (ImportError, ValueError):
        return False


def runtime_available(app_dir: str | Path) -> bool:
    return is_openvino_runtime_present(app_dir) or openvino_importable()


def activate_runtime(app_dir: str | Path) -> Path | None:
    """Put a fetched runtime on ``sys.path`` (once); ``None`` when none has
    been fetched. The manifest only holds packages the baked environment
    lacks, so the front position shadows nothing."""
    runtime_dir = openvino_runtime_dir(app_dir)
    if not is_openvino_runtime_present(app_dir):
        return None
    entry = str(runtime_dir)
    if entry not in sys.path:
        sys.path.insert(0, entry)
        logger.info(
            "openvino runtime activated",
            extra={"event": "openvino_runtime_activated", "path": entry},
        )
    return runtime_dir


def build_openvino_runtime_download(
    config: Config, *, transport: Transport | None = None
) -> CudaRuntimeDownload:
    """The runtime fetch, on the same machinery as the CUDA runtime. A
    tree from an older manifest is cleared first so the extraction never
    mixes two pins."""
    import shutil  # noqa: PLC0415 - only the replace path needs it

    runtime_dir = openvino_runtime_dir(config.app_dir)
    if runtime_dir.is_dir() and not is_openvino_runtime_present(config.app_dir):
        shutil.rmtree(runtime_dir, ignore_errors=True)
    return CudaRuntimeDownload(
        app_dir=config.app_dir,
        allowed_roots=(Path(config.app_dir),),
        packages=OPENVINO_PACKAGES,
        transport=transport,
        marker_relpath=_MARKER_RELPATH,
    )


def build_openvino_weights_download(
    config: Config, *, transport: Transport | None = None
) -> ModelDownload:
    """The pre-exported IR snapshot, pinned by `config.whisper_openvino_revision`."""
    from transcription.engines import openvino_weights_dir  # noqa: PLC0415 - cycle
    from transcription.model_download import ModelDownload  # noqa: PLC0415

    return ModelDownload(
        models_dir=openvino_weights_dir(config),
        allowed_roots=(Path(config.app_dir),),
        repo_id=config.whisper_openvino_repo,
        revision=config.whisper_openvino_revision,
        transport=transport,
    )
