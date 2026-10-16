"""`build_pyenv.platform_extras`: what each platform's bake always carries
beyond the base extras. Pure, no uv, no bake."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_pyenv  # noqa: E402


def test_the_mac_bake_carries_the_apple_engines() -> None:
    assert build_pyenv.platform_extras("darwin") == ("whisper-mlx", "diarization")


def test_windows_and_linux_bakes_add_nothing() -> None:
    """Windows keeps every accelerated payload as a first-run fetch (the
    32-bit NSIS limit); Linux is not a shipped target."""
    assert build_pyenv.platform_extras("win32") == ()
    assert build_pyenv.platform_extras("linux") == ()


def test_the_base_extras_are_unchanged() -> None:
    assert build_pyenv.BASE_EXTRAS == ("llm-cpu",)
