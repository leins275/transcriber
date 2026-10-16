"""`engines.py`: the per-machine engine plan, as a pure function of the
accelerators, the config and what is on disk."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from transcription import engines
from transcription.hardware import Accelerators

NVIDIA = Accelerators(
    nvidia=True, apple_silicon=False, intel_npu=False, intel_gpu=True, physical_memory_bytes=None
)
APPLE = Accelerators(
    nvidia=False, apple_silicon=True, intel_npu=False, intel_gpu=False, physical_memory_bytes=None
)
INTEL = Accelerators(
    nvidia=False, apple_silicon=False, intel_npu=True, intel_gpu=True, physical_memory_bytes=None
)
INTEL_GPU_ONLY = Accelerators(
    nvidia=False, apple_silicon=False, intel_npu=False, intel_gpu=True, physical_memory_bytes=None
)
PLAIN = Accelerators(
    nvidia=False, apple_silicon=False, intel_npu=False, intel_gpu=False, physical_memory_bytes=None
)


def _presence(**overrides: bool) -> engines.Presence:
    values = dict(
        whisper_model=True,
        cuda_runtime=True,
        mlx_weights=True,
        openvino_runtime=True,
        openvino_weights=True,
        llm_gpu_build=True,
        diarization_runtime=True,
    )
    values.update(overrides)
    return engines.Presence(**values)


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "app_dir": Path("/app"),
        "whisper_backend": "auto",
        "whisper_openvino_device": "auto",
        "whisper_openvino_repo": "OpenVINO/whisper-large-v3-turbo-int8-ov",
        "whisper_mlx_repo": "mlx-community/whisper-large-v3-mlx",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rows(accel: Accelerators, present: engines.Presence, **config: Any) -> dict[str, Any]:
    return {row.stage: row for row in engines.plan_engines(_config(**config), accel, present)}


def test_nvidia_windows_keeps_the_cuda_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines.sys, "platform", "win32")
    rows = _rows(NVIDIA, _presence(cuda_runtime=False))

    assert (rows["whisper"].engine, rows["whisper"].device) == ("ctranslate2", "cuda")
    assert rows["whisper"].present is False
    assert (rows["llm"].engine, rows["llm"].device, rows["llm"].present) == (
        "llama.cpp",
        "cuda",
        True,
    )
    assert rows["diarization"].device == "cuda"
    assert rows["compress"].engine == "hevc_nvenc"


def test_apple_silicon_runs_mlx_metal_and_videotoolbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines.sys, "platform", "darwin")
    rows = _rows(APPLE, _presence())

    assert (rows["whisper"].engine, rows["whisper"].device) == ("mlx", "metal")
    assert rows["llm"].device == "metal"
    assert rows["diarization"].device == "mps"
    assert rows["compress"].engine == "hevc_videotoolbox"


def test_apple_silicon_without_the_mlx_weights_reports_the_engine_as_not_set_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engines.sys, "platform", "darwin")
    rows = _rows(APPLE, _presence(mlx_weights=False))

    assert (rows["whisper"].engine, rows["whisper"].present) == ("mlx", False)
    assert engines.resolve_whisper_backend(_config(), APPLE, _presence(mlx_weights=False)) == (
        "ctranslate2"
    )


def test_intel_windows_runs_openvino_on_the_npu_and_qsv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines.sys, "platform", "win32")
    rows = _rows(INTEL, _presence())

    assert (rows["whisper"].engine, rows["whisper"].device) == ("openvino", "NPU")
    assert rows["llm"].device == "cpu"
    assert rows["diarization"].device == "cpu"
    assert rows["compress"].engine == "hevc_qsv"
    assert engines.openvino_device_order(_config(), INTEL) == ("NPU", "GPU", "CPU")
    assert engines.openvino_device_order(_config(), INTEL_GPU_ONLY) == ("GPU", "CPU")
    assert engines.openvino_device_order(_config(whisper_openvino_device="CPU"), INTEL) == ("CPU",)


def test_openvino_is_offered_on_windows_intel_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines.sys, "platform", "win32")
    assert engines.openvino_offered(INTEL) is True
    assert engines.openvino_offered(INTEL_GPU_ONLY) is True
    assert engines.openvino_offered(NVIDIA) is False, "CUDA serves an NVIDIA machine better"
    assert engines.openvino_offered(PLAIN) is False
    monkeypatch.setattr(engines.sys, "platform", "linux")
    assert engines.openvino_offered(INTEL) is False


def test_intel_without_the_payload_falls_back_to_ctranslate2_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engines.sys, "platform", "win32")
    present = _presence(openvino_runtime=False)
    rows = _rows(INTEL, present)

    assert (rows["whisper"].engine, rows["whisper"].device, rows["whisper"].present) == (
        "openvino",
        "NPU",
        False,
    )
    assert engines.resolve_whisper_backend(_config(), INTEL, present) == "ctranslate2"


def test_a_plain_machine_runs_everything_on_the_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines.sys, "platform", "win32")
    rows = _rows(PLAIN, _presence(whisper_model=False))

    assert (rows["whisper"].engine, rows["whisper"].device, rows["whisper"].present) == (
        "ctranslate2",
        "cpu",
        False,
    )
    assert rows["llm"].device == "cpu"
    assert rows["compress"].engine == "libx265"


def test_an_explicit_backend_wins_over_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines.sys, "platform", "darwin")
    assert (
        engines.resolve_whisper_backend(_config(whisper_backend="ctranslate2"), APPLE, _presence())
        == "ctranslate2"
    )
    rows = _rows(APPLE, _presence(), whisper_backend="ctranslate2")
    assert (rows["whisper"].engine, rows["whisper"].device) == ("ctranslate2", "cpu")


def test_weights_dirs_hang_off_the_app_models_folder() -> None:
    config = _config()
    assert engines.mlx_weights_dir(config) == Path("/app/models/whisper-mlx/whisper-large-v3-mlx")
    assert engines.openvino_weights_dir(config) == Path(
        "/app/models/whisper-openvino/whisper-large-v3-turbo-int8-ov"
    )


def test_presence_stats_the_markers_without_importing_an_engine(tmp_path: Path) -> None:
    config = _config(app_dir=tmp_path, model_path=str(tmp_path / "models" / "whisper"))
    present = engines.presence(config)
    assert present == engines.Presence(
        whisper_model=False,
        cuda_runtime=False,
        mlx_weights=False,
        openvino_runtime=False,
        openvino_weights=False,
        llm_gpu_build=False,
        diarization_runtime=present.diarization_runtime,
    )
    marker = engines.mlx_weights_dir(config) / engines.READY_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    assert engines.presence(config).mlx_weights is True


def test_describe_plan_is_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines.sys, "platform", "darwin")
    line = engines.describe_plan(engines.plan_engines(_config(), APPLE, _presence()))
    assert line == (
        "whisper=mlx/metal (ready) llm=llama.cpp/metal (ready) "
        "diarization=pyannote/mps (ready) compress=hevc_videotoolbox/gpu"
    )
