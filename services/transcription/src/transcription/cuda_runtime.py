"""CUDA runtime acquisition at first run (Defect 1 fix: NSIS's vendored,
32-bit `makensis.exe` cannot compile a ~2.3 GiB payload -- see
`docs/verification-installer.md`'s "Blocker 1"). The bake stage
(`scripts/build_pyenv.py`) no longer ships `--extra cuda`'s wheels inside
the installer; this module fetches them instead, on first run, into the
application folder, mirroring `model_download.py`'s resume/verify/cancel
pattern so the operator sees the same kind of progress UI for both.

Fetches the pinned `nvidia-cublas-cu12`, `nvidia-cuda-nvrtc-cu12` (cuBLAS's
own runtime dependency -- see `services/transcription/uv.lock`) and
`nvidia-cudnn-cu12` wheels straight from PyPI, at the exact versions/
digests `uv.lock` already pins -- a first-run download is byte-identical to
what `--extra cuda` used to bake into the installer. A wheel is a zip file;
only its `nvidia/` tree (the DLLs CTranslate2 loads) is extracted, into an
app-folder `runtime/` directory that `runtime_dlls.py`'s shim also scans in
addition to the venv's own `site-packages/nvidia` (Defect 3's fix), so the
loader-side lookup needs no format-specific branch, only one extra
directory.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from transcription.errors import ErrorKind, ServiceError
from transcription.model_download import DownloadState, HttpTransport, Transport
from transcription.paths import ensure_output_dir
from transcription.runtime_dlls import register_cuda_dll_dirs

_READY_MARKER = ".ready"
_WHEELS_DIRNAME = "_wheels"
_DEFAULT_PROGRESS_INTERVAL_SEC = 1.0
_CHUNK_SIZE = 1024 * 1024

RUNTIME_DIRNAME = "runtime"


@dataclass(frozen=True, kw_only=True)
class CudaPackage:
    """One pinned PyPI wheel this download fetches."""

    name: str
    version: str
    filename: str
    url: str
    size: int
    sha256: str


# Pinned to the exact versions/digests services/transcription/uv.lock
# records for `--extra cuda` (read once, by hand, off the committed lock
# file -- the same "pin a concrete revision/digest set" rationale
# `model_download.MODEL_REVISION` documents). `nvidia-cuda-nvrtc-cu12` is
# not one of the two packages NFR-1 names, but it is `nvidia-cublas-cu12`'s
# own locked dependency and cuBLAS does not load without it.
CUDA_PACKAGES: tuple[CudaPackage, ...] = (
    CudaPackage(
        name="nvidia-cublas-cu12",
        version="12.9.2.10",
        filename="nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
        url=(
            "https://files.pythonhosted.org/packages/20/e2/"
            "fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
            "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl"
        ),
        size=553162896,
        sha256="623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
    ),
    CudaPackage(
        name="nvidia-cuda-nvrtc-cu12",
        version="12.9.86",
        filename="nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
        url=(
            "https://files.pythonhosted.org/packages/52/de/"
            "823919be3b9d0ccbf1f784035423c5f18f4267fb0123558d58b813c6ec86/"
            "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl"
        ),
        size=76408187,
        sha256="72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349",
    ),
    CudaPackage(
        name="nvidia-cudnn-cu12",
        version="9.24.0.43",
        filename="nvidia_cudnn_cu12-9.24.0.43-py3-none-win_amd64.whl",
        url=(
            "https://files.pythonhosted.org/packages/29/28/"
            "2c9a2a97a8b3fedcf74a14f38fd5edfae12274380a829fdc6b16ce29be4c/"
            "nvidia_cudnn_cu12-9.24.0.43-py3-none-win_amd64.whl"
        ),
        size=737103728,
        sha256="cbd41a0ab084422c936dc9fb2fc89be5ea9a85bc421c6f23d0243bdfc945fbef",
    ),
)


def is_cuda_runtime_present(app_dir: str | Path) -> bool:
    """Whether a prior run's `.ready` marker already exists, so a restarted
    first-run wizard never re-fetches ~1.4 GB it already has (mirrors
    `model_download.py`'s FR-16 idempotency)."""
    return (Path(app_dir) / RUNTIME_DIRNAME / _READY_MARKER).exists()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_nvidia_trees(wheel_paths: Sequence[Path], dest_dir: Path) -> None:
    """Extract only each wheel's `nvidia/` tree into `dest_dir`, merging
    across wheels -- the same `nvidia/<component>/bin/*.dll` layout
    `build_pyenv.py` already produces under a baked `--extra cuda`
    `site-packages/`, so `runtime_dlls.py`'s lookup needs no format-specific
    branch, only one extra directory to scan."""
    for wheel_path in wheel_paths:
        with zipfile.ZipFile(wheel_path) as zf:
            for member in zf.namelist():
                normalized = member.replace("\\", "/")
                if not normalized.startswith("nvidia/") or normalized.endswith("/"):
                    continue
                zf.extract(member, dest_dir)


class CudaRuntimeDownload:
    """One download session for the pinned CUDA runtime wheels (Defect 1).

    Deliberately exposes the same surface `model_download.ModelDownload`
    does (`state`, `downloaded_bytes`, `total_bytes`, `current_file`,
    `error`, `start()`, `cancel()`), so `SetupDownload`
    (`api/model_routes.py`) can drive either one identically and
    `ModelDownloadManager`'s existing HTTP polling needs no changes at all.
    """

    def __init__(
        self,
        *,
        app_dir: str | Path,
        allowed_roots: tuple[Path, ...],
        packages: Sequence[CudaPackage] = CUDA_PACKAGES,
        transport: Transport | None = None,
    ) -> None:
        self._runtime_dir = ensure_output_dir(Path(app_dir) / RUNTIME_DIRNAME, allowed_roots)
        self._packages = tuple(packages)
        self._transport = transport or HttpTransport()
        self._cancel_event = threading.Event()

        self.state: DownloadState = DownloadState.IDLE
        self.downloaded_bytes = 0
        self.total_bytes = sum(pkg.size for pkg in self._packages)
        self.current_file = ""
        self.error: ServiceError | None = None

    def already_present(self) -> bool:
        return (self._runtime_dir / _READY_MARKER).exists()

    def cancel(self) -> None:
        """Signal cancellation; checked between chunks (stops within one chunk)."""
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
        progress_interval_sec: float = _DEFAULT_PROGRESS_INTERVAL_SEC,
    ) -> None:
        """Run the transfer+extraction to completion, cancellation, or error.

        Blocking and synchronous, like `ModelDownload.start()` -- callers
        that need this off the request thread run it in a background thread.
        """
        if self.state is DownloadState.DOWNLOADING:
            return

        if self.already_present():
            self.state = DownloadState.COMPLETE
            # Idempotent (R3/`runtime_dlls.register_cuda_dll_dirs` docstring)
            # -- a restarted first-run wizard hitting this branch must still
            # guarantee the directories are on the search path, in case this
            # is a fresh process whose `cli.main()` start-of-process call ran
            # before this app-folder `runtime\` tree existed (E1).
            register_cuda_dll_dirs()
            on_progress(self._progress_event())
            return

        self._cancel_event.clear()
        self.state = DownloadState.DOWNLOADING
        self.error = None
        self.downloaded_bytes = 0

        wheels_dir = self._runtime_dir / _WHEELS_DIRNAME
        wheels_dir.mkdir(parents=True, exist_ok=True)

        last_emit = time.monotonic()

        def emit(*, force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if force or (now - last_emit) >= progress_interval_sec:
                last_emit = now
                on_progress(self._progress_event())

        downloaded_wheel_paths: list[Path] = []

        for pkg in self._packages:
            if self._cancel_event.is_set():
                self.state = DownloadState.CANCELLED
                emit(force=True)
                return

            self.current_file = pkg.filename
            dest = wheels_dir / pkg.filename
            incomplete = dest.with_name(dest.name + ".incomplete")

            if dest.is_file() and dest.stat().st_size == pkg.size:
                # A prior run already landed this exact wheel (e.g. it died
                # partway through a later package, or during extraction) --
                # count it and move on rather than re-fetching (NFR-3-style
                # resume, one whole file at a time). E7: size alone is not
                # proof of integrity (the app folder is user-writable by
                # design, and a crash mid-write can leave a same-sized but
                # corrupt file) -- verify the digest here too, exactly like
                # a freshly-downloaded wheel below, before trusting it onto
                # the extraction path that ends up on `PATH`.
                if _sha256_file(dest) == pkg.sha256:
                    self.downloaded_bytes += pkg.size
                    downloaded_wheel_paths.append(dest)
                    emit()
                    continue
                dest.unlink()

            resume_from = incomplete.stat().st_size if incomplete.exists() else 0
            if resume_from > pkg.size:
                incomplete.unlink()
                resume_from = 0

            self.downloaded_bytes += resume_from

            def on_chunk(n: int) -> None:
                self.downloaded_bytes += n
                emit()

            self._transport.fetch(
                url=pkg.url,
                dest=incomplete,
                resume_from=resume_from,
                on_chunk=on_chunk,
                cancel=self._cancel_event,
            )

            if self._cancel_event.is_set():
                self.state = DownloadState.CANCELLED
                emit(force=True)
                return

            actual_size = incomplete.stat().st_size if incomplete.exists() else 0
            if actual_size != pkg.size:
                # A connection drop or killed process, not a deliberate
                # cancel -- leave the partial blob so a future start()
                # resumes from here (NFR-3).
                self.state = DownloadState.ERROR
                self.error = ServiceError(
                    ErrorKind.PROVIDER_UNAVAILABLE,
                    f"transfer interrupted for {pkg.filename}",
                    retryable=True,
                )
                emit(force=True)
                return

            digest = _sha256_file(incomplete)
            if digest != pkg.sha256:
                incomplete.unlink()
                self.state = DownloadState.ERROR
                self.error = ServiceError(
                    ErrorKind.INTERNAL, f"digest mismatch for {pkg.filename}", retryable=True
                )
                emit(force=True)
                return

            incomplete.replace(dest)
            downloaded_wheel_paths.append(dest)

        self.current_file = ""
        self.state = DownloadState.VERIFYING
        emit(force=True)

        try:
            _extract_nvidia_trees(downloaded_wheel_paths, self._runtime_dir)
        except Exception as exc:
            self.state = DownloadState.ERROR
            self.error = ServiceError(ErrorKind.INTERNAL, f"failed to extract CUDA runtime: {exc}")
            emit(force=True)
            raise self.error from exc

        self._write_ready_marker()
        shutil.rmtree(wheels_dir, ignore_errors=True)

        # E1 fix: `cli.main()` registered DLL directories once, at process
        # start, before this `runtime/nvidia` tree existed -- nothing made
        # the just-extracted DLLs visible to the *already-running* process
        # that downloaded them. Re-running the (idempotent, never-raising)
        # registration here, the moment extraction succeeds, means the very
        # next provider construction sees the updated search path/PATH with
        # no sidecar restart required.
        register_cuda_dll_dirs()

        self.state = DownloadState.COMPLETE
        emit(force=True)

    def _write_ready_marker(self) -> None:
        marker = self._runtime_dir / _READY_MARKER
        payload = {
            "packages": {pkg.name: pkg.version for pkg in self._packages},
            "verified_at": time.time(),
        }
        marker.write_text(json.dumps(payload), encoding="utf-8")
