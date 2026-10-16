"""`hardware.py`: the accelerator probes, entirely off the test runner's
real hardware -- every probe is a seam the tests pin."""

from __future__ import annotations

import pytest

from transcription import hardware
from transcription.hardware import PciDevice

# The suite-wide fixture (conftest) pins `intel_devices` to a lambda; these
# tests put the real, cached probe back and clear it.
_REAL_INTEL_DEVICES = hardware.intel_devices


@pytest.fixture(autouse=True)
def _real_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware, "intel_devices", _REAL_INTEL_DEVICES)
    _REAL_INTEL_DEVICES.cache_clear()


def test_classify_reads_the_intel_npu_and_gpu_classes() -> None:
    devices = [
        PciDevice("VEN_8086", "NeuralProcessors", "Intel(R) AI Boost"),
        PciDevice("VEN_8086", "Display", "Intel(R) Arc(TM) 140V GPU"),
        PciDevice("VEN_10DE", "Display", "NVIDIA GeForce RTX 4070"),
    ]
    assert hardware.classify_pci_devices(devices) == (True, True)


def test_classify_accepts_the_older_compute_accelerator_class() -> None:
    devices = [PciDevice("ven_8086", "computeaccelerator", "Intel(R) AI Boost")]
    assert hardware.classify_pci_devices(devices) == (True, False)


def test_classify_ignores_other_vendors_and_classes() -> None:
    devices = [
        PciDevice("VEN_10DE", "Display", "NVIDIA GeForce RTX 4070"),
        PciDevice("VEN_8086", "Net", "Intel(R) Wi-Fi 6E"),
        PciDevice("VEN_8086", "", "unknown class"),
    ]
    assert hardware.classify_pci_devices(devices) == (False, False)
    assert hardware.classify_pci_devices([]) == (False, False)


def test_intel_devices_is_off_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware.sys, "platform", "darwin")
    monkeypatch.setattr(
        hardware, "_pci_devices", lambda: (_ for _ in ()).throw(AssertionError("ran"))
    )
    assert hardware.intel_devices() == (False, False)


def test_intel_devices_walks_the_registry_once_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware.sys, "platform", "win32")
    calls: list[int] = []

    def fake_devices() -> list[PciDevice]:
        calls.append(1)
        return [
            PciDevice("VEN_8086", "NeuralProcessors", "Intel(R) AI Boost"),
            PciDevice("VEN_8086", "Display", "Intel(R) Graphics"),
        ]

    monkeypatch.setattr(hardware, "_pci_devices", fake_devices)
    assert hardware.intel_devices() == (True, True)
    assert hardware.intel_devices() == (True, True)
    assert len(calls) == 1


def test_a_failing_probe_means_no_intel_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware.sys, "platform", "win32")

    def boom() -> list[PciDevice]:
        raise OSError("registry unreadable")

    monkeypatch.setattr(hardware, "_pci_devices", boom)
    assert hardware.intel_devices() == (False, False)


@pytest.mark.skipif(hardware.sys.platform != "win32", reason="registry walk is Windows-only")
def test_the_registry_walk_runs_on_this_machine() -> None:
    """The one probe that touches the runner: it must enumerate without
    raising and classify to booleans (whatever this machine has)."""
    devices = hardware._pci_devices()
    assert all(isinstance(device, PciDevice) for device in devices)
    npu, gpu = hardware.classify_pci_devices(devices)
    assert isinstance(npu, bool) and isinstance(gpu, bool)


def test_detect_collects_every_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware, "nvidia_gpu_present", lambda: False)
    monkeypatch.setattr(hardware, "apple_silicon_present", lambda: True)
    monkeypatch.setattr(hardware, "intel_devices", lambda: (False, False))
    monkeypatch.setattr(hardware, "physical_memory_bytes", lambda: 16 * 1024**3)

    accel = hardware.detect()

    assert accel == hardware.Accelerators(
        nvidia=False,
        apple_silicon=True,
        intel_npu=False,
        intel_gpu=False,
        physical_memory_bytes=16 * 1024**3,
    )
    assert accel.as_dict()["apple_silicon"] is True


def test_detect_takes_the_callers_nvidia_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """`model_routes._nvidia_gpu_present` is what the download tests pin;
    `detect(nvidia=...)` keeps one answer per decision."""
    monkeypatch.setattr(hardware, "nvidia_gpu_present", lambda: False)
    monkeypatch.setattr(hardware, "apple_silicon_present", lambda: False)
    monkeypatch.setattr(hardware, "intel_devices", lambda: (False, False))
    monkeypatch.setattr(hardware, "physical_memory_bytes", lambda: None)

    assert hardware.detect(nvidia=True).nvidia is True
    assert hardware.detect().nvidia is False


def test_physical_memory_is_a_positive_number_or_unknown() -> None:
    memory = hardware.physical_memory_bytes()
    assert memory is None or memory > 0
