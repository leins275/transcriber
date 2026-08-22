"""Tests for the CUDA runtime acquisition core (Defect 1 fix).

No network, no real GPU, no real (multi-hundred-MB) wheels: a small,
in-memory zip stands in for each pinned wheel, and `FakeTransport` writes
deterministic bytes to disk (the same `model_download.py` test pattern) so
resume offsets and digest-mismatch handling can be asserted directly.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest

from transcription import runtime_dlls
from transcription.cuda_runtime import CudaPackage, CudaRuntimeDownload
from transcription.errors import ErrorKind
from transcription.model_download import DownloadState


def _make_wheel_bytes(
    *, nvidia_files: dict[str, bytes], extra_files: dict[str, bytes] | None = None
) -> bytes:
    """Build a real zip's bytes with an `nvidia/...` tree plus some
    non-`nvidia/` noise (dist-info etc.) that must never be extracted."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in nvidia_files.items():
            zf.writestr(f"nvidia/{name}", content)
        for name, content in (extra_files or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeTransport:
    """Writes pre-baked whole-wheel content in chunks; records resume offsets."""

    def __init__(self, contents: dict[str, bytes], *, chunk_size: int = 16) -> None:
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
        written = 0
        with open(dest, mode) as f:
            offset = resume_from
            while offset < len(data):
                if cancel.is_set():
                    return
                if (
                    self.interrupt_after_file == filename
                    and self.interrupt_at_bytes is not None
                    and resume_from + written >= self.interrupt_at_bytes
                ):
                    return
                chunk = data[offset : offset + self.chunk_size]
                f.write(chunk)
                offset += len(chunk)
                written += len(chunk)
                on_chunk(len(chunk))


def _one_package_download(
    tmp_path: Path, *, wheel_bytes: bytes, filename: str = "fake_pkg-1.0-py3-none-any.whl"
):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    pkg = CudaPackage(
        name="fake-pkg",
        version="1.0",
        filename=filename,
        url=f"https://fake.example/{filename}",
        size=len(wheel_bytes),
        sha256=_sha256(wheel_bytes),
    )
    transport = FakeTransport({filename: wheel_bytes})
    download = CudaRuntimeDownload(
        app_dir=app_dir,
        allowed_roots=(app_dir,),
        packages=(pkg,),
        transport=transport,
    )
    return download, transport, pkg, app_dir


def test_start_extracts_only_the_nvidia_tree_and_writes_ready_marker(tmp_path: Path) -> None:
    wheel_bytes = _make_wheel_bytes(
        nvidia_files={"cublas/bin/cublas64_12.dll": b"binary-dll-content"},
        extra_files={"fake_pkg-1.0.dist-info/METADATA": b"not nvidia"},
    )
    download, _transport, _pkg, app_dir = _one_package_download(tmp_path, wheel_bytes=wheel_bytes)
    events: list[dict] = []

    download.start(on_progress=events.append, progress_interval_sec=0.0)

    assert download.state == DownloadState.COMPLETE
    runtime_dir = app_dir / "runtime"
    assert (
        runtime_dir / "nvidia" / "cublas" / "bin" / "cublas64_12.dll"
    ).read_bytes() == b"binary-dll-content"
    assert not (runtime_dir / "fake_pkg-1.0.dist-info").exists()
    assert (runtime_dir / ".ready").exists()
    # The scratch wheel download directory is cleaned up once extraction
    # succeeds -- nothing multi-hundred-MB left duplicated on disk.
    assert not (runtime_dir / "_wheels").exists()
    assert events[-1]["state"] == "complete"
    assert events[-1]["downloaded_bytes"] == len(wheel_bytes)


def test_already_present_short_circuits_without_a_transport_call(tmp_path: Path) -> None:
    wheel_bytes = _make_wheel_bytes(nvidia_files={"cudnn/bin/cudnn64_9.dll": b"x"})
    download, transport, _pkg, app_dir = _one_package_download(tmp_path, wheel_bytes=wheel_bytes)
    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)
    assert download.state == DownloadState.COMPLETE

    # A brand new session against the same app_dir must see it as already
    # present and do nothing (FR-16-style idempotency: never re-fetch ~1.4
    # GB a prior first-run already got).
    transport2 = FakeTransport({})
    download2 = CudaRuntimeDownload(
        app_dir=app_dir,
        allowed_roots=(app_dir,),
        packages=(),
        transport=transport2,
    )
    events: list[dict] = []
    download2.start(on_progress=events.append, progress_interval_sec=0.0)

    assert download2.state == DownloadState.COMPLETE
    assert events[-1]["state"] == "complete"


