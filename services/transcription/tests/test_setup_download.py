"""Tests for `SetupDownload` and `build_setup_download` (Defect 1 fix):
the combined CUDA-runtime-then-model first-run acquisition that
`ModelDownloadManager`'s default factory wires in, entirely offline via
small fakes for both phases -- neither a real hub call nor a real PyPI
fetch happens anywhere in this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from transcription.api import model_routes
from transcription.errors import ErrorKind, ServiceError
from transcription.model_download import DownloadState


class _FakePhase:
    """Stands in for either `CudaRuntimeDownload` or `ModelDownload`: records
    every `start()` call and lets the test script state transitions."""

    def __init__(
        self,
        *,
        total_bytes: int,
        final_state: DownloadState,
        downloaded: int | None = None,
        already_present: bool = False,
    ) -> None:
        self.state = DownloadState.IDLE
        self.total_bytes = total_bytes
        self.downloaded_bytes = 0
        self.current_file = "phase.file"
        self.error: ServiceError | None = None
        self._final_state = final_state
        self._final_downloaded = downloaded if downloaded is not None else total_bytes
        self._already_present = already_present
        self.start_calls = 0
        self.cancel_calls = 0
        # Only relevant for the model phase (SetupDownload reads these).
        self.repo_id = "Fake/repo"
        self.revision = "deadbeef"

    def start(self, on_progress, *, progress_interval_sec=1.0) -> None:
        self.start_calls += 1
        self.state = DownloadState.DOWNLOADING
        on_progress({})
        self.downloaded_bytes = self._final_downloaded
        self.state = self._final_state
        if self._final_state is DownloadState.ERROR:
            self.error = ServiceError(ErrorKind.INTERNAL, "phase failed")
        on_progress({})

    def cancel(self) -> None:
        self.cancel_calls += 1

    def already_present(self) -> bool:
        # Only relevant for the model phase (SetupDownload reads this, E13).
        return self._already_present


def test_start_runs_cuda_runtime_then_model_and_sums_bytes() -> None:
    runtime = _FakePhase(total_bytes=1000, final_state=DownloadState.COMPLETE)
    model = _FakePhase(total_bytes=2000, final_state=DownloadState.COMPLETE)
    setup = model_routes.SetupDownload(cuda_runtime=runtime, model=model)  # type: ignore[arg-type]

    events: list[dict[str, Any]] = []
    setup.start(on_progress=events.append, progress_interval_sec=0.0)

    assert runtime.start_calls == 1
    assert model.start_calls == 1
    assert setup.state == DownloadState.COMPLETE
    assert setup.downloaded_bytes == 3000
    assert setup.total_bytes == 3000
    assert events[-1]["downloaded_bytes"] == 3000
    assert events[-1]["total_bytes"] == 3000
    assert events[-1]["state"] == "complete"


def test_a_failed_cuda_runtime_phase_continues_to_the_model_phase_as_a_warning() -> None:
    """E4: a failed CUDA-runtime fetch (network drop, digest mismatch) must
    not block the model the operator actually needs -- it becomes a
    non-fatal `cuda_warning`, and the model phase still runs and can still
    succeed."""
    runtime = _FakePhase(total_bytes=1000, final_state=DownloadState.ERROR, downloaded=400)
    model = _FakePhase(total_bytes=2000, final_state=DownloadState.COMPLETE)
    setup = model_routes.SetupDownload(cuda_runtime=runtime, model=model)  # type: ignore[arg-type]

    setup.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert runtime.start_calls == 1
    assert model.start_calls == 1
    assert setup.state == DownloadState.COMPLETE
    assert setup.error is None, "the model phase succeeding must read as an overall success"
    assert setup.cuda_warning is not None
    assert setup.cuda_warning.message == "phase failed"
    assert setup.downloaded_bytes == 400 + 2000


def test_a_retry_of_the_cuda_phase_skips_the_model_phase_when_already_present() -> None:
    """E13: "Retry GPU setup" re-POSTs `/v1/model/download`, which builds a
    *fresh* `SetupDownload` (a fresh `ModelDownload` included). If the model
    already landed in an earlier run, the model phase must not run at all --
    `ModelDownload.start()` has no already-present short-circuit of its own
    and would otherwise re-fetch every file from byte zero."""
    runtime = _FakePhase(total_bytes=1000, final_state=DownloadState.ERROR, downloaded=400)
    model = _FakePhase(total_bytes=2000, final_state=DownloadState.COMPLETE, already_present=True)
    setup = model_routes.SetupDownload(cuda_runtime=runtime, model=model)  # type: ignore[arg-type]

    events: list[dict[str, Any]] = []
    setup.start(on_progress=events.append, progress_interval_sec=0.0)

    assert model.start_calls == 0, "an already-present model must never be re-fetched"
    assert setup.state == DownloadState.COMPLETE
    assert setup.cuda_warning is not None
    assert setup.cuda_warning.message == "phase failed"
    assert events[-1]["state"] == "complete"
    assert events[-1]["cuda_warning"] == "phase failed"


def test_a_cancelled_cuda_runtime_phase_never_starts_the_model_phase() -> None:
    runtime = _FakePhase(total_bytes=1000, final_state=DownloadState.CANCELLED, downloaded=100)
    model = _FakePhase(total_bytes=2000, final_state=DownloadState.COMPLETE)
    setup = model_routes.SetupDownload(cuda_runtime=runtime, model=model)  # type: ignore[arg-type]

    setup.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert model.start_calls == 0
    assert setup.state == DownloadState.CANCELLED


def test_cancel_propagates_to_both_phases() -> None:
    runtime = _FakePhase(total_bytes=1000, final_state=DownloadState.CANCELLED)
    model = _FakePhase(total_bytes=2000, final_state=DownloadState.COMPLETE)
    setup = model_routes.SetupDownload(cuda_runtime=runtime, model=model)  # type: ignore[arg-type]

    setup.cancel()

    assert runtime.cancel_calls == 1
    assert model.cancel_calls == 1


def test_repo_id_and_revision_proxy_the_model_phase() -> None:
    runtime = _FakePhase(total_bytes=1000, final_state=DownloadState.COMPLETE)
    model = _FakePhase(total_bytes=2000, final_state=DownloadState.COMPLETE)
    setup = model_routes.SetupDownload(cuda_runtime=runtime, model=model)  # type: ignore[arg-type]

    assert setup.repo_id == model.repo_id
    assert setup.revision == model.revision


# --- build_setup_download's platform/device gating -------------------------


class _FakeConfig:
    def __init__(self, *, device: str, app_dir: Path, model_path: Path) -> None:
        self.device = device
        self.app_dir = app_dir
        self.model_path = model_path


def _fake_config(tmp_path: Path, *, device: str) -> _FakeConfig:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    return _FakeConfig(device=device, app_dir=app_dir, model_path=app_dir / "models" / "x")


def test_nvidia_gpu_present_reflects_nvidia_smi_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """E4: the GPU-presence probe for the *download decision* is
    `nvidia-smi`'s mere presence on `PATH` -- independent of
    `ctranslate2.get_cuda_device_count()`, which this repo's own T14 pass
    found reports a device even when the CUDA runtime cannot load."""
    monkeypatch.setattr(model_routes.shutil, "which", lambda name: None)
    assert model_routes._nvidia_gpu_present() is False

    monkeypatch.setattr(
        model_routes.shutil, "which", lambda name: r"C:\Windows\System32\nvidia-smi.exe"
    )
    assert model_routes._nvidia_gpu_present() is True


def test_build_setup_download_skips_cuda_runtime_on_non_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_routes.sys, "platform", "linux")
    config = _fake_config(tmp_path, device="auto")

    result = model_routes.build_setup_download(config)  # type: ignore[arg-type]

    assert isinstance(result, model_routes.ModelDownload)


def test_build_setup_download_skips_cuda_runtime_when_device_is_explicitly_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_routes.sys, "platform", "win32")
    config = _fake_config(tmp_path, device="cpu")

    result = model_routes.build_setup_download(config)  # type: ignore[arg-type]

    assert isinstance(result, model_routes.ModelDownload)


def test_build_setup_download_wraps_in_setup_download_on_windows_with_gpu_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_routes.sys, "platform", "win32")
    monkeypatch.setattr(model_routes, "_nvidia_gpu_present", lambda: True)
    config = _fake_config(tmp_path, device="auto")

    result = model_routes.build_setup_download(config)  # type: ignore[arg-type]

    assert isinstance(result, model_routes.SetupDownload)


def test_build_setup_download_skips_cuda_runtime_when_no_nvidia_gpu_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E4: a Windows machine with `device: auto` (the default) but no NVIDIA
    GPU at all must never fetch ~1.4 GB of CUDA wheels it could never use --
    gating on `device != "cpu"` alone (the previous behaviour) could not
    distinguish this from a real GPU machine."""
    monkeypatch.setattr(model_routes.sys, "platform", "win32")
    monkeypatch.setattr(model_routes, "_nvidia_gpu_present", lambda: False)
    config = _fake_config(tmp_path, device="auto")

    result = model_routes.build_setup_download(config)  # type: ignore[arg-type]

    assert isinstance(result, model_routes.ModelDownload)
