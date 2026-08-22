"""Tests for scripts/bootstrap.ps1 (T2 — developer bootstrap script, FR-3).

These tests shell out to the real PowerShell 5.1 interpreter and run the
script against the *actual* state of the machine running the test suite.
They are therefore integration tests of the operator's machine, not pure
unit tests — that is deliberate per FR-3's acceptance criterion ("run on
the operator's current machine").

No test in this module installs anything system-wide: `-Check` is fully
read-only, and the "installer was attempted but a tool is still missing"
branch is exercised via `-DryRun`, which reports what it would run without
executing package-manager commands.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.ps1"

REQUIRED_ROW_NAMES = {"rust", "node", "npm", "uv", "make", "tauri-cli"}
ALL_ROW_NAMES = REQUIRED_ROW_NAMES | {"python"}


def _powershell() -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if exe is None:
        pytest.skip("no PowerShell interpreter on PATH")
    return exe


def _clean_env(ps: str) -> dict:
    """A subprocess environment reflecting the operator's persisted machine
    state (registry Machine + User `Path`), not this test runner's own
    ambient `os.environ`.

    Invoking this suite through `uv run` prepends uv's managed-CPython and
    build-cache `Scripts/` directories to the *current process's* PATH so
    that `uv run python ...` resolves — but that is an artifact of the test
    runner, not something a developer's plain shell has. Left uncorrected,
    `Get-Command python` inside the child PowerShell process would resolve
    to uv's real, working interpreter instead of the Store stub the
    detection logic is meant to be exercised against (FR-3).
    """
    probe = subprocess.run(
        [
            ps,
            "-NoProfile",
            "-Command",
            "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + "
            "[Environment]::GetEnvironmentVariable('Path','User')",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    clean_path = probe.stdout.strip()
    passthrough = (
        "SystemRoot",
        "windir",
        "USERPROFILE",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramData",
        "ComSpec",
        "PATHEXT",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    )
    env = {name: os.environ[name] for name in passthrough if name in os.environ}
    env["PATH"] = clean_path
    return env


def _run(*args: str) -> subprocess.CompletedProcess:
    ps = _powershell()
    return subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BOOTSTRAP), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=_clean_env(ps),
    )


def _rows_by_name(report: list[dict]) -> dict[str, dict]:
    return {row["name"]: row for row in report}


def test_check_json_emits_one_row_per_prerequisite():
    result = _run("-Check", "-Json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert isinstance(report, list)
    names = {row["name"] for row in report}
    assert names == ALL_ROW_NAMES
    for row in report:
        assert set(row.keys()) >= {"name", "present", "found_version", "install_command"}


def test_check_exits_zero_even_with_gaps():
    result = _run("-Check", "-Json")
    assert result.returncode == 0, result.stderr


def test_make_row_reports_missing_with_concrete_install_command():
    result = _run("-Check", "-Json")
    report = json.loads(result.stdout)
    rows = _rows_by_name(report)
    make_row = rows["make"]
    # Machine truth for this operator's environment: GNU make is absent.
    assert make_row["present"] is False
    assert make_row["found_version"] is None
    assert make_row["install_command"], "make row must carry a concrete install command"
    assert isinstance(make_row["install_command"], str)


def test_rust_row_reports_present_via_toolchain_detection():
    # Machine truth for this operator's environment: rustup has installed
    # the stable-x86_64-pc-windows-msvc toolchain (cargo/rustc live under
    # %USERPROFILE%\.cargo\bin, which may not yet be on a stale shell's
    # live PATH — detection must fall back to that well-known location).
    result = _run("-Check", "-Json")
    report = json.loads(result.stdout)
    rows = _rows_by_name(report)
    rust_row = rows["rust"]
    assert rust_row["present"] is True
    assert rust_row["found_version"], "a present rust row must report its version"


def test_tauri_cli_row_reports_missing_with_concrete_install_command():
    result = _run("-Check", "-Json")
    report = json.loads(result.stdout)
    rows = _rows_by_name(report)
    tauri_row = rows["tauri-cli"]
    assert tauri_row["present"] is False
    assert tauri_row["install_command"]
    assert "tauri-cli" in tauri_row["install_command"]


def test_python_row_reports_store_stub_not_present_and_never_recommends_the_stub():
    result = _run("-Check", "-Json")
    report = json.loads(result.stdout)
    rows = _rows_by_name(report)
    python_row = rows["python"]
    # Must be the distinct "stub" state — never plain `true`, and never
    # silently treated as satisfying the prerequisite.
    assert python_row["present"] == "stub"
    assert python_row["present"] is not True
    remedy = python_row["install_command"]
    assert remedy, "the stub row must still carry a remedy"
    assert "uv" in remedy.lower()
    assert "windowsapps" not in remedy.lower()
    assert "python.org" not in remedy.lower()


def test_node_npm_uv_rows_report_present_with_versions():
    result = _run("-Check", "-Json")
    report = json.loads(result.stdout)
    rows = _rows_by_name(report)
    for name in ("node", "npm", "uv"):
        row = rows[name]
        assert row["present"] is True, f"{name} row expected present=true, got {row}"
        assert row["found_version"], f"{name} row missing a found_version"


def test_dry_run_exits_nonzero_when_a_required_tool_is_still_missing():
    # Plain (non -Check) invocation attempts installs and then re-checks;
    # -DryRun proves that "still missing after the attempt" path without
    # mutating the machine (make and tauri-cli are known-absent here and
    # a dry run performs no real install, so the post-attempt check must
    # still report them missing and the script must exit non-zero).
    result = _run("-DryRun", "-Json")
    assert result.returncode != 0, result.stdout + result.stderr


def test_check_output_is_pure_json_with_no_interleaved_status_text():
    result = _run("-Check", "-Json")
    # json.loads would already fail if stdout had anything other than the
    # array on it; assert explicitly for a clearer failure message.
    json.loads(result.stdout)
