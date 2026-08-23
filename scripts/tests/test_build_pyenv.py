"""Tests for `scripts/build_pyenv.py` (T4, FR-8, NFR-1, R2).

The full bake is a real `uv`-driven build: it downloads a managed CPython,
installs services/transcription's frozen dependency set with `uv pip install
--target`, and copies the F2 source tree. That is several minutes and a few
hundred MB even without CUDA wheels, so it lives behind one `session`-scoped
fixture, marked `slow`, and every test in this module is skipped outright
when `uv` is not on PATH (R6/NFR-6 detection pattern).

Per this feature's spec gate (NFR-1 revised for GPU-first CUDA inference),
the release bake additionally passes `--extra cuda` so the tree also carries
`nvidia-cublas-cu12`/`nvidia-cudnn-cu12` -- but those wheels alone are
roughly 1 GB and slow to fetch, which is disproportionate for this module's
fast local test loop (T8 owns the full-size gate on the real release bake).
`test_cuda_extra_is_wired_into_the_export` below proves the *mechanism* --
that requesting the `cuda` extra changes what `uv export` resolves --
without installing those wheels; `pyenv-manifest.json`'s `extras` field
records whichever extras a given invocation used, so a release build's
manifest is auditable.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_pyenv  # noqa: E402

pytestmark = [
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH"),
    pytest.mark.slow,
]


@pytest.fixture(scope="session")
def baked_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real, non-CUDA bake shared by every test in this module."""
    out_dir = tmp_path_factory.mktemp("pyenv-bake") / "pyenv"
    build_pyenv.bake(out_dir)
    return out_dir


def _site_packages_names(baked_tree: Path) -> set[str]:
    site_packages = baked_tree / "site-packages"
    return {p.name for p in site_packages.iterdir()}


def test_tree_contains_bundled_python_executable(baked_tree: Path) -> None:
    assert (baked_tree / "python" / "python.exe").is_file()


def test_site_packages_holds_the_core_transcription_dependencies(baked_tree: Path) -> None:
    names = _site_packages_names(baked_tree)
    assert "faster_whisper" in names
    assert "ctranslate2" in names
    assert "huggingface_hub" in names
    # The LLM feature's hard deps: the built-in llama.cpp runtime (CPU wheel
    # from the project's own index) and the markdown -> PDF stack.
    assert "llama_cpp" in names
    assert "markdown" in names
    assert "xhtml2pdf" in names


def test_no_pytorch_anywhere_in_the_tree(baked_tree: Path) -> None:
    forbidden = {"torch", "torchaudio", "nvidia_cudnn_cu11"}
    hit_names = set()
    for path in baked_tree.rglob("*"):
        if path.name.lower().replace("-", "_").split(".")[0] in forbidden:
            hit_names.add(path.name)
    assert not hit_names, f"found forbidden PyTorch-related paths: {hit_names}"


def test_relocated_tree_runs_help_from_an_arbitrary_path(
    baked_tree: Path, tmp_path: Path
) -> None:
    # A single extra path segment is enough to prove relocation to a
    # different absolute path (R2); nesting it deeper than that risks
    # tripping Windows' 260-char MAX_PATH purely as a pytest tmp_path
    # artifact -- the real install path is comfortably under the limit
    # (~210 chars for this tree's deepest file, computed separately).
    relocated = tmp_path / "moved" / "pyenv"
    shutil.copytree(baked_tree, relocated)

    proc = subprocess.run(
        [str(relocated / "python" / "python.exe"), "-m", "transcription", "--help"],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "transcription" in proc.stdout.lower()


def test_relocated_tree_has_no_build_time_absolute_path_leak(
    baked_tree: Path, tmp_path: Path
) -> None:
    relocated = tmp_path / "moved" / "pyenv"
    shutil.copytree(baked_tree, relocated)

    needle = str(baked_tree).encode("ascii", errors="ignore")
    offenders = []
    for path in relocated.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle in data:
            offenders.append(path)
    assert not offenders, f"found the pre-relocation build path baked into: {offenders}"


def test_manifest_lists_no_pytorch_and_records_total_size(baked_tree: Path) -> None:
    manifest = build_pyenv.read_manifest(baked_tree)
    assert "torch" not in manifest["packages"]
    assert "torchaudio" not in manifest["packages"]
    assert manifest["total_bytes"] > 0


def test_bake_fails_loudly_when_the_lock_is_stale(tmp_path: Path) -> None:
    stale_service_dir = tmp_path / "service"
    shutil.copytree(
        build_pyenv.SERVICE_DIR,
        stale_service_dir,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", ".git"),
    )
    pyproject = stale_service_dir / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'faster-whisper>=1.2,<2', "faster-whisper>=999"
        ),
        encoding="utf-8",
    )

    with pytest.raises(build_pyenv.BuildPyenvError):
        build_pyenv.bake(tmp_path / "out", service_dir=stale_service_dir)


def test_cuda_extra_is_wired_into_the_export(tmp_path: Path) -> None:
    """Proves the release-build mechanism without installing the ~1GB of wheels."""
    requirements_file = tmp_path / "requirements-cuda.txt"
    build_pyenv.export_requirements(
        build_pyenv.SERVICE_DIR,
        build_pyenv.find_uv(),
        ["cuda"],
        requirements_file,
    )
    text = requirements_file.read_text(encoding="utf-8")
    assert "nvidia-cublas-cu12" in text
    assert "nvidia-cudnn-cu12" in text


def test_bake_is_idempotent(tmp_path_factory: pytest.TempPathFactory) -> None:
    out_dir = tmp_path_factory.mktemp("pyenv-idempotent") / "pyenv"
    manifest_1 = build_pyenv.bake(out_dir)
    manifest_2 = build_pyenv.bake(out_dir)
    assert manifest_1["packages"] == manifest_2["packages"]
    assert (out_dir / "python" / "python.exe").is_file()
