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

from transcription import llm_catalog
from transcription.config import Config
from transcription.cuda_runtime import CudaRuntimeDownload
from transcription.errors import ErrorKind, ServiceError
from transcription.llm_catalog import CatalogEntry
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


class LlmGgufDownload(ModelDownload):
    """A :class:`ModelDownload` whose "already present" means *the target
    GGUF file is on disk*, not "the shared ``.ready`` marker exists".

    The catalog keeps several GGUFs flat in one ``models/llm`` directory, so
    the directory-level marker the base class checks stops being meaningful
    the moment a second model exists: a present 35B would short-circuit the
    9B's :class:`SetupDownload` model phase (never downloading it), and a
    re-``POST`` for a file already on disk would re-fetch it from byte zero.
    File presence is also exactly the check the llama.cpp provider itself
    makes at load time (hand-copied models count).
    """

    def __init__(self, *, target_file: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._target_file = target_file

    def already_present(self) -> bool:
        return (self._models_dir / self._target_file).is_file()


def build_llm_model_download(
    config: Config,
    entry: CatalogEntry | None = None,
    *,
    hub_client: HubClient | None = None,
    transport: Transport | None = None,
) -> LlmGgufDownload:
    """The GGUF download for the built-in llama.cpp runtime.

    Reuses :class:`ModelDownload` wholesale (resume, digest verification,
    cancel) with a file filter selecting exactly the one wanted quantization
    -- a GGUF repo carries one file per quant, and downloading the whole
    snapshot would move hundreds of GB. ``entry`` picks a specific catalog
    model; ``None`` uses the config-resolved pin (the CLI path, and the
    escape hatch for a hand-picked GGUF).
    """
    repo = entry.repo if entry else config.llm_model_repo
    revision = entry.revision if entry else config.llm_model_revision
    file = entry.file if entry else config.llm_model_file
    wanted = file.casefold()
    return LlmGgufDownload(
        target_file=file,
        models_dir=config.llm_model_path,
        allowed_roots=default_allowed_roots(config),
        repo_id=repo,
        revision=revision,
        hub_client=hub_client,
        transport=transport,
        file_filter=lambda remote: remote.path.casefold() == wanted,
    )


def is_llm_model_present(config: Config, entry: CatalogEntry | None = None) -> bool:
    """Whether the GGUF file is on disk (the load-time check the llama.cpp
    provider itself makes -- presence of the file, not any ``.ready``
    marker, so a hand-copied model also counts)."""
    file = entry.file if entry else config.llm_model_file
    return (Path(config.llm_model_path) / file).is_file()


def llm_gpu_build_present(config: Config) -> bool | None:
    """Whether the CUDA build of the LLM runtime has been fetched.

    ``None`` on a host with no NVIDIA GPU (same convention as
    ``cuda_runtime_present`` on ``/health``: never prompt about a payload
    this machine could never use); otherwise the fetch's ``.ready`` marker.
    """
    if sys.platform != "win32" or not _nvidia_gpu_present():
        return None
    from transcription.llm.runtime_fetch import (  # noqa: PLC0415 - keeps llm/ lazy
        is_llama_cuda_present,
    )

    return is_llama_cuda_present(config.app_dir)


def build_llm_setup_download(
    config: Config, entry: CatalogEntry | None = None
) -> ModelDownload | SetupDownload:
    """The real, production LLM download: the GGUF alone on a GPU-less
    machine, or the CUDA llama.cpp build first and the GGUF second on a
    machine with an NVIDIA GPU -- the exact `build_setup_download` shape,
    reusing :class:`SetupDownload` unchanged (each phase short-circuits via
    its own ``already_present()``, so re-POSTing after the GGUF landed
    fetches only the missing GPU build, and vice versa)."""
    gguf = build_llm_model_download(config, entry)
    if sys.platform != "win32" or not _nvidia_gpu_present():
        return gguf
    from transcription.llm.runtime_fetch import (  # noqa: PLC0415 - keeps llm/ lazy
        build_llama_cuda_download,
    )

    return SetupDownload(cuda_runtime=build_llama_cuda_download(config), model=gguf)


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


_ACTIVE_TRANSFER_STATES = (DownloadState.DOWNLOADING, DownloadState.VERIFYING)


class LlmModelsManager:
    """The curated LLM catalog's per-model download slots and file lifecycle.

    One :class:`ModelDownloadManager` per catalog id (plus one for the
    escape-hatch model when the active config points outside the catalog),
    with the same one-transfer-at-a-time rule the single-slot managers have
    -- extended across slots: starting model B while model A is transferring
    is refused instead of racing two multi-GB downloads.

    Selection (writing ``llm_model`` into config.json and restarting the
    sidecar) is the desktop app's job, not this manager's -- the service's
    ``Config`` is frozen for the life of the process.
    """

    def __init__(
        self,
        config: Config,
        factory_for: Callable[[CatalogEntry | None], ModelDownload | SetupDownload] | None = None,
        has_active_llm_job: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config
        make = factory_for or (lambda entry: build_llm_setup_download(config, entry))
        self._has_active_llm_job = has_active_llm_job or (lambda: False)
        self._entries: dict[str, CatalogEntry | None] = {
            entry.id: entry for entry in llm_catalog.CATALOG
        }
        if config.llm_model not in self._entries:
            # Escape hatch: the active config points at a hand-picked GGUF.
            # Give it a slot too so the UI can show (and re-download) it.
            self._entries[config.llm_model] = None
        self._managers: dict[str, ModelDownloadManager] = {
            model_id: ModelDownloadManager(config, factory=self._bind(make, entry))
            for model_id, entry in self._entries.items()
        }

    @staticmethod
    def _bind(
        make: Callable[[CatalogEntry | None], ModelDownload | SetupDownload],
        entry: CatalogEntry | None,
    ) -> DownloadFactory:
        return lambda: make(entry)

    def manager_for_active(self) -> ModelDownloadManager:
        """The active model's slot -- what the legacy ``/v1/llm-model/download``
        routes (and the first-run assistant flow) poll."""
        return self._managers[self._config.llm_model]

    def _slot(self, model_id: str) -> ModelDownloadManager:
        manager = self._managers.get(model_id)
        if manager is None:
            known = ", ".join(sorted(self._managers))
            raise ServiceError(
                ErrorKind.INVALID_REQUEST,
                f"unknown llm model {model_id!r}: known models are {known}",
            )
        return manager

    def _file_for(self, model_id: str) -> str:
        entry = self._entries[model_id]
        return entry.file if entry else self._config.llm_model_file

    def _is_transferring(self, model_id: str) -> bool:
        state = self._managers[model_id].status()["state"]
        return state in {s.value for s in _ACTIVE_TRANSFER_STATES}

    def list(self) -> dict[str, Any]:
        models = []
        for model_id, entry in self._entries.items():
            models.append(
                {
                    "id": model_id,
                    "label": entry.label if entry else self._config.llm_model,
                    "file": self._file_for(model_id),
                    "size_bytes": entry.size_bytes if entry else None,
                    "catalog": entry is not None,
                    "present": is_llm_model_present(self._config, entry),
                    "active": model_id == self._config.llm_model,
                    "download": self._managers[model_id].status(),
                }
            )
        return {"active": self._config.llm_model, "models": models}

    def start(self, model_id: str) -> dict[str, Any]:
        slot = self._slot(model_id)
        for other_id in self._managers:
            if other_id != model_id and self._is_transferring(other_id):
                raise ServiceError(
                    ErrorKind.INVALID_REQUEST,
                    f"another model ({other_id!r}) is downloading; "
                    f"cancel it or wait for it to finish first",
                )
        return slot.start()

    def cancel(self, model_id: str) -> dict[str, Any]:
        return self._slot(model_id).cancel()

    def delete(self, model_id: str) -> dict[str, Any]:
        self._slot(model_id)
        if model_id == self._config.llm_model:
            raise ServiceError(
                ErrorKind.INVALID_REQUEST,
                "cannot delete the active model; select another model first",
            )
        if self._is_transferring(model_id):
            raise ServiceError(
                ErrorKind.INVALID_REQUEST,
                "cannot delete a model while it is downloading; cancel the download first",
            )
        if self._has_active_llm_job():
            raise ServiceError(
                ErrorKind.INVALID_REQUEST,
                "cannot delete a model while assistant jobs are running",
            )
        file = Path(self._config.llm_model_path) / self._file_for(model_id)
        for path in (file, file.with_name(file.name + ".incomplete")):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise ServiceError(
                    ErrorKind.INTERNAL, f"failed to delete {path.name}: {exc}"
                ) from exc
        return self.list()


def build_llm_models_router(require_token: Callable[..., None]) -> APIRouter:
    """Routes for the curated LLM catalog (``/v1/llm-models``), mounted by
    ``app.create_app`` next to the per-slot download routers."""
    router = APIRouter()
    deps = [Depends(require_token)]

    def _manager(request: Request) -> LlmModelsManager:
        manager: LlmModelsManager = request.app.state.llm_models_manager
        return manager

    @router.get("/v1/llm-models", dependencies=deps)
    async def list_models(request: Request) -> dict[str, Any]:
        return _manager(request).list()

    @router.post("/v1/llm-models/{model_id}/download", status_code=202, dependencies=deps)
    async def start_model_download(request: Request, model_id: str) -> dict[str, Any]:
        return _manager(request).start(model_id)

    @router.delete("/v1/llm-models/{model_id}/download", dependencies=deps)
    async def cancel_model_download(request: Request, model_id: str) -> dict[str, Any]:
        return _manager(request).cancel(model_id)

    @router.delete("/v1/llm-models/{model_id}", dependencies=deps)
    async def delete_model(request: Request, model_id: str) -> dict[str, Any]:
        return _manager(request).delete(model_id)

    return router


def build_model_router(
    require_token: Callable[..., None],
    *,
    prefix: str = "/v1/model/download",
    state_attr: str = "model_download_manager",
) -> APIRouter:
    """One router per download slot, mounted by ``app.create_app`` -- its
    only edits there are wiring the manager onto ``app.state`` and including
    this router (FR-12, "no download logic in the route handlers").

    Mounted twice: the whisper slot at the default ``/v1/model/download``
    and the GGUF slot at ``/v1/llm-model/download`` (``state_attr``
    ``llm_model_download_manager``), each polling its own
    :class:`ModelDownloadManager` with the identical wire shape.
    """
    router = APIRouter()
    deps = [Depends(require_token)]

    def _manager(request: Request) -> ModelDownloadManager:
        manager: ModelDownloadManager = getattr(request.app.state, state_attr)
        return manager

    @router.post(prefix, status_code=202, dependencies=deps)
    async def start_download(request: Request) -> dict[str, Any]:
        return _manager(request).start()

    @router.get(prefix, dependencies=deps)
    async def get_download_status(request: Request) -> dict[str, Any]:
        return _manager(request).status()

    @router.delete(prefix, dependencies=deps)
    async def cancel_download(request: Request) -> dict[str, Any]:
        return _manager(request).cancel()

    return router
