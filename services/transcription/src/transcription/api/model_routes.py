"""HTTP routes for the model download core (FR-12, FR-17).

Thin routing over :mod:`transcription.model_download` -- no download logic
lives here beyond orchestrating a single background thread per process (the
HTTP request must return immediately, FR-12) and translating a
:class:`~transcription.model_download.ModelDownload`'s state into the wire
shape both a first-run wizard and the CLI can poll. Exactly one download
exists at a time (:class:`ModelDownloadManager`); a second ``POST`` while
one is already running is a no-op that returns the current status, never a
second parallel transfer.

:func:`build_download` is the one place that decides the allowed root for
the model payload: the app folder itself (``config.app_dir``), never the
vault allowlist used for job audio/output paths (F2 FR-9) -- so both this
module's routes and ``cli.py``'s ``download-model`` subcommand share the
exact same path-validation behaviour.
"""

from __future__ import annotations

import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from transcription.config import Config
from transcription.cuda_runtime import CudaRuntimeDownload
from transcription.errors import ErrorKind, ServiceError
from transcription.model_download import (
    DownloadState,
    HubClient,
    ModelDownload,
    Transport,
)


def default_allowed_roots(config: Config) -> tuple[Path, ...]:
    """The model payload's allowlist is the app folder itself -- never the
    vault roots configured for job audio/output paths (F2 FR-9)."""
    return (Path(config.app_dir),)


def build_download(
    config: Config,
    *,
    out_dir: str | None = None,
    hub_client: HubClient | None = None,
    transport: Transport | None = None,
) -> ModelDownload:
    """Construct one :class:`ModelDownload` for ``config``, or an explicit
    ``out_dir`` override (the CLI's ``--out``)."""
    return ModelDownload(
        models_dir=out_dir or config.model_path,
        allowed_roots=default_allowed_roots(config),
        hub_client=hub_client,
        transport=transport,
    )


def build_cuda_runtime_download(
    config: Config, *, transport: Transport | None = None
) -> CudaRuntimeDownload:
    """Construct one :class:`CudaRuntimeDownload` for ``config`` (Defect 1)."""
    return CudaRuntimeDownload(
        app_dir=config.app_dir,
        allowed_roots=default_allowed_roots(config),
        transport=transport,
    )


def _nvidia_gpu_present() -> bool:
    """Best-effort "is there an NVIDIA GPU on this machine at all" probe
    (E4) -- deliberately independent of whether the CUDA *runtime*
    (cuBLAS/cuDNN) is installed, since that is exactly what this probe
    decides whether to go fetch. `nvidia-smi` ships with every NVIDIA
    display-driver install (not with CUDA itself, and not with
    `ctranslate2`), so its mere presence on `PATH` is a cheap, honest
    GPU-presence signal that needs no driver query, no subprocess/timeout
    handling and no admin rights -- unlike
    `ctranslate2.get_cuda_device_count()`, which this repo's own T14 pass
    found reports a device even when cuBLAS cannot be loaded (the wrong
    question for *this* decision: "will the runtime work", not "is there a
    GPU").
    """
    return shutil.which("nvidia-smi") is not None


def build_setup_download(config: Config) -> ModelDownload | SetupDownload:
    """The real, production first-run download: the model alone on a
    platform/device/machine that never needs the CUDA runtime, or the
    combined :class:`SetupDownload` (CUDA runtime, then model) otherwise
    (Defect 1, E4).

    Only wired as the *default* factory `ModelDownloadManager` builds when
    nothing else is supplied (`app.py`'s real server startup) -- every test
    that exercises `/v1/model/download` supplies its own
    `model_download_factory` and never reaches this function, so none of
    them make a real network call for the CUDA runtime.
    """
    model = build_download(config)
    if sys.platform != "win32" or getattr(config, "device", "auto") == "cpu":
        # Out of scope for the MVP (Windows-only, CPU is best-effort) --
        # never fetch a runtime nothing on this configuration would use.
        return model
    if not _nvidia_gpu_present():
        # E4: no NVIDIA GPU at all -- never fetch ~1.4 GB of CUDA wheels a
        # machine with no GPU could never use, and never let that fetch's
        # failure (on a flaky connection) block the model download the
        # operator actually needs.
        return model
    cuda_runtime = build_cuda_runtime_download(config)
    return SetupDownload(cuda_runtime=cuda_runtime, model=model)


