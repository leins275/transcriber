"""Model download core: resume, verify, cancel, progress (FR-12, NFR-3).

Downloads the pinned ``faster-whisper-large-v3`` snapshot into the
configured ``<app folder>/models/`` directory. File listing and digests
come from a thin :class:`HubClient` seam over ``huggingface_hub`` (a
metadata-only call -- no weights are transferred by it); the actual bytes
move through a thin :class:`Transport` seam so the whole flow is testable
offline with fakes (per F2's established GPU-free pattern) while the
default implementations do the real HTTP work.

Progress is reported through a callback with the shape
``{downloaded_bytes, total_bytes, percent, file, state}``, throttled to at
most once per ``progress_interval_sec`` except for the start-of-transfer
and terminal events, which are always forced through (FR-12: "at least
once a second"). Interruption resume relies on a ``<file>.incomplete``
sibling: a restarted transfer picks up from that file's existing size
rather than re-fetching from byte zero (NFR-3). Verification re-walks the
completed snapshot and checks size plus digest before writing the
``.ready`` marker the service reads before loading (FR-12 acceptance) --
never on a truncated or mismatched file.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from transcription.errors import ErrorKind, ServiceError
from transcription.paths import ensure_output_dir

MODEL_REPO_ID = "Systran/faster-whisper-large-v3"
# Pinned so verification always has a concrete revision + digest set to
# compare against (queried once by hand against the real hub; FR-12 "Pin
# the revision so verification has something to compare against").
MODEL_REVISION = "edaa852ec7e145841d8ffdb056a99866b5f0a478"

_READY_MARKER = ".ready"
_DEFAULT_PROGRESS_INTERVAL_SEC = 1.0
_CHUNK_SIZE = 1024 * 1024


class DownloadState(StrEnum):
    IDLE = "idle"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True, kw_only=True)
class RemoteFile:
    """One file in the remote snapshot: its relative path, size and digest."""

    path: str
    size: int
    sha256: str | None = None


class HubClient(Protocol):
    """The seam over ``huggingface_hub`` -- metadata only, no byte transfer."""

    def list_files(self, repo_id: str, revision: str) -> list[RemoteFile]: ...

    def file_url(self, repo_id: str, revision: str, filename: str) -> str: ...


class Transport(Protocol):
    """The seam that moves bytes. Fakes replace this entirely in tests."""

    def fetch(
        self,
        *,
        url: str,
        dest: Path,
        resume_from: int,
        on_chunk: Callable[[int], None],
        cancel: threading.Event,
    ) -> None: ...


class HuggingFaceHubClient:
    """Default :class:`HubClient` backed by the real ``huggingface_hub``."""

    def list_files(self, repo_id: str, revision: str) -> list[RemoteFile]:
        import huggingface_hub

        info = huggingface_hub.HfApi().model_info(repo_id, revision=revision, files_metadata=True)
        files: list[RemoteFile] = []
        for sibling in info.siblings or []:
            sha256 = None
            if sibling.lfs:
                sha256 = (
                    sibling.lfs.get("sha256")
                    if isinstance(sibling.lfs, dict)
                    else getattr(sibling.lfs, "sha256", None)
                )
            files.append(RemoteFile(path=sibling.rfilename, size=sibling.size or 0, sha256=sha256))
        return files

    def file_url(self, repo_id: str, revision: str, filename: str) -> str:
        import huggingface_hub

        return huggingface_hub.hf_hub_url(repo_id, filename, revision=revision)


class HttpTransport:
    """Default :class:`Transport`: a resumable ranged GET over the hub's session."""

    def fetch(
        self,
        *,
        url: str,
        dest: Path,
        resume_from: int,
        on_chunk: Callable[[int], None],
        cancel: threading.Event,
    ) -> None:
        from huggingface_hub.utils import get_session  # type: ignore[attr-defined]

        headers = {}
        mode = "wb"
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
            mode = "ab"

        session = get_session()
        with session.stream("GET", url, headers=headers, timeout=30) as response:
            response.raise_for_status()
            with open(dest, mode) as f:
                for chunk in response.iter_bytes(chunk_size=_CHUNK_SIZE):
                    if cancel.is_set():
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    on_chunk(len(chunk))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelDownload:
    """One download session for the pinned model snapshot (FR-12)."""

    def __init__(
        self,
        *,
        models_dir: str | Path,
        allowed_roots: tuple[Path, ...],
        repo_id: str = MODEL_REPO_ID,
        revision: str = MODEL_REVISION,
        hub_client: HubClient | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._models_dir = ensure_output_dir(models_dir, allowed_roots)
        self.repo_id = repo_id
        self.revision = revision
        self._hub_client = hub_client or HuggingFaceHubClient()
        self._transport = transport or HttpTransport()
        self._cancel_event = threading.Event()

        self.state: DownloadState = DownloadState.IDLE
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.current_file = ""
        self.error: ServiceError | None = None
        self._files: list[RemoteFile] = []

    def cancel(self) -> None:
        """Signal cancellation; checked between chunks (stops within one chunk)."""
        self._cancel_event.set()

    def already_present(self) -> bool:
        """Whether the pinned snapshot's `.ready` marker already exists
        (mirrors `CudaRuntimeDownload.already_present()`, E13) -- lets a
        caller such as `SetupDownload.start()` skip a model phase that has
        nothing left to do, instead of calling `start()` unconditionally:
        this class has no already-present short-circuit of its own and
        would otherwise re-fetch every file from byte zero into a fresh
        `.incomplete` sibling even though the real file is already on
        disk."""
        return (self._models_dir / _READY_MARKER).exists()

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
        """Run the transfer to completion, to cancellation, or to error.

        Blocking and synchronous -- callers that need this off the request
        thread (the HTTP/CLI layer) run it in a background thread.
        """
        if self.state is DownloadState.DOWNLOADING:
            return

        self._cancel_event.clear()
        self.state = DownloadState.DOWNLOADING
        self.error = None

        try:
            self._files = self._hub_client.list_files(self.repo_id, self.revision)
        except Exception as exc:
            self.state = DownloadState.ERROR
            self.error = ServiceError(
                ErrorKind.PROVIDER_UNAVAILABLE,
                f"failed to list model files for {self.repo_id!r}: {exc}",
                retryable=True,
            )
            on_progress(self._progress_event())
            raise self.error from exc

        self.total_bytes = sum(f.size for f in self._files)
        self.downloaded_bytes = 0

        # Written flat, directly into `self._models_dir` -- no per-revision
        # or hub-cache-shaped subdirectory (T14's real-machine verification,
        # `docs/verification-installer.md` "Blocker 2", found the previous
        # `<models_dir>/<revision>/*` layout mutually incompatible with the
        # local provider's loader, which passes `models_dir` straight
        # through as a literal directory, bypassing the hub-cache-style
        # download-and-cache convention entirely). The caller decides what
        # `models_dir` names (the app passes the exact, already-model-
        # specific directory via `TRANSCRIBER_MODEL_PATH` -- see
        # `docs/config-contract.md`), so this class never renames it.
        snapshot_dir = self._models_dir
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        last_emit = time.monotonic()

        def emit(*, force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if force or (now - last_emit) >= progress_interval_sec:
                last_emit = now
                on_progress(self._progress_event())

        for remote_file in self._files:
            if self._cancel_event.is_set():
                self.state = DownloadState.CANCELLED
                emit(force=True)
                return

            self.current_file = remote_file.path
            dest = snapshot_dir / remote_file.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            incomplete = dest.with_name(dest.name + ".incomplete")

            resume_from = incomplete.stat().st_size if incomplete.exists() else 0
            if resume_from > remote_file.size:
                # A stale/corrupt partial blob larger than the real file --
                # never trust it as a resume point.
                incomplete.unlink()
                resume_from = 0

            self.downloaded_bytes += resume_from
            url = self._hub_client.file_url(self.repo_id, self.revision, remote_file.path)

            def on_chunk(n: int) -> None:
                self.downloaded_bytes += n
                emit()

            self._transport.fetch(
                url=url,
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
            if actual_size != remote_file.size:
                # The transport stopped early without setting the cancel
                # event -- a connection drop or killed process, not a
                # deliberate cancel. Leave the partial blob in place so a
                # future `start()` resumes from here instead of byte zero
                # (NFR-3), rather than treating a short read as complete.
                self.state = DownloadState.ERROR
                self.error = ServiceError(
                    ErrorKind.PROVIDER_UNAVAILABLE,
                    f"transfer interrupted for {remote_file.path}",
                    retryable=True,
                )
                emit(force=True)
                return

            incomplete.replace(dest)

        self.current_file = ""
        self.state = DownloadState.VERIFYING
        emit(force=True)

        ok, reason = self._verify_snapshot(snapshot_dir)
        if not ok:
            self.state = DownloadState.ERROR
            self.error = ServiceError(ErrorKind.INTERNAL, f"model verification failed: {reason}")
            emit(force=True)
            raise self.error

        self._write_ready_marker(snapshot_dir)
        self.state = DownloadState.COMPLETE
        emit(force=True)

    def _verify_snapshot(self, snapshot_dir: Path) -> tuple[bool, str]:
        for remote_file in self._files:
            path = snapshot_dir / remote_file.path
            if not path.exists():
                return False, f"missing file: {remote_file.path}"
            if path.stat().st_size != remote_file.size:
                return False, f"size mismatch: {remote_file.path}"
            if remote_file.sha256 is not None:
                actual = _sha256_file(path)
                if actual != remote_file.sha256:
                    return False, f"digest mismatch: {remote_file.path}"
        return True, ""

    def _write_ready_marker(self, snapshot_dir: Path) -> None:
        marker = snapshot_dir / _READY_MARKER
        payload = {
            "repo_id": self.repo_id,
            "revision": self.revision,
            "verified_at": time.time(),
        }
        marker.write_text(json.dumps(payload), encoding="utf-8")

    def verify(self) -> bool:
        """Re-check the snapshot on disk against the remote manifest (FR-12)."""
        snapshot_dir = self._models_dir
        if not self._files:
            try:
                self._files = self._hub_client.list_files(self.repo_id, self.revision)
            except Exception:
                return False
        ok, _reason = self._verify_snapshot(snapshot_dir)
        if not ok:
            marker = snapshot_dir / _READY_MARKER
            if marker.exists():
                marker.unlink()
            return False
        return True
