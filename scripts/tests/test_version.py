"""Tests for scripts/sync_version.py (FR-5: single version source of truth).

Self-contained by design: there is deliberately no scripts/tests/conftest.py
(see plan.md), so this module derives the repo root itself instead of relying
on a shared fixture.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sync_version.py"

_spec = importlib.util.spec_from_file_location("sync_version", SCRIPT)
sync_version = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["sync_version"] = sync_version  # dataclass introspection needs this
_spec.loader.exec_module(sync_version)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _snapshot(paths):
    # E15: bytes, not text -- `Path.write_text` without an explicit
    # `newline=` applies platform-default translation on restore, which would
    # silently flip a CRLF-on-disk manifest to LF (or vice versa) every time
    # a test in this module runs, fighting `sync_version.py`'s own
    # newline-preserving fix.
    return {p: p.read_bytes() for p in paths}


def _restore(snapshot):
    for path, content in snapshot.items():
        path.write_bytes(content)


def test_version_file_is_one_semver_line():
    text = (REPO_ROOT / "version.txt").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 1
    assert re.match(r"^\d+\.\d+\.\d+$", lines[0])


def test_all_manifests_match_version_txt():
    version = sync_version.read_version()
    for manifest in sync_version.MANIFESTS:
        assert sync_version.manifest_version(manifest) == version, manifest.path


def test_check_passes_on_synced_tree():
    result = _run("--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_fails_naming_drifting_file():
    target = sync_version.MANIFESTS[0].path  # apps/desktop/src-tauri/tauri.conf.json
    snapshot = _snapshot([target])
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        data["version"] = "9.9.9-drift"
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        result = _run("--check")

        assert result.returncode != 0
        assert "tauri.conf.json" in result.stdout + result.stderr
    finally:
        _restore(snapshot)


def test_set_then_check_syncs_every_manifest_and_is_idempotent():
    version_txt = REPO_ROOT / "version.txt"
    # Cargo.lock is restored too: `--set` now writes the workspace members'
    # versions there as well, so a snapshot without it leaves the lockfile
    # dirty at the test's scratch version.
    tracked = [version_txt, sync_version.CARGO_LOCK, sync_version.UV_LOCK] + [
        m.path for m in sync_version.MANIFESTS
    ]
    snapshot = _snapshot(tracked)
    try:
        result = _run("--set", "9.9.9")
        assert result.returncode == 0, result.stdout + result.stderr

        check_result = _run("--check")
        assert check_result.returncode == 0, check_result.stdout + check_result.stderr

        for manifest in sync_version.MANIFESTS:
            assert sync_version.manifest_version(manifest) == "9.9.9"

        first_pass = _snapshot(tracked)

        # Re-running --set with the same version must be byte-for-byte identical.
        second_result = _run("--set", "9.9.9")
        assert second_result.returncode == 0, second_result.stdout + second_result.stderr
        assert _snapshot(tracked) == first_pass
    finally:
        _restore(snapshot)


def test_write_json_version_preserves_a_crlf_files_newline_style(tmp_path):
    """E15: a manifest committed CRLF (this repo's default on Windows, via
    `core.autocrlf=true`) must stay CRLF after a version write -- not flip to
    LF, and not flip to the platform default either."""
    path = tmp_path / "crlf.json"
    path.write_bytes(b'{\r\n  "name": "x",\r\n  "version": "0.0.0"\r\n}\r\n')

    sync_version._write_json_version(path, "1.2.3")

    raw = path.read_bytes()
    assert b'"version": "1.2.3"' in raw
    assert raw.count(b"\r\n") == raw.count(b"\n"), "every line ending must stay CRLF"


def test_write_json_version_preserves_an_lf_files_newline_style(tmp_path):
    """E15: the flip side -- a manifest already LF (Prettier's own output)
    must stay LF, not be forced to CRLF by a naive `Path.write_text` on
    Windows."""
    path = tmp_path / "lf.json"
    path.write_bytes(b'{\n  "name": "x",\n  "version": "0.0.0"\n}\n')

    sync_version._write_json_version(path, "1.2.3")

    raw = path.read_bytes()
    assert b'"version": "1.2.3"' in raw
    assert b"\r\n" not in raw, "an LF file must never gain CRLF line endings"


def test_write_toml_version_preserves_the_files_existing_newline_style(tmp_path):
    path = tmp_path / "crlf.toml"
    path.write_bytes(b'[package]\r\nname = "x"\r\nversion = "0.0.0"\r\n')

    sync_version._write_toml_version(path, "package", "1.2.3")

    raw = path.read_bytes()
    assert b'version = "1.2.3"' in raw
    assert raw.count(b"\r\n") == raw.count(b"\n"), "every line ending must stay CRLF"


def test_print_artifact_name_embeds_the_version():
    version_txt = REPO_ROOT / "version.txt"
    # Cargo.lock is restored too: `--set` now writes the workspace members'
    # versions there as well, so a snapshot without it leaves the lockfile
    # dirty at the test's scratch version.
    tracked = [version_txt, sync_version.CARGO_LOCK, sync_version.UV_LOCK] + [
        m.path for m in sync_version.MANIFESTS
    ]
    snapshot = _snapshot(tracked)
    try:
        _run("--set", "1.2.3")
        result_a = _run("--print-artifact-name")
        assert result_a.returncode == 0, result_a.stdout + result_a.stderr
        name_a = result_a.stdout.strip()
        assert "1.2.3" in name_a

        _run("--set", "1.2.4")
        result_b = _run("--print-artifact-name")
        name_b = result_b.stdout.strip()
        assert "1.2.4" in name_b
        assert name_a != name_b
    finally:
        _restore(snapshot)


# -- Cargo.lock (the sixth and seventh copies of the version) ---------------
#
# `tauri build --locked` -- what scripts/build_installer.py runs -- fails
# outright when a workspace member's version in Cargo.lock disagrees with its
# Cargo.toml. These pin that the bump keeps them together.


def test_cargo_lock_workspace_members_match_version_txt():
    version = sync_version.read_version()
    for package in sync_version.LOCK_PACKAGES:
        assert sync_version.lock_version(package) == version, package.name


def test_check_fails_when_only_cargo_lock_drifts():
    lock = sync_version.CARGO_LOCK
    snapshot = _snapshot([lock])
    try:
        text = lock.read_text(encoding="utf-8")
        pattern = sync_version._lock_version_re("vault")
        lock.write_text(
            pattern.sub(r"\g<1>9.9.9\g<3>", text, count=1),
            encoding="utf-8",
            newline=sync_version._detect_newline(lock),
        )

        result = _run("--check")

        assert result.returncode == 1
        assert "Cargo.lock (vault)" in result.stderr
    finally:
        _restore(snapshot)


def test_set_syncs_cargo_lock_members_without_touching_registry_crates():
    lock = sync_version.CARGO_LOCK
    paths = [REPO_ROOT / "version.txt", lock, sync_version.UV_LOCK] + [
        m.path for m in sync_version.MANIFESTS
    ]
    snapshot = _snapshot(paths)
    try:
        before = lock.read_text(encoding="utf-8")
        # A registry crate's own version line, which must survive untouched.
        serde_before = sync_version._lock_version_re("serde").search(before)

        sync_version.set_version("9.9.9")

        for package in sync_version.LOCK_PACKAGES:
            assert sync_version.lock_version(package) == "9.9.9", package.name

        after = lock.read_text(encoding="utf-8")
        if serde_before is not None:
            serde_after = sync_version._lock_version_re("serde").search(after)
            assert serde_after is not None
            assert serde_after.group(2) == serde_before.group(2)
        # Exactly two lines differ: one per workspace member.
        changed = [
            (a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b
        ]
        cargo_packages = [p for p in sync_version.LOCK_PACKAGES if p.path == lock]
        assert len(changed) == len(cargo_packages), changed
    finally:
        _restore(snapshot)


def test_set_preserves_cargo_locks_newline_style():
    lock = sync_version.CARGO_LOCK
    paths = [REPO_ROOT / "version.txt", lock, sync_version.UV_LOCK] + [
        m.path for m in sync_version.MANIFESTS
    ]
    snapshot = _snapshot(paths)
    try:
        had_crlf = b"\r\n" in lock.read_bytes()

        sync_version.set_version("9.9.9")

        assert (b"\r\n" in lock.read_bytes()) is had_crlf
    finally:
        _restore(snapshot)


def test_uv_lock_project_entry_matches_version_txt():
    # `uv export --frozen` in the pyenv bake refuses a lockfile whose project
    # version disagrees with pyproject.toml -- the release build's first real
    # failure, before this was synced.
    version = sync_version.read_version()
    transcription = next(
        package
        for package in sync_version.LOCK_PACKAGES
        if package.path == sync_version.UV_LOCK
    )
    assert sync_version.lock_version(transcription) == version


def test_check_fails_when_only_uv_lock_drifts():
    lock = sync_version.UV_LOCK
    snapshot = _snapshot([lock])
    try:
        text = lock.read_text(encoding="utf-8")
        pattern = sync_version._lock_version_re("transcription")
        lock.write_text(
            pattern.sub(r"\g<1>9.9.9\g<3>", text, count=1),
            encoding="utf-8",
            newline=sync_version._detect_newline(lock),
        )

        result = _run("--check")

        assert result.returncode == 1
        assert "services/transcription/uv.lock (transcription)" in result.stderr
    finally:
        _restore(snapshot)


def test_set_syncs_the_uv_lock_project_without_touching_dependencies():
    lock = sync_version.UV_LOCK
    paths = [REPO_ROOT / "version.txt", lock, sync_version.CARGO_LOCK] + [
        m.path for m in sync_version.MANIFESTS
    ]
    snapshot = _snapshot(paths)
    try:
        before = lock.read_text(encoding="utf-8")

        sync_version.set_version("9.9.9")

        after = lock.read_text(encoding="utf-8")
        changed = [
            (a, b)
            for a, b in zip(before.splitlines(), after.splitlines(), strict=True)
            if a != b
        ]
        # Exactly one line: the project's own version. Every other
        # `[[package]]` in that file is a third-party dependency.
        assert len(changed) == 1, changed
    finally:
        _restore(snapshot)