def is_model_present(config: Config) -> bool:
    """Whether the pinned snapshot's ``.ready`` marker exists under
    ``config.model_path`` (FR-17: the app must detect a missing model
    without guessing).

    ``config.model_path`` is itself the literal, already-model-specific
    snapshot directory (Defect 2 fix, `docs/verification-installer.md`
    "Blocker 2" -- see `docs/config-contract.md` and
    `model_download.ModelDownload`'s own docstring), not a parent "models"
    directory, so no further join happens here.
    """
    return (Path(config.model_path) / ".ready").exists()


class SetupDownload:
    """Combined first-run acquisition: the CUDA runtime, then the model
    (Defect 1 fix, `docs/verification-installer.md` "Blocker 1" -- NSIS's
    32-bit compiler cannot compile the ~2.3 GiB `--extra cuda` payload, so
    it is fetched here instead, at first run, rather than baked in).

    Exposes exactly the attribute surface :class:`ModelDownloadManager`
    already reads off a bare :class:`ModelDownload` (``state``,
    ``downloaded_bytes``, ``total_bytes``, ``error``, ``start()``,
    ``cancel()``), so the existing ``/v1/model/download`` HTTP route and
    the app's ``ModelDownloadStep`` need no changes at all: progress simply
    counts through two phases (CUDA runtime, then model weights) under one
    combined byte total instead of one.
    """

    def __init__(self, *, cuda_runtime: CudaRuntimeDownload, model: ModelDownload) -> None:
        self._cuda_runtime = cuda_runtime
        self._model = model
        self._runtime_done_bytes = 0
        self._active: CudaRuntimeDownload | ModelDownload = cuda_runtime
        # E4: set when the CUDA-runtime phase ends in `error` (not
        # `cancelled` -- see `start()`) and the model phase is attempted
        # anyway; `.error` itself reports the *overall* outcome (`None`
        # once the model phase succeeds), so this is the one place a caller
        # can still learn the CUDA phase failed and a retry is available
        # for it specifically.
        self.cuda_warning: ServiceError | None = None

    @property
    def state(self) -> DownloadState:
        return self._active.state

    @property
    def downloaded_bytes(self) -> int:
        if self._active is self._model:
            return self._runtime_done_bytes + self._active.downloaded_bytes
        return self._active.downloaded_bytes

    @property
    def total_bytes(self) -> int:
        # The model's own total is unknown (its hub `list_files()` has not
        # run yet) until its own `start()` begins, so this under-reports
        # during the CUDA-runtime phase -- the progress bar's percentage
        # catches up, rather than staying wrong, the moment phase two begins.
        return self._cuda_runtime.total_bytes + self._model.total_bytes

    @property
    def error(self) -> ServiceError | None:
        return self._active.error

    @property
    def repo_id(self) -> str:
        return self._model.repo_id

    @property
    def revision(self) -> str:
        return self._model.revision

    def cancel(self) -> None:
        self._cuda_runtime.cancel()
        self._model.cancel()

    def start(
        self,
        on_progress: Callable[[dict[str, object]], None],
        *,
        progress_interval_sec: float = 1.0,
    ) -> None:
        def _relay(_event: dict[str, object]) -> None:
            on_progress(self._event())

        self._active = self._cuda_runtime
        self._cuda_runtime.start(on_progress=_relay, progress_interval_sec=progress_interval_sec)
        self._runtime_done_bytes = self._cuda_runtime.downloaded_bytes

        if self._cuda_runtime.state is DownloadState.CANCELLED:
            # A deliberate cancel (`cancel()` below signals both phases at
            # once) must stop the whole setup, not silently pivot into a
            # multi-gigabyte model transfer the operator never asked to
            # start -- `ModelDownload.start()` itself would clear this
            # phase's own cancel flag the instant it were called, discarding
            # the operator's cancel with no way to tell.
            return

        if self._cuda_runtime.state is DownloadState.ERROR:
            # E4: a failed CUDA-runtime fetch (a network drop, a digest
            # mismatch, a flaky connection) must not block the model the
            # operator actually needs -- surface it as a non-fatal warning
            # (`cuda_warning`) and continue to the model phase instead. A
            # retry of the CUDA runtime alone remains available afterwards
            # (`is_cuda_runtime_present`/a fresh `CudaRuntimeDownload`).
            self.cuda_warning = self._cuda_runtime.error

        self._active = self._model
        if self._model.already_present():
            # E13: the "Retry GPU setup" path re-POSTs `/v1/model/download`
            # after a CUDA-phase failure, which builds a *fresh*
            # `SetupDownload` (a fresh `ModelDownload` included) -- if the
            # model already landed in an earlier run, calling `start()`
            # unconditionally would re-fetch every one of its ~3 GB of files
            # from byte zero for no reason (`ModelDownload.start()` has no
            # already-present short-circuit of its own). Report the whole
            # setup as complete without touching the model phase at all.
            self._model.state = DownloadState.COMPLETE
            on_progress(self._event())
            return

        self._model.start(on_progress=_relay, progress_interval_sec=progress_interval_sec)

    def _event(self) -> dict[str, object]:
        percent = (self.downloaded_bytes / self.total_bytes) * 100.0 if self.total_bytes else 0.0
        current_file = getattr(self._active, "current_file", "")
        event: dict[str, object] = {
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "percent": percent,
            "file": current_file,
            "state": self.state.value,
        }
        if self.cuda_warning is not None:
            event["cuda_warning"] = self.cuda_warning.message
        return event


