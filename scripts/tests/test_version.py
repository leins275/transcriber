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
    tracked = [version_txt] + [m.path for m in sync_version.MANIFESTS]
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
    tracked = [version_txt] + [m.path for m in sync_version.MANIFESTS]
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
