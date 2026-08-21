"""Tests for the path allowlist and traversal defence (FR-9)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from transcription.errors import ErrorKind, ServiceError
from transcription.paths import ensure_output_dir, resolve_under_roots


def test_file_directly_under_allowed_root_resolves(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "audio.wav"
    target.write_bytes(b"data")

    resolved = resolve_under_roots(target, [root], must_exist=True)

    assert resolved == target.resolve()
    assert resolved.is_absolute()


def test_windows_style_traversal_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(
            str(root) + "\\..\\..\\Windows\\System32\\config\\SAM",
            [root],
            must_exist=False,
        )

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST


def test_posix_style_traversal_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(
            str(root) + "/../../Windows/System32/config/SAM",
            [root],
            must_exist=False,
        )

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST


def test_unc_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(r"\\server\share\x.mp4", [root], must_exist=False)

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST


def test_device_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(r"\\?\C:\x.mp4", [root], must_exist=False)

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST


def test_symlink_escaping_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_bytes(b"secret")

    link = root / "escape.txt"
    try:
        link.symlink_to(outside_file)
    except OSError:
        pytest.skip("symlink creation requires Developer Mode or admin on this machine")

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(link, [root], must_exist=True)

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST


def test_symlink_staying_inside_root_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    real = root / "real.txt"
    real.write_bytes(b"data")

    link = root / "link.txt"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation requires Developer Mode or admin on this machine")

    resolved = resolve_under_roots(link, [root], must_exist=True)

    assert resolved == real.resolve()


def test_escape_via_dotdot_without_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    sibling_file = sibling / "x.mp4"
    sibling_file.write_bytes(b"data")

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(root / ".." / "sibling" / "x.mp4", [root], must_exist=True)

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST


def test_empty_allowed_roots_rejects_everything(tmp_path: Path) -> None:
    target = tmp_path / "audio.wav"
    target.write_bytes(b"data")

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(target, [], must_exist=True)

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST


def test_must_exist_missing_file_raises_with_filename_in_message(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    missing = root / "does-not-exist.wav"

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(missing, [root], must_exist=True)

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST
    assert "does-not-exist.wav" in excinfo.value.message


def test_ensure_output_dir_creates_missing_directory_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    target = root / "outputs" / "job-1"

    resolved = ensure_output_dir(target, [root])

    assert resolved.is_dir()
    assert resolved == target.resolve()


def test_ensure_output_dir_refuses_to_create_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside" / "job-1"

    with pytest.raises(ServiceError) as excinfo:
        ensure_output_dir(outside, [root])

    assert excinfo.value.kind == ErrorKind.INVALID_REQUEST
    assert not outside.exists()


@pytest.mark.skipif(os.name != "nt", reason="case-insensitivity is a Windows-specific guarantee")
def test_case_insensitive_root_comparison_on_windows(tmp_path: Path) -> None:
    root = tmp_path / "Vault"
    root.mkdir()
    target = root / "x.wav"
    target.write_bytes(b"data")

    differently_cased_candidate = Path(str(target).replace("Vault", "vault"))

    resolved = resolve_under_roots(differently_cased_candidate, [root], must_exist=True)

    assert resolved == target.resolve()


def test_rejection_message_never_echoes_full_attacker_path(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    attacker_path = str(root) + "\\..\\..\\Windows\\System32\\config\\SAM"

    with pytest.raises(ServiceError) as excinfo:
        resolve_under_roots(attacker_path, [root], must_exist=False)

    assert attacker_path not in excinfo.value.message
    assert str(root) not in excinfo.value.message
