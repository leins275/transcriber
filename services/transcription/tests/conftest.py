"""Universal pytest fixtures shared by the whole suite.

This file is created once (T1 of the plan) and never edited again -- every
later task adds fixtures to its own test module, or imports
``tests/fakes.py`` (owned by T8), so parallel agents never collide here.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_app_dir(tmp_path: Path) -> Path:
    """A fresh app folder with the subdirectories the service expects."""
    (tmp_path / "models").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _plain_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every hardware probe to a machine with no accelerator, so no test
    ever runs the PnP query or depends on the runner's GPU (test_hardware
    overrides what it exercises)."""
    from transcription import hardware

    monkeypatch.setattr(hardware, "nvidia_gpu_present", lambda: False)
    monkeypatch.setattr(hardware, "apple_silicon_present", lambda: False)
    monkeypatch.setattr(hardware, "intel_devices", lambda: (False, False))
    monkeypatch.setattr(hardware, "physical_memory_bytes", lambda: 32 * 1024**3)
