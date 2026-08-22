"""Tests for `scripts/verify_install.py` (T14).

Pure-function unit cases over a synthetic install tree under `tmp_path` --
no real installer, no real Windows install, no network. The real
install/uninstall/upgrade/silent-install proof is `docs/verification-
installer.md` and the manual smoke checklist, executed by hand against the
real built `.exe` (this module's own docstring says as much: it is the
checker `verify_install.py --install-dir ... --artifact ...` runs for real
during that pass).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_install  # noqa: E402


# --- app-folder skeleton (FR-8) ------------------------------------------


def test_flags_a_missing_models_logs_or_data_directory(tmp_path: Path) -> None:
    install_dir = tmp_path / "Transcriber"
    install_dir.mkdir()
    (install_dir / "models").mkdir()
    (install_dir / "logs").mkdir()
    # "data" deliberately missing

    problems = verify_install.check_directory_skeleton(install_dir)

    assert len(problems) == 1
    assert str(install_dir / "data") in problems[0]


def test_passes_when_all_three_skeleton_directories_exist(tmp_path: Path) -> None:
    install_dir = tmp_path / "Transcriber"
    install_dir.mkdir()
    for name in ("models", "logs", "data"):
        (install_dir / name).mkdir()

    assert verify_install.check_directory_skeleton(install_dir) == []


# --- writability (FR-8 acceptance) ---------------------------------------


def test_flags_a_non_writable_directory(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist" / "models"

    problems = verify_install.check_writable_dir(missing)

    assert problems != []


def test_passes_on_a_writable_directory(tmp_path: Path) -> None:
    writable = tmp_path / "models"
    writable.mkdir()

    assert verify_install.check_writable_dir(writable) == []
    # the probe file must not be left behind
    assert list(writable.iterdir()) == []


# --- artifact size gate (NFR-1) ------------------------------------------


def test_flags_an_installer_artifact_over_the_size_budget(tmp_path: Path) -> None:
    oversized = tmp_path / "setup.exe"
    with open(oversized, "wb") as fh:
        fh.truncate(verify_install.SIZE_LIMIT_BYTES + 1)

    problems = verify_install.check_artifact_size(oversized)

    assert problems != []


def test_passes_at_exactly_the_size_budget(tmp_path: Path) -> None:
    exact = tmp_path / "setup.exe"
    with open(exact, "wb") as fh:
        fh.truncate(verify_install.SIZE_LIMIT_BYTES)

    assert verify_install.check_artifact_size(exact) == []


# --- config.json validity (FR-10/11) -------------------------------------


def test_flags_invalid_json(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{not valid json", encoding="utf-8")

    problems = verify_install.check_config_json(config_path)

    assert problems != []


def test_flags_a_config_missing_meetings_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    problems = verify_install.check_config_json(config_path)

    assert any("meetings_root" in p for p in problems)


def test_flags_a_config_missing_schema_version(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"meetings_root": "D:\\Meetings"}), encoding="utf-8")

    problems = verify_install.check_config_json(config_path)

    assert any("schema_version" in p for p in problems)


def test_passes_on_a_well_formed_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "meetings_root": "D:\\Meetings"}), encoding="utf-8"
    )

    assert verify_install.check_config_json(config_path) == []


# --- vault-root resolution (FR-11 acceptance) -----------------------------


def test_resolve_vault_root_reads_meetings_root_the_way_the_app_does(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "meetings_root": "D:\\Meetings"}), encoding="utf-8"
    )

    assert verify_install.resolve_vault_root(config_path) == "D:\\Meetings"


def test_resolve_vault_root_tolerates_a_bom(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    body = json.dumps({"schema_version": 1, "meetings_root": "D:\\Meetings"})
    config_path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    assert verify_install.resolve_vault_root(config_path) == "D:\\Meetings"


def test_resolve_vault_root_is_none_when_the_key_is_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    assert verify_install.resolve_vault_root(config_path) is None


def test_reports_a_mismatch_against_the_value_the_user_picked(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "meetings_root": "D:\\Meetings"}), encoding="utf-8"
    )

    problems = verify_install.check_vault_root_matches(config_path, expected="D:\\Other")

    assert problems != []


def test_no_mismatch_when_the_resolved_root_matches(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "meetings_root": "D:\\Meetings"}), encoding="utf-8"
    )

    assert verify_install.check_vault_root_matches(config_path, expected="D:\\Meetings") == []
