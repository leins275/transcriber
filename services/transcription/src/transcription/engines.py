"""Which engine runs each stage on this machine -- the plan `/health` reports
and the whisper provider factory follows.

Four stages, one row each: transcription (whisper), summaries (the LLM),
speaker identification (pyannote) and video compression. The row names the
engine and the device it runs on, and whether what it needs is installed
(``present``; ``None`` where nothing is fetched for it). The plan is a pure
function of the config, the machine's :class:`~transcription.hardware.
Accelerators` and what is on disk (:class:`Presence`), so it is testable
without hardware and cheap enough for the ``/health`` request path (NFR-1:
a few ``stat`` calls, no imports of any engine library).

Whisper backends:

* ``ctranslate2`` -- faster-whisper, the universal fallback (CUDA on an
  NVIDIA machine, int8 on the CPU everywhere else);
* ``mlx`` -- mlx-whisper on the Apple Silicon GPU (baked into the macOS
  build; weights fetched at first run);
* ``openvino`` -- OpenVINO GenAI's WhisperPipeline on the Intel NPU (then
  GPU, then CPU; runtime and weights fetched at first run on an Intel
  machine without an NVIDIA GPU).

``whisper_backend: auto`` picks the accelerated one whose payload is on
disk and falls back to ``ctranslate2`` otherwise -- never an error.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from transcription.hardware import Accelerators

WHISPER_BACKENDS = ("auto", "ctranslate2", "mlx", "openvino")
OPENVINO_DEVICES = ("auto", "NPU", "GPU", "CPU")

# Where the accelerated engines' weights live, beside the whisper snapshot
# and the GGUFs: `<app_dir>/models/<subdir>/<repo basename>/`.
MLX_MODELS_SUBDIR = "whisper-mlx"
OPENVINO_MODELS_SUBDIR = "whisper-openvino"
READY_MARKER = ".ready"

# The provider-registry name each backend resolves to (`providers/__init__`).
PROVIDER_FOR_BACKEND = {
    "ctranslate2": "local",
    "mlx": "mlx",
    "openvino": "openvino",
}


@dataclass(frozen=True)
class EngineRow:
    stage: str
    engine: str
    device: str
    present: bool | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Presence:
    """What is on disk, as the plan needs it."""

    whisper_model: bool
    cuda_runtime: bool
    mlx_weights: bool
    openvino_runtime: bool
    openvino_weights: bool
    llm_gpu_build: bool
    diarization_runtime: bool


def _repo_basename(repo_id: str) -> str:
    return repo_id.rsplit("/", 1)[-1]


def mlx_weights_dir(config: Any) -> Path:
    return (
        Path(config.app_dir)
        / "models"
        / MLX_MODELS_SUBDIR
        / _repo_basename(config.whisper_mlx_repo)
    )


def openvino_weights_dir(config: Any) -> Path:
    return (
        Path(config.app_dir)
        / "models"
        / OPENVINO_MODELS_SUBDIR
        / _repo_basename(config.whisper_openvino_repo)
    )


def presence(config: Any) -> Presence:
    """Stat the markers; never imports an engine."""
    from transcription.api.model_routes import is_model_present  # noqa: PLC0415 - cycle
    from transcription.cuda_runtime import is_cuda_runtime_present  # noqa: PLC0415
    from transcription.diarization_runtime import runtime_available  # noqa: PLC0415
    from transcription.llm.runtime_fetch import is_llama_cuda_present  # noqa: PLC0415
    from transcription.openvino_runtime import is_openvino_runtime_present  # noqa: PLC0415

    return Presence(
        whisper_model=is_model_present(config),
        cuda_runtime=is_cuda_runtime_present(config.app_dir),
        mlx_weights=(mlx_weights_dir(config) / READY_MARKER).exists(),
        openvino_runtime=is_openvino_runtime_present(config.app_dir),
        openvino_weights=(openvino_weights_dir(config) / READY_MARKER).exists(),
        llm_gpu_build=is_llama_cuda_present(config.app_dir),
        diarization_runtime=runtime_available(config.app_dir),
    )


def openvino_offered(accel: Accelerators) -> bool:
    """Whether the OpenVINO whisper engine is worth fetching here: Windows
    (the runtime manifest pins win_amd64 wheels), an Intel NPU or GPU to
    run it on, and no NVIDIA GPU -- which CUDA serves better."""
    return bool(
        sys.platform == "win32" and (accel.intel_npu or accel.intel_gpu) and not accel.nvidia
    )


def mlx_offered(accel: Accelerators) -> bool:
    return accel.apple_silicon


def preferred_whisper_backend(accel: Accelerators) -> str:
    """The backend this machine *should* run, payload or not."""
    if mlx_offered(accel):
        return "mlx"
    if openvino_offered(accel):
        return "openvino"
    return "ctranslate2"


def resolve_whisper_backend(config: Any, accel: Accelerators, present: Presence) -> str:
    """The backend that actually runs: the configured one when it is
    explicit, else the preferred one when its payload is on disk, else
    ``ctranslate2``."""
    configured = getattr(config, "whisper_backend", "auto") or "auto"
    if configured != "auto":
        return str(configured)
    preferred = preferred_whisper_backend(accel)
    if preferred == "mlx" and present.mlx_weights:
        return "mlx"
    if preferred == "openvino" and present.openvino_runtime and present.openvino_weights:
        return "openvino"
    return "ctranslate2"


def openvino_device_order(config: Any, accel: Accelerators) -> tuple[str, ...]:
    """The devices the OpenVINO provider tries, best first."""
    configured = getattr(config, "whisper_openvino_device", "auto") or "auto"
    if configured != "auto":
        return (str(configured),)
    order: list[str] = []
    if accel.intel_npu:
        order.append("NPU")
    if accel.intel_gpu:
        order.append("GPU")
    order.append("CPU")
    return tuple(order)


def _whisper_row(config: Any, accel: Accelerators, present: Presence) -> EngineRow:
    backend = resolve_whisper_backend(config, accel, present)
    if backend == "mlx":
        return EngineRow("whisper", "mlx", "metal", present.mlx_weights)
    if backend == "openvino":
        device = openvino_device_order(config, accel)[0]
        return EngineRow(
            "whisper", "openvino", device, present.openvino_runtime and present.openvino_weights
        )
    if accel.nvidia and sys.platform == "win32":
        return EngineRow("whisper", "ctranslate2", "cuda", present.cuda_runtime)
    # ctranslate2 on the CPU: the fallback. When an accelerated engine is
    # preferred but not fetched yet, say so (`present: False`) so the app
    # can offer the setup.
    preferred = preferred_whisper_backend(accel)
    if preferred != "ctranslate2" and (getattr(config, "whisper_backend", "auto") or "auto") == (
        "auto"
    ):
        device = "metal" if preferred == "mlx" else openvino_device_order(config, accel)[0]
        return EngineRow("whisper", preferred, device, False)
    return EngineRow("whisper", "ctranslate2", "cpu", present.whisper_model)


def _llm_row(accel: Accelerators, present: Presence) -> EngineRow:
    if accel.nvidia and sys.platform == "win32":
        return EngineRow("llm", "llama.cpp", "cuda", present.llm_gpu_build)
    if accel.apple_silicon:
        return EngineRow("llm", "llama.cpp", "metal", True)
    return EngineRow("llm", "llama.cpp", "cpu", True)


def _diarization_row(accel: Accelerators, present: Presence) -> EngineRow:
    if accel.nvidia and sys.platform == "win32":
        device = "cuda"
    elif accel.apple_silicon:
        device = "mps"
    else:
        device = "cpu"
    return EngineRow("diarization", "pyannote", device, present.diarization_runtime)


def _compress_row(accel: Accelerators) -> EngineRow:
    if accel.nvidia:
        return EngineRow("compress", "hevc_nvenc", "gpu", None)
    if sys.platform == "darwin":
        return EngineRow("compress", "hevc_videotoolbox", "gpu", None)
    if sys.platform == "win32" and accel.intel_gpu:
        return EngineRow("compress", "hevc_qsv", "gpu", None)
    return EngineRow("compress", "libx265", "cpu", None)


def plan_engines(config: Any, accel: Accelerators, present: Presence) -> list[EngineRow]:
    """One row per stage, in display order."""
    return [
        _whisper_row(config, accel, present),
        _llm_row(accel, present),
        _diarization_row(accel, present),
        _compress_row(accel),
    ]


def describe_plan(rows: list[EngineRow]) -> str:
    """One log line: ``whisper=mlx/metal (ready) llm=llama.cpp/metal ...``."""
    parts = []
    for row in rows:
        suffix = "" if row.present is None else (" (ready)" if row.present else " (not set up)")
        parts.append(f"{row.stage}={row.engine}/{row.device}{suffix}")
    return " ".join(parts)