DownloadFactory = Callable[[], "ModelDownload | SetupDownload"]


class ModelDownloadManager:
    """Owns the single in-process download slot the HTTP routes poll (FR-12).

    The default factory (used only when `app.py`'s real server startup
    supplies none) is `build_setup_download`, not the model-only
    `build_download` -- so the one `/v1/model/download` resource the app's
    `ModelDownloadStep` already polls transparently covers the CUDA runtime
    too (Defect 1). Every test supplies its own factory and never reaches
    this default, so this never makes a real network call under test.
    """

    def __init__(self, config: Config, factory: DownloadFactory | None = None) -> None:
        self._config = config
        self._factory: DownloadFactory = factory or (lambda: build_setup_download(config))
        self._download: ModelDownload | SetupDownload | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._background_error: ServiceError | None = None

    def start(self) -> dict[str, Any]:
        """Start a transfer on a background thread, unless one is already
        running -- a second ``POST`` never starts a parallel transfer."""
        with self._lock:
            if self._download is not None and self._download.state is DownloadState.DOWNLOADING:
                return self.status()

            download = self._factory()
            self._download = download
            self._background_error = None

            def run() -> None:
                try:
                    download.start(on_progress=lambda _event: None, progress_interval_sec=1.0)
                except ServiceError as exc:
                    self._background_error = exc
                except Exception as exc:  # noqa: BLE001 - never die silently off-thread
                    self._background_error = ServiceError(ErrorKind.INTERNAL, str(exc))

            thread = threading.Thread(target=run, daemon=True, name="model-download")
            self._thread = thread
            thread.start()
            return self.status()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._download is not None:
                self._download.cancel()
            return self.status()

    def status(self) -> dict[str, Any]:
        download = self._download
        if download is None:
            return {
                "state": DownloadState.IDLE.value,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "percent": 0.0,
                "error_kind": None,
                "error_message": None,
            }

        total = download.total_bytes
        percent = (download.downloaded_bytes / total * 100.0) if total else 0.0
        error = download.error or self._background_error
        status: dict[str, Any] = {
            "state": download.state.value,
            "downloaded_bytes": download.downloaded_bytes,
            "total_bytes": total,
            "percent": percent,
            "error_kind": error.kind.value if error else None,
            "error_message": error.message if error else None,
        }
        # E4: additive -- only a `SetupDownload` whose CUDA-runtime phase
        # failed (and then continued into the model phase anyway) ever sets
        # this, so a bare `ModelDownload` (no CUDA phase at all) never gains
        # the key, keeping this backward compatible with a strict `set(body)`
        # equality check.
        cuda_warning = getattr(download, "cuda_warning", None)
        if cuda_warning is not None:
            status["cuda_warning"] = cuda_warning.message
        return status


def build_model_router(require_token: Callable[..., None]) -> APIRouter:
    """One router, mounted once by ``app.create_app`` -- its only edits
    there are wiring ``app.state.model_download_manager`` and including this
    router (FR-12, "no download logic in the route handlers")."""
    router = APIRouter()
    deps = [Depends(require_token)]

    def _manager(request: Request) -> ModelDownloadManager:
        manager: ModelDownloadManager = request.app.state.model_download_manager
        return manager

    @router.post("/v1/model/download", status_code=202, dependencies=deps)
    async def start_download(request: Request) -> dict[str, Any]:
        return _manager(request).start()

    @router.get("/v1/model/download", dependencies=deps)
    async def get_download_status(request: Request) -> dict[str, Any]:
        return _manager(request).status()

    @router.delete("/v1/model/download", dependencies=deps)
    async def cancel_download(request: Request) -> dict[str, Any]:
        return _manager(request).cancel()

    return router
