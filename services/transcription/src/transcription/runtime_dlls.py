"""CUDA DLL directory registration for pip-installed nvidia wheels (R3).

pip/uv-installed `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels place
their DLLs under ``site-packages/nvidia/<component>/bin``, a location
Windows' DLL search path does not include by default (unlike a directory
next to the importing module or on ``PATH``). CTranslate2 (the local
provider's native backend) loads those DLLs via the OS loader, not via a
Python import, so without an explicit registration CUDA silently fails to
initialize and the service falls back to CPU even with a CUDA GPU present.

:func:`register_cuda_dll_dirs` must run once at process startup -- before
the local provider seam's first native-backend import, i.e. before the
first ``get_provider("local", ...)`` call -- so the search path is already in
place when the model actually loads. It is a deliberate no-op, and never
raises, on a machine with no ``nvidia`` wheels installed at all (CPU-only
fallback is best-effort, not a broken import -- see the spec's "Out of
scope: CPU-only optimization").

T14's real-machine verification (`docs/verification-installer.md`,
"Blocker 3") found ``os.add_dll_directory`` alone insufficient: CTranslate2
loads cuBLAS/cuDNN internally at model *construction* time, not through a
Python import ``add_dll_directory``-registered directories intercept the
way ``ctypes.WinDLL`` and Python's own import machinery do -- a real
``ctranslate2.models.Whisper(...)`` construction still failed to find
``cublas64_12.dll`` even with every directory registered via
``add_dll_directory``, and only succeeded once the same directories were
also prepended onto the process's ``PATH`` environment variable. Both are
now done, in that order, so any loader (Python import, ``ctypes``,
CTranslate2's own internal loading) finds the DLLs regardless of which
search mechanism it consults.

Also scans an app-folder ``runtime/nvidia`` directory (Defect 1's fix,
`docs/verification-installer.md` "Blocker 1"): the installer no longer
bakes ``--extra cuda``'s wheels into the pyenv payload, so
``transcription.cuda_runtime`` extracts them into the application folder
at first run instead, outside the interpreter's own ``site-packages`` and
so never found by the ``sys.path`` scan below on its own.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_LOGGER_NAME = "transcription"

# The subdirectory `transcription.cuda_runtime.CudaRuntimeDownload` extracts
# the first-run-downloaded CUDA wheels' `nvidia/` tree into, relative to the
# application folder -- kept as a plain string constant here (rather than an
# import from `cuda_runtime`) so this module never depends on that one
# either, keeping this shim's own import graph minimal.
_APP_FOLDER_RUNTIME_SUBDIR = ("runtime", "nvidia")


def _nvidia_package_dir() -> Path | None:
    """Locate the installed ``nvidia`` namespace package directory, if any.

    Tries a real import first (the common case); falls back to scanning
    ``sys.path`` for a `nvidia/` directory, since namespace packages
    sometimes have no single, reliable ``__file__``.
    """
    try:
        import nvidia  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        nvidia_file = getattr(nvidia, "__file__", None)
        if nvidia_file:
            return Path(nvidia_file).resolve().parent

    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry) / "nvidia"
        if candidate.is_dir():
            return candidate

    return None


def _app_folder_nvidia_dir() -> Path | None:
    """The app-folder ``runtime/nvidia`` directory Defect 1's first-run CUDA
    download extracts into, if present.

    Resolves the application folder the same way ``config._resolve_app_dir``
    does (``TRANSCRIBER_APP_DIR`` env var, else the running interpreter's
    grandparent directory) rather than importing ``transcription.config``
    itself -- this shim must run at process startup, before configuration
    has necessarily been loaded (`cli.py main()` calls it before
    `load_config`).
    """
    raw = os.environ.get("TRANSCRIBER_APP_DIR")
    app_dir = Path(raw) if raw else Path(sys.executable).resolve().parent.parent
    candidate = app_dir.joinpath(*_APP_FOLDER_RUNTIME_SUBDIR)
    return candidate if candidate.is_dir() else None


def _iter_bin_dirs(nvidia_dir: Path) -> list[Path]:
    return [
        component_dir / "bin"
        for component_dir in sorted(nvidia_dir.iterdir())
        if (component_dir / "bin").is_dir()
    ]


def register_cuda_dll_dirs() -> list[str]:
    """Register every existing ``nvidia/*/bin`` directory with the OS loader,
    via both ``os.add_dll_directory`` and the process's own ``PATH``.

    Returns the sorted list of directories actually registered, so the
    caller can log them (Done-when: "the shim's log line must name the
    registered directories"). Idempotent: safe to call repeatedly, always
    returns the current set of existing directories. Never raises -- a
    missing ``nvidia`` tree, a component with no ``bin/`` subdirectory, or
    even a failing ``os.add_dll_directory`` call are all handled and simply
    excluded from the result rather than propagated.

    Scans both the interpreter's own ``site-packages/nvidia`` (a baked
    ``--extra cuda`` pyenv, still used by local dev/CI builds) and the
    app-folder ``runtime/nvidia`` directory Defect 1's first-run download
    populates -- whichever exist, in that order, deduplicated.
    """
    logger = logging.getLogger(_LOGGER_NAME)

    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        # Not on Windows -- nothing to register, never an error.
        return []

    bin_dirs: list[Path] = []
    seen: set[str] = set()
    for nvidia_dir in (_nvidia_package_dir(), _app_folder_nvidia_dir()):
        if nvidia_dir is None or not nvidia_dir.is_dir():
            continue
        for bin_dir in _iter_bin_dirs(nvidia_dir):
            key = os.path.normcase(str(bin_dir))
            if key in seen:
                continue
            seen.add(key)
            bin_dirs.append(bin_dir)

    registered: list[str] = []
    for bin_dir in bin_dirs:
        try:
            add_dll_directory(str(bin_dir))
        except OSError:
            logger.warning("failed to register CUDA DLL directory", extra={"path": str(bin_dir)})
            continue

        registered.append(str(bin_dir))

    if registered:
        # `os.add_dll_directory` alone is not enough for CTranslate2's own
        # internal cuBLAS/cuDNN loading (see this module's docstring,
        # Defect 3) -- prepend the same directories onto the process's own
        # `PATH` too, in addition to (never instead of) the registration
        # above, before the first provider construction ever runs.
        current_path = os.environ.get("PATH", "")
        existing = {os.path.normcase(p) for p in current_path.split(os.pathsep) if p}
        prefix = [d for d in registered if os.path.normcase(d) not in existing]
        if prefix:
            os.environ["PATH"] = (
                os.pathsep.join([*prefix, current_path])
                if current_path
                else os.pathsep.join(prefix)
            )

        logger.info(
            "registered CUDA DLL search directories",
            extra={"directories": registered},
        )

    return registered
