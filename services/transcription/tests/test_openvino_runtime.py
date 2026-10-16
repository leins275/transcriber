"""`openvino_runtime.py`: the first-run OpenVINO whisper runtime manifest
and its presence/activation rules (the speaker runtime's shape)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcription import openvino_runtime as ovr
from transcription.openvino_runtime_packages import OPENVINO_WHEELS, TOTAL_BYTES


def test_manifest_pins_the_openvino_stack_as_windows_wheels() -> None:
    names = {wheel.name for wheel in OPENVINO_WHEELS}
    assert {"openvino", "openvino-genai", "openvino-tokenizers"} <= names
    for wheel in OPENVINO_WHEELS:
        assert wheel.filename.endswith(".whl")
        assert "win_amd64" in wheel.filename or "none-any" in wheel.filename
        assert len(wheel.sha256) == 64
        assert wheel.size > 0
        assert wheel.url.startswith("https://")
    assert TOTAL_BYTES == sum(wheel.size for wheel in OPENVINO_WHEELS)
    assert ovr.runtime_total_bytes() == TOTAL_BYTES
    assert all(pkg.dest_subdir == ovr.OPENVINO_SUBDIR for pkg in ovr.OPENVINO_PACKAGES)


def _marker(packages: tuple[ovr.CudaPackage, ...]) -> str:
    return json.dumps({"packages": {pkg.name: pkg.version for pkg in packages}})


def test_presence_needs_a_marker_naming_this_manifest(tmp_path: Path) -> None:
    assert ovr.is_openvino_runtime_present(tmp_path) is False

    runtime = ovr.openvino_runtime_dir(tmp_path)
    runtime.mkdir(parents=True)
    (runtime / ".ready").write_text(json.dumps({"packages": {"openvino": "0.0"}}))
    assert ovr.is_openvino_runtime_present(tmp_path) is False, "a stale pin reads as absent"

    (runtime / ".ready").write_text(_marker(ovr.OPENVINO_PACKAGES))
    assert ovr.is_openvino_runtime_present(tmp_path) is True


def test_activate_runtime_puts_a_present_tree_on_sys_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert ovr.activate_runtime(tmp_path) is None

    runtime = ovr.openvino_runtime_dir(tmp_path)
    runtime.mkdir(parents=True)
    (runtime / ".ready").write_text(_marker(ovr.OPENVINO_PACKAGES))
    assert ovr.activate_runtime(tmp_path) == runtime
    assert ovr.activate_runtime(tmp_path) == runtime
    assert sys.path.count(str(runtime)) == 1
    assert sys.path[0] == str(runtime)


def test_a_stale_tree_is_cleared_before_a_fresh_fetch(tmp_path: Path) -> None:
    runtime = ovr.openvino_runtime_dir(tmp_path)
    runtime.mkdir(parents=True)
    (runtime / "openvino").mkdir()
    (runtime / ".ready").write_text(json.dumps({"packages": {"openvino": "0.0"}}))
    config = SimpleNamespace(app_dir=tmp_path)

    download = ovr.build_openvino_runtime_download(config)  # type: ignore[arg-type]

    assert not runtime.exists()
    assert download.already_present() is False


def test_the_weights_download_targets_the_pinned_repo(tmp_path: Path) -> None:
    config = SimpleNamespace(
        app_dir=tmp_path,
        whisper_openvino_repo="OpenVINO/whisper-large-v3-turbo-int8-ov",
        whisper_openvino_revision="b568445dd5dc8c695bde596f8acbb4694fd6ba64",
    )

    download = ovr.build_openvino_weights_download(config)  # type: ignore[arg-type]

    assert download.repo_id == "OpenVINO/whisper-large-v3-turbo-int8-ov"
    assert download.revision == "b568445dd5dc8c695bde596f8acbb4694fd6ba64"
    assert (tmp_path / "models" / "whisper-openvino" / "whisper-large-v3-turbo-int8-ov").is_dir()
