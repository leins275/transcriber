"""Packaging and tooling configuration tests for the transcription service.

Covers FR-1 (self-contained uv package, console script), FR-13 (NOTICE
attribution) and FR-15 (gpu marker excluded by default addopts).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import transcription

PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"
NOTICE_PATH = Path(__file__).resolve().parent.parent / "NOTICE"


def _load_pyproject() -> dict:
    with PYPROJECT_PATH.open("rb") as f:
        return tomllib.load(f)


def test_import_exposes_version_matching_pyproject() -> None:
    pyproject = _load_pyproject()
    assert transcription.__version__ == pyproject["project"]["version"]


def test_console_script_entry_point_declared_without_importing_cli() -> None:
    # cli.py does not exist until T15 -- this test must never import it.
    pyproject = _load_pyproject()
    scripts = pyproject["project"]["scripts"]
    assert scripts["transcription-service"] == "transcription.cli:main"


def test_the_mcp_console_script_is_declared() -> None:
    pyproject = _load_pyproject()
    scripts = pyproject["project"]["scripts"]
    assert scripts["transcriber-mcp"] == "transcription.mcp_server:main"


def test_requires_python_is_at_least_312() -> None:
    pyproject = _load_pyproject()
    assert pyproject["project"]["requires-python"] == ">=3.12"


def test_cuda_optional_dependency_group_lists_expected_packages() -> None:
    pyproject = _load_pyproject()
    cuda_deps = pyproject["project"]["optional-dependencies"]["cuda"]
    joined = " ".join(cuda_deps)
    assert "nvidia-cublas-cu12" in joined
    assert "nvidia-cudnn-cu12" in joined


def test_gpu_marker_registered_and_excluded_by_default_addopts() -> None:
    pyproject = _load_pyproject()
    pytest_ini = pyproject["tool"]["pytest"]["ini_options"]
    markers = pytest_ini["markers"]
    assert any(marker.startswith("gpu:") for marker in markers)
    assert "not gpu" in pytest_ini["addopts"]


def test_notice_file_exists_and_attributes_vexa() -> None:
    assert NOTICE_PATH.is_file()
    content = NOTICE_PATH.read_text(encoding="utf-8")
    assert content.strip() != ""
    assert "Vexa" in content
    assert "Apache License, Version 2.0" in content