def test_interrupted_transfer_resumes_from_existing_incomplete_blob(tmp_path: Path) -> None:
    content = b"c" * 500
    wheel_bytes = _make_wheel_bytes(nvidia_files={"cublas/bin/x.dll": content})
    download, transport, pkg, app_dir = _one_package_download(tmp_path, wheel_bytes=wheel_bytes)
    transport.interrupt_after_file = pkg.filename
    transport.interrupt_at_bytes = len(wheel_bytes) // 2

    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert download.state in (DownloadState.ERROR, DownloadState.DOWNLOADING)
    incomplete = app_dir / "runtime" / "_wheels" / (pkg.filename + ".incomplete")
    assert incomplete.exists()
    first_size = incomplete.stat().st_size
    assert 0 < first_size < len(wheel_bytes)

    transport.interrupt_after_file = None
    transport.interrupt_at_bytes = None
    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert transport.resume_offsets[-1] == first_size
    assert download.state == DownloadState.COMPLETE


def test_digest_mismatch_is_an_error_and_never_marks_usable(tmp_path: Path) -> None:
    wheel_bytes = _make_wheel_bytes(nvidia_files={"cublas/bin/x.dll": b"y"})
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    pkg = CudaPackage(
        name="fake-pkg",
        version="1.0",
        filename="fake_pkg-1.0-py3-none-any.whl",
        url="https://fake.example/fake_pkg-1.0-py3-none-any.whl",
        size=len(wheel_bytes),
        sha256="0" * 64,  # deliberately wrong
    )
    transport = FakeTransport({pkg.filename: wheel_bytes})
    download = CudaRuntimeDownload(
        app_dir=app_dir, allowed_roots=(app_dir,), packages=(pkg,), transport=transport
    )

    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert download.state == DownloadState.ERROR
    assert download.error is not None
    assert download.error.kind == ErrorKind.INTERNAL
    assert not (app_dir / "runtime" / ".ready").exists()


def test_cancel_stops_within_one_chunk_and_leaves_resumable_state(tmp_path: Path) -> None:
    content = b"z" * 500
    wheel_bytes = _make_wheel_bytes(nvidia_files={"cublas/bin/x.dll": content})
    download, _transport, pkg, app_dir = _one_package_download(tmp_path, wheel_bytes=wheel_bytes)

    chunks_seen = 0

    def stop_after_first_chunk(event: dict) -> None:
        nonlocal chunks_seen
        chunks_seen += 1
        if chunks_seen == 1:
            download.cancel()

    download.start(on_progress=stop_after_first_chunk, progress_interval_sec=0.0)

    assert download.state == DownloadState.CANCELLED
    incomplete = app_dir / "runtime" / "_wheels" / (pkg.filename + ".incomplete")
    assert incomplete.exists()
    assert 0 < incomplete.stat().st_size < len(wheel_bytes)


