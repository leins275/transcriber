"""First-run acquisition of the speaker-diarization runtime and models.

Two payloads, two download slots, both fetched only on the operator's
request (Settings -> "Identify speakers"):

- **The runtime**: ``pyannote.audio`` and the torch stack under it, in its
  CUDA build -- the ``diarization`` extra the installer never bakes
  (gigabytes of GPU-specific payload). Every archive pinned in
  `diarization_runtime_packages.py` (generated from ``uv.lock`` by
  ``scripts/gen_diarization_runtime.py``) rides the existing
  :class:`~transcription.cuda_runtime.CudaRuntimeDownload` machinery
  (resume, digest verification, cancel, ``.ready`` marker) into
  ``<app_dir>/runtime/diarization/``, which :func:`activate_runtime` puts
  on ``sys.path`` before ``diarizer.py`` imports pyannote.
- **The models**: the three Hugging Face repos the stock
  ``pyannote/speaker-diarization-3.1`` pipeline loads. Two are gated: the
  operator accepts their terms on the hub once and supplies a read token
  (``hf_token`` in the shared config file, never argv).
  :class:`DiarizationModelDownload` snapshots all three at pinned revisions
  into ``<app_dir>/models/diarization/`` -- the hub-cache layout pyannote
  reads (``PYANNOTE_CACHE``) -- and pins each repo's ``refs/main`` to the
  snapshot, so later loads resolve offline and never drift from the pin.

Everything here degrades to a classified error rather than raising into
the download thread: a gated repo without an accepted license names the
repo to accept; a network failure is retryable.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any

from transcription.cuda_runtime import RUNTIME_DIRNAME, CudaPackage, CudaRuntimeDownload
from transcription.diarization_runtime_packages import (
    DIARIZATION_WHEELS,
    TORCH_CUDA_VARIANT,
    TOTAL_BYTES,
)
from transcription.errors import ErrorKind, ServiceError
from transcription.model_download import DownloadState, Transport

if TYPE_CHECKING:
    from transcription.config import Config

logger = logging.getLogger("transcription")

# The subdirectory of `<app_dir>/runtime/` the whole package tree lands in
# (a directory name, not an importable path -- imports resolve `pyannote`,
# `torch`, ... *inside* it, exactly like `runtime/llama-cuda`).
DIARIZATION_SUBDIR = "diarization"
_RUNTIME_MARKER_RELPATH = f"{DIARIZATION_SUBDIR}/.ready"

# Where the hub snapshots live: `<app_dir>/models/diarization/`, beside the
# whisper snapshot and the GGUFs, so the uninstaller's model relocation
# rules cover it too.
MODELS_SUBDIR: tuple[str, ...] = ("models", "diarization")
_MODELS_MARKER = ".ready"

RUNTIME_TOTAL_BYTES = TOTAL_BYTES
RUNTIME_CUDA_VARIANT = TORCH_CUDA_VARIANT

DIARIZATION_PACKAGES: tuple[CudaPackage, ...] = tuple(
    CudaPackage(
        name=wheel.name,
        version=wheel.version,
        filename=wheel.filename,
        url=wheel.url,
        size=wheel.size,
        sha256=wheel.sha256,
        extract_prefix=wheel.extract_prefix,
        dest_subdir=DIARIZATION_SUBDIR,
        archive_root=wheel.archive_root,
    )
    for wheel in DIARIZATION_WHEELS
)


# -- the runtime ------------------------------------------------------------


def diarization_runtime_dir(app_dir: str | Path) -> Path:
    """Where the fetched package tree lives."""
    return Path(app_dir) / RUNTIME_DIRNAME / DIARIZATION_SUBDIR


def _manifest_versions(packages: tuple[CudaPackage, ...] = DIARIZATION_PACKAGES) -> dict[str, str]:
    return {pkg.name: pkg.version for pkg in packages}


def is_diarization_runtime_present(
    app_dir: str | Path, packages: tuple[CudaPackage, ...] = DIARIZATION_PACKAGES
) -> bool:
    """Whether a prior fetch landed *this* manifest: the ``.ready`` marker
    exists and records exactly the pinned package versions.

    A build that re-pins the runtime (a torch bump, say) must not treat an
    older tree as usable -- it would import a mix the pins were never
    tested against -- so a stale marker reads as "absent", the Settings row
    offers the fetch again, and the fetch replaces the tree.
    """
    marker = Path(app_dir) / RUNTIME_DIRNAME / _RUNTIME_MARKER_RELPATH
    try:
        if not marker.is_file():
            return False
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("packages") == _manifest_versions(packages)


def pyannote_importable() -> bool:
    """Whether ``pyannote.audio`` resolves without the fetched runtime -- a
    dev environment synced with ``--extra diarization``."""
    try:
        return find_spec("pyannote.audio") is not None
    except (ImportError, ValueError):
        return False


def runtime_available(app_dir: str | Path) -> bool:
    """The runtime is usable: fetched into the app folder, or installed in
    the environment itself."""
    return is_diarization_runtime_present(app_dir) or pyannote_importable()


def activate_runtime(app_dir: str | Path) -> Path | None:
    """Put a fetched runtime on ``sys.path`` (once, ahead of the baked
    environment); ``None`` when none has been fetched.

    The manifest only ever holds packages the baked environment lacks, so
    the front position shadows nothing the service already imports.
    """
    runtime_dir = diarization_runtime_dir(app_dir)
    if not is_diarization_runtime_present(app_dir):
        return None
    entry = str(runtime_dir)
    if entry not in sys.path:
        sys.path.insert(0, entry)
        logger.info(
            "diarization runtime activated",
            extra={"event": "diarization_runtime_activated", "path": entry},
        )
    return runtime_dir


def build_diarization_runtime_download(
    config: Config, *, transport: Transport | None = None
) -> CudaRuntimeDownload:
    """The runtime fetch, on the same machinery as the STT CUDA runtime.

    A tree left by an older manifest (its marker names other versions) is
    removed first: the download only ever *adds* files, and a torch of one
    version over the DLLs of another is not a runtime anyone tested.
    """
    runtime_dir = diarization_runtime_dir(config.app_dir)
    if runtime_dir.exists() and not is_diarization_runtime_present(config.app_dir):
        logger.info(
            "replacing a stale diarization runtime",
            extra={"event": "diarization_runtime_replaced", "path": str(runtime_dir)},
        )
        shutil.rmtree(runtime_dir, ignore_errors=True)
    return CudaRuntimeDownload(
        app_dir=config.app_dir,
        allowed_roots=(Path(config.app_dir),),
        packages=DIARIZATION_PACKAGES,
        transport=transport,
        marker_relpath=_RUNTIME_MARKER_RELPATH,
    )


# -- the models ---------------------------------------------------------------


@dataclass(frozen=True)
class ModelRepo:
    """One Hugging Face repo the pipeline loads, at a pinned revision."""

    repo_id: str
    revision: str
    gated: bool


# Pinned to the hub's current `main` commits (read once, by hand -- the same
# "pin a concrete revision" rationale `model_download.MODEL_REVISION`
# documents). The pipeline repo names the two models it loads by hub id, so
# all three must be in the cache for an offline load.
DIARIZATION_MODEL_REPOS: tuple[ModelRepo, ...] = (
    ModelRepo(
        repo_id="pyannote/speaker-diarization-3.1",
        revision="84fd25912480287da0247647c3d2b4853cb3ee5d",
        gated=True,
    ),
    ModelRepo(
        repo_id="pyannote/segmentation-3.0",
        revision="e66f3d3b9eb0873085418a7b813d3b369bf160bb",
        gated=True,
    ),
    ModelRepo(
        repo_id="pyannote/wespeaker-voxceleb-resnet34-LM",
        revision="837717ddb9ff5507820346191109dc79c958d614",
        gated=False,
    ),
)

# What the operator has to accept on the hub before a token can fetch the
# gated repos -- surfaced verbatim in the error message so the fix is one
# click away.
GATED_REPO_URLS: tuple[str, ...] = tuple(
    f"https://huggingface.co/{repo.repo_id}" for repo in DIARIZATION_MODEL_REPOS if repo.gated
)


def diarization_cache_dir(app_dir: str | Path) -> Path:
    """The hub-cache directory the models are snapshotted into (and the
    value ``PYANNOTE_CACHE`` is pointed at before pyannote is imported)."""
    return Path(app_dir).joinpath(*MODELS_SUBDIR)


def _marker_payload(repos: tuple[ModelRepo, ...]) -> dict[str, Any]:
    return {"repos": {repo.repo_id: repo.revision for repo in repos}}


def is_diarization_model_present(
    app_dir: str | Path, repos: tuple[ModelRepo, ...] = DIARIZATION_MODEL_REPOS
) -> bool:
    """Whether every pinned snapshot has landed: the marker exists *and*
    records exactly these revisions (a re-pinned build re-fetches)."""
    marker = diarization_cache_dir(app_dir) / _MODELS_MARKER
    try:
        if not marker.is_file():
            return False
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("repos") == _marker_payload(repos)["repos"]


def _repo_folder(repo_id: str) -> str:
    """The hub cache's folder name for a model repo
    (`huggingface_hub.file_download.repo_folder_name`)."""
    return "models--" + repo_id.replace("/", "--")


def _pin_main_ref(cache_dir: Path, repo_id: str, revision: str) -> None:
    """Point the cache's ``refs/main`` at the pinned snapshot, so a load that
    asks for ``main`` (pyannote never passes a revision) resolves to it
    offline instead of consulting the hub."""
    refs_dir = cache_dir / _repo_folder(repo_id) / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "main").write_text(revision, encoding="utf-8")


# (repo_id, revision, cache_dir, token) -> None; the hub seam tests replace.
SnapshotFn = Callable[[str, str, Path, str | None], None]


# What the pipeline actually loads from each repo (plus the license and
# card, which travel with any redistribution): the benchmark tables, demo
# images and hub CI files in the repos are not worth the bytes -- these
# snapshots are also committed to the repository and shipped in the
# installer, so every megabyte counts twice.
SNAPSHOT_ALLOW_PATTERNS: tuple[str, ...] = (
    "config.yaml",
    "pytorch_model.bin",
    "LICENSE",
    "README.md",
)


def _hub_snapshot(repo_id: str, revision: str, cache_dir: Path, token: str | None) -> None:
    from huggingface_hub import snapshot_download  # noqa: PLC0415 - lazy, like every hub call

    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        cache_dir=str(cache_dir),
        token=token,
        library_name="pyannote",
        allow_patterns=list(SNAPSHOT_ALLOW_PATTERNS),
    )


def _classify_hub_error(exc: Exception, repo: ModelRepo) -> ServiceError:
    """A hub failure as a classified error: gated/unauthorized names the
    repo whose terms to accept; anything else is a retryable transfer
    failure."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    kind_name = type(exc).__name__
    unauthorized = kind_name in ("GatedRepoError", "RepositoryNotFoundError") or status in (
        401,
        403,
    )
    if unauthorized:
        return ServiceError(
            ErrorKind.MODEL_LOAD,
            f"{repo.repo_id} is gated on Hugging Face: accept its terms at "
            f"https://huggingface.co/{repo.repo_id} (signed in with the account the token "
            "belongs to) and check that the token has read access",
        )
    return ServiceError(
        ErrorKind.PROVIDER_UNAVAILABLE,
        f"could not fetch {repo.repo_id} from Hugging Face: {exc}",
        retryable=True,
    )


