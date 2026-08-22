"""CUDA DLL directory registration shim (R3, windows-installer-build T5).

pip/uv-installed `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels place
their DLLs under `site-packages/nvidia/<component>/bin`, a directory Windows
does not search by default. `register_cuda_dll_dirs()` must register every
such directory that actually exists, be a safe no-op on a CPU-only machine
(no `nvidia` package at all), never touch a real GPU, and run before the
first `faster_whisper` import happens at process startup (F2's GPU-free
test pattern: fake the filesystem layout, never a real CUDA device).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from transcription import runtime_dlls


def _make_nvidia_tree(root: Path, components: list[str]) -> Path:
    """Build a fake `<root>/nvidia/<component>/bin` tree; returns `root`."""
    for component in components:
        (root / "nvidia" / component / "bin").mkdir(parents=True)
    return root


def _record_add_dll_directory(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _fake_add_dll_directory(path: str) -> Any:
        if not Path(path).is_dir():
            raise FileNotFoundError(path)
        calls.append(path)
        return object()

    monkeypatch.setattr(
        runtime_dlls.os, "add_dll_directory", _fake_add_dll_directory, raising=False
    )
    return calls


def test_registers_every_existing_nvidia_bin_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = _make_nvidia_tree(tmp_path / "site-packages", ["cublas", "cudnn"])
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: site_packages / "nvidia")
    calls = _record_add_dll_directory(monkeypatch)

    registered = runtime_dlls.register_cuda_dll_dirs()

    expected = {
        str(site_packages / "nvidia" / "cublas" / "bin"),
        str(site_packages / "nvidia" / "cudnn" / "bin"),
    }
    assert set(registered) == expected
    assert set(calls) == expected


def test_skips_a_component_with_no_bin_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = _make_nvidia_tree(tmp_path / "site-packages", ["cublas"])
    # A component directory that exists but has no `bin/` (e.g. a headers-only
    # sub-package) must never be handed to `os.add_dll_directory`.
    (site_packages / "nvidia" / "no_bin_here").mkdir(parents=True)
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: site_packages / "nvidia")
    calls = _record_add_dll_directory(monkeypatch)

    registered = runtime_dlls.register_cuda_dll_dirs()

    assert registered == [str(site_packages / "nvidia" / "cublas" / "bin")]
    assert calls == registered


def test_no_op_when_no_nvidia_tree_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No real `nvidia` package on this machine (confirmed absent) and
    # `sys.path` here carries no `nvidia` directory either -- the CPU-only-
    # machine case (spec: CPU is best-effort fallback).
    monkeypatch.setattr(runtime_dlls.sys, "path", [str(tmp_path)])
    calls = _record_add_dll_directory(monkeypatch)

    registered = runtime_dlls.register_cuda_dll_dirs()

    assert registered == []
    assert calls == []


def test_never_raises_when_add_dll_directory_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = _make_nvidia_tree(tmp_path / "site-packages", ["cublas"])
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: site_packages / "nvidia")

    def _boom(path: str) -> Any:
        raise OSError("simulated loader failure")

    monkeypatch.setattr(runtime_dlls.os, "add_dll_directory", _boom, raising=False)

    registered = runtime_dlls.register_cuda_dll_dirs()

    assert registered == []


def test_idempotent_across_repeated_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_packages = _make_nvidia_tree(tmp_path / "site-packages", ["cublas", "cudnn"])
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: site_packages / "nvidia")
    calls = _record_add_dll_directory(monkeypatch)

    first = runtime_dlls.register_cuda_dll_dirs()
    second = runtime_dlls.register_cuda_dll_dirs()

    assert first == second
    assert set(calls) == set(first)


def test_falls_back_to_scanning_sys_path_when_nvidia_package_not_importable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = _make_nvidia_tree(tmp_path / "site-packages", ["cudnn"])
    monkeypatch.setattr(runtime_dlls.sys, "path", [str(site_packages)])
    calls = _record_add_dll_directory(monkeypatch)

    registered = runtime_dlls.register_cuda_dll_dirs()

    assert registered == [str(site_packages / "nvidia" / "cudnn" / "bin")]
    assert calls == registered


def test_registered_directories_are_also_prepended_onto_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 3 (`docs/verification-installer.md`): `os.add_dll_directory`
    alone does not cover CTranslate2's own internal cuBLAS/cuDNN loading --
    the same directories must also land on the process `PATH`, in addition
    to (not instead of) the `add_dll_directory` registration."""
    site_packages = _make_nvidia_tree(tmp_path / "site-packages", ["cublas", "cudnn"])
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: site_packages / "nvidia")
    monkeypatch.setattr(runtime_dlls, "_app_folder_nvidia_dir", lambda: None)
    _record_add_dll_directory(monkeypatch)
    monkeypatch.setenv("PATH", r"C:\Windows\System32")

    registered = runtime_dlls.register_cuda_dll_dirs()

    path_entries = os.environ["PATH"].split(os.pathsep)
    for bin_dir in registered:
        assert bin_dir in path_entries
    assert path_entries[-1] == r"C:\Windows\System32"