def test_start_re_registers_cuda_dll_dirs_after_extraction_so_no_restart_is_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1 (blocker): nothing previously made the just-downloaded CUDA DLLs
    visible to the *already-running* sidecar process that downloaded them --
    `register_cuda_dll_dirs()` only ran once, at `cli.main()` start, before
    this `runtime/nvidia` tree ever existed. `CudaRuntimeDownload.start()`
    must re-run that (idempotent, never-raising) registration itself the
    moment extraction succeeds, so a fresh job attempted right after
    `/v1/model/download` reports `complete` sees the updated DLL search path
    with no sidecar restart required."""
    wheel_bytes = _make_wheel_bytes(nvidia_files={"cublas/bin/cublas64_12.dll": b"x"})
    download, _transport, _pkg, app_dir = _one_package_download(tmp_path, wheel_bytes=wheel_bytes)

    # `register_cuda_dll_dirs` resolves the app-folder `runtime/nvidia` tree
    # via `TRANSCRIBER_APP_DIR` (or the running interpreter's location) --
    # point it at this test's fake app dir, and fake out the one real-OS
    # call (`os.add_dll_directory`) so this runs deterministically on any
    # platform, exactly like `test_runtime_dlls.py`'s own fakes.
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(app_dir))
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: None)
    calls: list[str] = []

    def fake_add_dll_directory(path: str) -> object:
        calls.append(path)
        return object()

    monkeypatch.setattr(runtime_dlls.os, "add_dll_directory", fake_add_dll_directory, raising=False)
    monkeypatch.setenv("PATH", r"C:\Windows\System32")

    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert download.state == DownloadState.COMPLETE
    expected_bin_dir = str(app_dir / "runtime" / "nvidia" / "cublas" / "bin")
    assert expected_bin_dir in calls, (
        "extraction completing must trigger a fresh DLL-directory registration"
    )
    path_entries = os.environ["PATH"].split(os.pathsep)
    assert expected_bin_dir in path_entries, (
        "the freshly-extracted directory must land on PATH with no process restart"
    )


def test_a_same_sized_but_corrupt_pre_existing_wheel_is_re_fetched_not_trusted(
    tmp_path: Path,
) -> None:
    """E7 (NFR-3): the resume-from-complete branch previously trusted any
    file already sitting at the wheel's destination path whose *size*
    matched, with no digest check -- a same-sized-but-corrupt leftover from
    a crash mid-write (the app folder is user-writable by design) would
    have been extracted onto the CUDA DLL search path unverified. It must
    instead be detected and re-fetched, exactly like a freshly-downloaded
    wheel that fails its digest check."""
    wheel_bytes = _make_wheel_bytes(nvidia_files={"cublas/bin/x.dll": b"good-content"})
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    pkg = CudaPackage(
        name="fake-pkg",
        version="1.0",
        filename="fake_pkg-1.0-py3-none-any.whl",
        url="https://fake.example/fake_pkg-1.0-py3-none-any.whl",
        size=len(wheel_bytes),
        sha256=_sha256(wheel_bytes),
    )
    # A same-length but wrong-content file already sitting at the wheel's
    # destination path, as if left over from a prior crash.
    wheels_dir = app_dir / "runtime" / "_wheels"
    wheels_dir.mkdir(parents=True)
    corrupt = bytes((b + 1) % 256 for b in wheel_bytes)
    assert len(corrupt) == len(wheel_bytes)
    (wheels_dir / pkg.filename).write_bytes(corrupt)

    transport = FakeTransport({pkg.filename: wheel_bytes})
    download = CudaRuntimeDownload(
        app_dir=app_dir, allowed_roots=(app_dir,), packages=(pkg,), transport=transport
    )

    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert download.state == DownloadState.COMPLETE
    assert transport.resume_offsets, "the corrupt pre-existing file must trigger a real fetch"
    assert (
        app_dir / "runtime" / "nvidia" / "cublas" / "bin" / "x.dll"
    ).read_bytes() == b"good-content"


def test_multiple_packages_merge_into_one_nvidia_tree(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    wheel_a = _make_wheel_bytes(nvidia_files={"cublas/bin/a.dll": b"a"})
    wheel_b = _make_wheel_bytes(nvidia_files={"cudnn/bin/b.dll": b"b"})
    pkg_a = CudaPackage(
        name="a",
        version="1",
        filename="a.whl",
        url="https://fake.example/a.whl",
        size=len(wheel_a),
        sha256=_sha256(wheel_a),
    )
    pkg_b = CudaPackage(
        name="b",
        version="1",
        filename="b.whl",
        url="https://fake.example/b.whl",
        size=len(wheel_b),
        sha256=_sha256(wheel_b),
    )
    transport = FakeTransport({"a.whl": wheel_a, "b.whl": wheel_b})
    download = CudaRuntimeDownload(
        app_dir=app_dir, allowed_roots=(app_dir,), packages=(pkg_a, pkg_b), transport=transport
    )

    download.start(on_progress=lambda _e: None, progress_interval_sec=0.0)

    assert download.state == DownloadState.COMPLETE
    runtime_dir = app_dir / "runtime"
    assert (runtime_dir / "nvidia" / "cublas" / "bin" / "a.dll").read_bytes() == b"a"
    assert (runtime_dir / "nvidia" / "cudnn" / "bin" / "b.dll").read_bytes() == b"b"