class DiarizationModelDownload:
    """One download session for the pinned pyannote snapshots.

    Exposes the surface `ModelDownloadManager` drives (`state`,
    `downloaded_bytes`, `total_bytes`, `current_file`, `error`, `start()`,
    `cancel()`), so the existing `/v1/<slot>/download` route shape needs
    no changes. Progress counts repos, not bytes (the three snapshots
    total ~30 MB; the hub client reports no byte progress worth plumbing).
    """

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        token: str | None,
        repos: tuple[ModelRepo, ...] = DIARIZATION_MODEL_REPOS,
        snapshot: SnapshotFn | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._token = token or None
        self._repos = repos
        self._snapshot: SnapshotFn = snapshot or _hub_snapshot
        self._cancel_event = threading.Event()

        self.state: DownloadState = DownloadState.IDLE
        self.downloaded_bytes = 0
        self.total_bytes = len(repos)
        self.current_file = ""
        self.error: ServiceError | None = None
        # What the CLI's download summary reports (`cli._run_download`
        # reads these off every download it drives).
        self.repo_id = ", ".join(repo.repo_id for repo in repos)
        self.revision = "pinned"

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def already_present(self) -> bool:
        marker = self._cache_dir / _MODELS_MARKER
        try:
            data = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(data, dict) and data.get("repos") == _marker_payload(self._repos)["repos"]

    def cancel(self) -> None:
        """Signal cancellation; honoured between repos (a snapshot in
        flight runs to its end)."""
        self._cancel_event.set()

    def _progress_event(self) -> dict[str, object]:
        percent = (self.downloaded_bytes / self.total_bytes) * 100.0 if self.total_bytes else 0.0
        return {
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "percent": percent,
            "file": self.current_file,
            "state": self.state.value,
        }

    def start(
        self,
        on_progress: Callable[[dict[str, object]], None],
        *,
        progress_interval_sec: float = 1.0,
    ) -> None:
        """Snapshot every repo to completion, cancellation, or error.
        Blocking and synchronous, like the other downloads."""
        if self.state is DownloadState.DOWNLOADING:
            return
        if self.already_present():
            self.state = DownloadState.COMPLETE
            self.downloaded_bytes = self.total_bytes
            on_progress(self._progress_event())
            return
        if self._token is None and any(repo.gated for repo in self._repos):
            self.state = DownloadState.ERROR
            self.error = ServiceError(
                ErrorKind.MODEL_LOAD,
                "a Hugging Face read token is required: accept the terms of "
                + " and ".join(GATED_REPO_URLS)
                + ", then paste a token from https://huggingface.co/settings/tokens",
            )
            on_progress(self._progress_event())
            return

        self._cancel_event.clear()
        self.state = DownloadState.DOWNLOADING
        self.error = None
        self.downloaded_bytes = 0
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        last_emit = time.monotonic()

        def emit(*, force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if force or (now - last_emit) >= progress_interval_sec:
                last_emit = now
                on_progress(self._progress_event())

        for repo in self._repos:
            if self._cancel_event.is_set():
                self.state = DownloadState.CANCELLED
                emit(force=True)
                return
            self.current_file = repo.repo_id
            try:
                self._snapshot(repo.repo_id, repo.revision, self._cache_dir, self._token)
                _pin_main_ref(self._cache_dir, repo.repo_id, repo.revision)
            except Exception as exc:  # noqa: BLE001 - classified, never raised off-thread
                self.state = DownloadState.ERROR
                self.error = _classify_hub_error(exc, repo)
                emit(force=True)
                return
            self.downloaded_bytes += 1
            emit()

        self.current_file = ""
        marker = self._cache_dir / _MODELS_MARKER
        marker.write_text(json.dumps(_marker_payload(self._repos)), encoding="utf-8")
        self.state = DownloadState.COMPLETE
        emit(force=True)
        logger.info(
            "diarization models fetched",
            extra={"event": "diarization_models_fetched", "repos": len(self._repos)},
        )


def build_diarization_model_download(
    config: Config, *, snapshot: SnapshotFn | None = None
) -> DiarizationModelDownload:
    return DiarizationModelDownload(
        cache_dir=diarization_cache_dir(config.app_dir),
        token=config.hf_token,
        snapshot=snapshot,
    )


def install_hub_compat() -> bool:
    """Let pyannote 3.x talk to huggingface_hub 1.x.

    pyannote 3.x passes ``use_auth_token=`` to ``hf_hub_download`` (its
    three call sites all do ``from huggingface_hub import hf_hub_download``
    at import time); huggingface_hub 1.0 renamed the parameter to
    ``token=`` and rejects the old one. The service ships hub 1.x for its
    own model downloads, so the name pyannote binds is wrapped here to
    translate the argument. Must run *before* pyannote is imported (it
    binds the attribute then); idempotent; a no-op on a hub that still
    accepts the old spelling. Returns whether a wrapper is in place.
    """
    import functools  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    import huggingface_hub  # noqa: PLC0415 - lazy, like every hub call

    original = huggingface_hub.hf_hub_download
    if getattr(original, "_transcriber_hub_compat", False):
        return True
    if "use_auth_token" in inspect.signature(original).parameters:
        return False

    @functools.wraps(original)
    def compat(*args: Any, **kwargs: Any) -> Any:
        if "use_auth_token" in kwargs:
            token = kwargs.pop("use_auth_token")
            kwargs.setdefault("token", token)
        return original(*args, **kwargs)

    compat._transcriber_hub_compat = True  # type: ignore[attr-defined]
    huggingface_hub.hf_hub_download = compat
    return True


@contextmanager
def hub_offline(enabled: bool) -> Iterator[None]:
    """Force ``huggingface_hub`` into offline mode for the duration.

    Once the pinned snapshots are on disk, the pipeline load must resolve
    them from the cache -- never re-consult the hub, which would need the
    token again and could silently move past the pin. `huggingface_hub`
    reads the flag off its ``constants`` module at call time, so flipping
    it around one load is enough (and nothing else in this process makes
    a hub call from the worker thread while a job runs).
    """
    if not enabled:
        yield
        return
    from huggingface_hub import constants  # noqa: PLC0415 - lazy, like every hub call

    previous = constants.HF_HUB_OFFLINE
    constants.HF_HUB_OFFLINE = True
    try:
        yield
    finally:
        constants.HF_HUB_OFFLINE = previous


# -- status ---------------------------------------------------------------------


def gpu_present() -> bool:
    """The same "is there an NVIDIA GPU at all" probe the STT runtime
    download decides on (`api.model_routes._nvidia_gpu_present`)."""
    if sys.platform != "win32":
        return False
    from transcription.api.model_routes import (  # noqa: PLC0415 - avoids an import cycle
        _nvidia_gpu_present,
    )

    return _nvidia_gpu_present()


def diarization_status(config: Config) -> dict[str, Any]:
    """`GET /v1/diarization/status`: everything the app needs to render the
    Speakers settings row -- which prerequisites are met and whether the
    feature is switched on."""
    return {
        "runtime_present": runtime_available(config.app_dir),
        "model_present": is_diarization_model_present(config.app_dir),
        "token_present": bool(config.hf_token),
        "enabled": bool(config.diarize),
        "gpu_present": gpu_present(),
        "runtime_total_bytes": RUNTIME_TOTAL_BYTES,
    }
