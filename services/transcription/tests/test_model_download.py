"""Tests for the model download core (FR-12, NFR-3).

No network, no GPU, no real weights: `huggingface_hub` is fully faked via a
`FakeHubClient` and the byte transport is a `FakeTransport` that writes
deterministic bytes to disk so resume offsets can be asserted directly.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from transcription.errors import ErrorKind, ServiceError
from transcription.model_download import DownloadState, ModelDownload, RemoteFile

CONTENT_A = b"a" * 100
CONTENT_B = b"b" * 10_000
SHA_A = hashlib.sha256(CONTENT_A).hexdigest()
SHA_B = hashlib.sha256(CONTENT_B).hexdigest()


class FakeHubClient:
    """Stands in for the huggingface_hub calls -- no network."""

    def __init__(self, files: list[RemoteFile]) -> None:
        self.files = files
        self.list_calls = 0

    def list_files(self, repo_id: str, revision: str) -> list[RemoteFile]:
        self.list_calls += 1
        return self.files

    def file_url(self, repo_id: str, revision: str, filename: str) -> str:
        return f"https://fake.example/{repo_id}/{revision}/{filename}"


class FakeTransport:
    """Writes pre-baked content in chunks, recording every resume offset."""

    def __init__(self, contents: dict[str, bytes], *, chunk_size: int = 10) -> None:
        self.contents = contents
        self.chunk_size = chunk_size
        self.resume_offsets: list[int] = []
        self.interrupt_after_file: str | None = None
        self.interrupt_at_bytes: int | None = None

    def fetch(self, *, url, dest, resume_from, on_chunk, cancel):
        self.resume_offsets.append(resume_from)
        filename = Path(dest).name.removesuffix(".incomplete")
        data = self.contents[filename]
        mode = "ab" if resume_from else "wb"
        written_this_call = 0
        with open(dest, mode) as f:
            offset = resume_from
            while offset < len(data):
                if cancel.is_set():
                    return
                if (
                    self.interrupt_after_file == filename
                    and self.interrupt_at_bytes is not None
                    and resume_from + written_this_call >= self.interrupt_at_bytes
                ):
                    return
                chunk = data[offset : offset + self.chunk_size]
                f.write(chunk)
                offset += len(chunk)
                written_this_call += len(chunk)
                on_chunk(len(chunk))


def _single_file_download(tmp_path: Path, *, content: bytes = CONTENT_B, digest: str = SHA_B):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    hub = FakeHubClient([RemoteFile(path="model.bin", size=len(content), sha256=digest)])
    transport = FakeTransport({"model.bin": content})
    download = ModelDownload(
        models_dir=models_dir,
        allowed_roots=(tmp_path,),
        hub_client=hub,
        transport=transport,
    )
    return download, transport, hub, models_dir


def test_start_reports_progress_shape_and_fires_repeatedly(tmp_path: Path) -> None:
    download, _transport, _hub, _models_dir = _single_file_download(tmp_path)
    events: list[dict] = []

    download.start(on_progress=events.append, progress_interval_sec=0.0)

    assert len(events) >= 2
    for event in events:
        assert set(event) == {"downloaded_bytes", "total_bytes", "percent", "file", "state"}
    assert events[-1]["state"] == "complete"
    assert events[-1]["downloaded_bytes"] == len(CONTENT_B)
    assert events[-1]["percent"] == pytest.approx(100.0)


def test_progress_callback_fires_at_least_once_a_second(tmp_path: Path) -> None:
    download, _transport, _hub, _models_dir = _single_file_download(tmp_path)
    timestamps: list[float] = []

    def record(event: dict) -> None:
        timestamps.append(time.monotonic())

    download.start(on_progress=record, progress_interval_sec=1.0)

    # A throttled sink must still emit a start event and a final (forced)
    # completion event even though the whole fake transfer takes far less
    # than a second -- proving the interval is a rate *limit*, not a
    # requirement to wait a full second before ever reporting.
    assert len(timestamps) >= 2


def test_interrupted_transfer_resumes_from_existing_incomplete_blob(tmp_path: Path) -> None:
    download, transport, hub, models_dir = _single_file_download(tmp_path)
    transport.interrupt_after_file = "model.bin"
    transport.interrupt_at_bytes = len(CONTENT_B) // 2

    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert download.state == DownloadState.ERROR or download.state == DownloadState.DOWNLOADING
    snapshot_dir = models_dir  # flat: model_download writes directly into models_dir now
    incomplete = snapshot_dir / "model.bin.incomplete"
    assert incomplete.exists()
    first_size = incomplete.stat().st_size
    assert 0 < first_size < len(CONTENT_B)

    # Restart: resume must pick up from the existing partial blob, not 0.
    transport.interrupt_after_file = None
    transport.interrupt_at_bytes = None
    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert transport.resume_offsets[-1] == first_size
    assert download.state == DownloadState.COMPLETE


def test_verify_fails_on_truncated_file_and_does_not_mark_usable(tmp_path: Path) -> None:
    download, _transport, _hub, models_dir = _single_file_download(tmp_path)
    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)
    assert download.state == DownloadState.COMPLETE

    snapshot_dir = models_dir  # flat: model_download writes directly into models_dir now
    (snapshot_dir / "model.bin").write_bytes(CONTENT_B[:-1])  # truncate by one byte

    assert download.verify() is False
    assert not (snapshot_dir / ".ready").exists()


def test_verify_fails_on_digest_mismatch(tmp_path: Path) -> None:
    download, _transport, _hub, models_dir = _single_file_download(tmp_path)
    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)
    assert download.state == DownloadState.COMPLETE

    snapshot_dir = models_dir  # flat: model_download writes directly into models_dir now
    corrupted = bytes((b + 1) % 256 for b in CONTENT_B)
    (snapshot_dir / "model.bin").write_bytes(corrupted)

    assert download.verify() is False
    assert not (snapshot_dir / ".ready").exists()


def test_cancel_stops_within_one_chunk_and_leaves_resumable_state(tmp_path: Path) -> None:
    download, transport, _hub, models_dir = _single_file_download(tmp_path)

    chunks_seen = 0

    def stop_after_first_chunk(event: dict) -> None:
        nonlocal chunks_seen
        chunks_seen += 1
        if chunks_seen == 1:
            download.cancel()

    download.start(on_progress=stop_after_first_chunk, progress_interval_sec=0.0)

    assert download.state == DownloadState.CANCELLED
    snapshot_dir = models_dir  # flat: model_download writes directly into models_dir now
    incomplete = snapshot_dir / "model.bin.incomplete"
    assert incomplete.exists()
    assert 0 < incomplete.stat().st_size < len(CONTENT_B)
    assert not (snapshot_dir / "model.bin").exists()


def test_success_lands_snapshot_and_writes_ready_marker(tmp_path: Path) -> None:
    download, _transport, _hub, models_dir = _single_file_download(tmp_path)

    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert download.state == DownloadState.COMPLETE
    snapshot_dir = models_dir  # flat: model_download writes directly into models_dir now
    assert (snapshot_dir / "model.bin").read_bytes() == CONTENT_B
    ready = snapshot_dir / ".ready"
    assert ready.exists()
    assert download.revision in ready.read_text(encoding="utf-8")


def test_snapshot_is_written_flat_with_no_extra_subdirectory(tmp_path: Path) -> None:
    """Blocker 2 (`docs/verification-installer.md`): `model_download.py`
    must write directly into whatever `models_dir` it is given -- the app
    already passes the exact, model-specific directory via
    `TRANSCRIBER_MODEL_PATH` (`docs/config-contract.md`), so nesting a
    revision-named (or any other) subdirectory underneath it is exactly the
    layout mismatch that broke `providers/local_whisper.py`'s loader, which
    passes `models_dir` straight through as a literal directory."""
    download, _transport, _hub, models_dir = _single_file_download(tmp_path)

    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert download.state == DownloadState.COMPLETE
    assert (models_dir / "model.bin").is_file()
    assert (models_dir / ".ready").is_file()
    assert [p.name for p in models_dir.iterdir() if p.is_dir()] == []


def test_target_path_outside_configured_app_folder_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "models"
    allowed_root = tmp_path / "app"
    allowed_root.mkdir()

    with pytest.raises(ServiceError) as excinfo:
        ModelDownload(
            models_dir=outside,
            allowed_roots=(allowed_root,),
            hub_client=FakeHubClient([]),
            transport=FakeTransport({}),
        )

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST
