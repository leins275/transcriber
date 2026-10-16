"""Which accelerators this machine has -- the one place that asks.

Every engine choice the service makes (which whisper engine, whether the
LLM offloads, which torch build the speaker runtime wants, which video
encoder goes first) starts from the same four facts about the machine:
an NVIDIA GPU, an Apple Silicon SoC, an Intel NPU, an Intel GPU. They are
cheap to find out and never change while the process runs, so the probes
are collected once into :class:`Accelerators` and read everywhere else.

Each probe is a module-level function so tests monkeypatch it the way
`local_whisper._cuda_device_count` is patched -- nothing here ever runs on
the test runner's real hardware. The Windows registry walk (the only probe
that costs more than a `PATH` lookup) is cached for the process.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("transcription")


@dataclass(frozen=True)
class Accelerators:
    """The machine's accelerators, as far as the service cares."""

    nvidia: bool
    apple_silicon: bool
    intel_npu: bool
    intel_gpu: bool
    # `None` when the probe failed; consumers treat that as "unknown" and
    # make the conservative choice.
    physical_memory_bytes: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def nvidia_gpu_present() -> bool:
    """`nvidia-smi` ships with every NVIDIA display driver (not with CUDA,
    not with ctranslate2), so its presence on `PATH` is a cheap, honest
    "is there an NVIDIA GPU at all" signal that needs no subprocess."""
    return shutil.which("nvidia-smi") is not None


def apple_silicon_present() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


# The Intel questions are answered from the registry's PnP enumeration
# (`HKLM\SYSTEM\CurrentControlSet\Enum\PCI`, readable by every user):
# each device instance carries its `ClassGUID` and vendor id, and the
# `Control\Class` tree names the class behind each GUID. The NPU is a
# device of class `NeuralProcessors` (Device Manager's "Neural
# processors"; older drivers filed it under `ComputeAccelerator`), the GPU
# one of class `Display`. No subprocess, no PowerShell, no WMI.
_INTEL_VENDOR = "VEN_8086"
_NPU_CLASSES = frozenset({"neuralprocessors", "computeaccelerator"})
_DISPLAY_CLASS = "display"
_PCI_ENUM_KEY = r"SYSTEM\CurrentControlSet\Enum\PCI"
_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class"


@dataclass(frozen=True)
class PciDevice:
    vendor: str
    device_class: str
    description: str


def _registry_subkeys(key: Any) -> list[str]:
    import winreg  # noqa: PLC0415 - Windows only

    names: list[str] = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            return names
        index += 1


def _registry_value(key: Any, name: str) -> Any:
    import winreg  # noqa: PLC0415 - Windows only

    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _pci_devices() -> list[PciDevice]:
    """Every PCI device instance the registry enumerates; the seam tests
    replace."""
    import winreg  # noqa: PLC0415 - Windows only

    class_names: dict[str, str] = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CLASS_KEY) as classes:
        for guid in _registry_subkeys(classes):
            try:
                with winreg.OpenKey(classes, guid) as class_key:
                    name = _registry_value(class_key, "Class")
            except OSError:
                continue
            if isinstance(name, str):
                class_names[guid.lower()] = name
    devices: list[PciDevice] = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PCI_ENUM_KEY) as pci:
        for device in _registry_subkeys(pci):
            vendor = device.split("&", 1)[0]
            try:
                with winreg.OpenKey(pci, device) as device_key:
                    instances = _registry_subkeys(device_key)
                    for instance in instances:
                        with winreg.OpenKey(device_key, instance) as instance_key:
                            guid = _registry_value(instance_key, "ClassGUID")
                            desc = _registry_value(instance_key, "DeviceDesc")
            except OSError:
                continue
            else:
                if not isinstance(guid, str):
                    continue
                devices.append(
                    PciDevice(
                        vendor=vendor,
                        device_class=class_names.get(guid.lower(), ""),
                        description=str(desc or "").rsplit(";", 1)[-1],
                    )
                )
    return devices


def classify_pci_devices(devices: list[PciDevice]) -> tuple[bool, bool]:
    """``(intel_npu, intel_gpu)`` from the enumeration."""
    intel = [d for d in devices if d.vendor.upper() == _INTEL_VENDOR]
    intel_npu = any(d.device_class.lower() in _NPU_CLASSES for d in intel)
    intel_gpu = any(d.device_class.lower() == _DISPLAY_CLASS for d in intel)
    return intel_npu, intel_gpu


@functools.cache
def intel_devices() -> tuple[bool, bool]:
    """``(intel_npu, intel_gpu)``: Windows only (the NPU driver exists for
    nothing else), cached because the registry walk visits every PCI
    device."""
    if sys.platform != "win32":
        return False, False
    try:
        return classify_pci_devices(_pci_devices())
    except Exception as exc:  # noqa: BLE001 - "could not ask" means "none found"
        logger.info("intel accelerator probe failed: %s", exc)
        return False, False


def physical_memory_bytes() -> int | None:
    """Installed RAM -- on Apple Silicon that is also the GPU's memory."""
    try:
        if sys.platform == "win32":
            return _windows_physical_memory()
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages) * int(page_size)
    except Exception:  # noqa: BLE001 - unknown beats a crash on an odd host
        return None


def _windows_physical_memory() -> int | None:
    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009 - absent off Windows
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys)


def detect(*, nvidia: bool | None = None) -> Accelerators:
    """The machine's accelerators.

    ``nvidia`` lets a caller that already answered the NVIDIA question its
    own way (`api.model_routes._nvidia_gpu_present`, which tests pin) keep
    one answer per decision.
    """
    intel_npu, intel_gpu = intel_devices()
    return Accelerators(
        nvidia=nvidia_gpu_present() if nvidia is None else nvidia,
        apple_silicon=apple_silicon_present(),
        intel_npu=intel_npu,
        intel_gpu=intel_gpu,
        physical_memory_bytes=physical_memory_bytes(),
    )