def test_path_prepend_does_not_duplicate_an_already_present_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = _make_nvidia_tree(tmp_path / "site-packages", ["cublas"])
    bin_dir = site_packages / "nvidia" / "cublas" / "bin"
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: site_packages / "nvidia")
    monkeypatch.setattr(runtime_dlls, "_app_folder_nvidia_dir", lambda: None)
    _record_add_dll_directory(monkeypatch)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + r"C:\Windows\System32")

    runtime_dlls.register_cuda_dll_dirs()

    path_entries = os.environ["PATH"].split(os.pathsep)
    assert path_entries.count(str(bin_dir)) == 1


def test_also_scans_the_app_folder_runtime_nvidia_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect 1 (`docs/verification-installer.md`): the first-run CUDA
    download extracts wheels into `<app folder>/runtime/nvidia`, outside
    the interpreter's own `site-packages` -- the shim must scan that
    location too."""
    app_folder_nvidia = _make_nvidia_tree(tmp_path / "app" / "runtime", ["cudnn"]) / "nvidia"
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: None)
    monkeypatch.setattr(runtime_dlls, "_app_folder_nvidia_dir", lambda: app_folder_nvidia)
    calls = _record_add_dll_directory(monkeypatch)

    registered = runtime_dlls.register_cuda_dll_dirs()

    expected = str(app_folder_nvidia / "cudnn" / "bin")
    assert registered == [expected]
    assert calls == [expected]


def test_app_folder_nvidia_dir_resolves_via_transcriber_app_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    runtime_nvidia = _make_nvidia_tree(app_dir / "runtime", ["cublas"]) / "nvidia"
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(app_dir))

    found = runtime_dlls._app_folder_nvidia_dir()

    assert found == runtime_nvidia


def test_app_folder_nvidia_dir_is_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBER_APP_DIR", str(tmp_path / "app-with-no-runtime-dir"))

    assert runtime_dlls._app_folder_nvidia_dir() is None


def test_deduplicates_a_bin_dir_present_under_both_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = _make_nvidia_tree(tmp_path / "site-packages", ["cublas"])
    app_nvidia = _make_nvidia_tree(tmp_path / "app" / "runtime", ["cublas"]) / "nvidia"
    monkeypatch.setattr(runtime_dlls, "_nvidia_package_dir", lambda: site_packages / "nvidia")
    monkeypatch.setattr(runtime_dlls, "_app_folder_nvidia_dir", lambda: app_nvidia)
    calls = _record_add_dll_directory(monkeypatch)

    registered = runtime_dlls.register_cuda_dll_dirs()

    assert len(registered) == 2  # both distinct directories, one per location
    assert len(calls) == 2


def test_invoked_from_cli_main_before_any_provider_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service/CLI entry point (`transcription.cli.main`) must call the
    shim, so it always runs before a request triggers the lazy
    `faster_whisper` import in `providers.local_whisper` (F2's registry
    resolves providers lazily; this is the only hook point before any of
    them could ever load)."""
    import transcription.cli as cli_mod

    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "register_cuda_dll_dirs", lambda: calls.append("called") or [])

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(["--help"])

    assert exc_info.value.code == 0
    assert calls == ["called"]
