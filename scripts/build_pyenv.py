#!/usr/bin/env python
"""Bake a relocatable Python runtime + frozen dependency tree (FR-8, NFR-1, R2).

`make installer` (T8) calls this as one stage of the release pipeline. It
produces, under `--out` (default `build/pyenv/`):

    python/             a copy of a uv-managed CPython 3.12 standalone build
    site-packages/       services/transcription's frozen dependencies,
                         installed with `uv pip install --target` (never a
                         venv -- see below)
    service/             a copy of the services/transcription source tree
    pyenv-manifest.json  python version, resolved package versions, extras
                         used, and total tree size in bytes (consumed by T8)

No venv is created (Q2-A / R2's stated failure mode). A venv's
`pyvenv.cfg` records an absolute `home = ...` path back to the interpreter
that created it, and any console-script shims it generates embed an
absolute shebang -- both break the moment the tree is copied to its final
install directory. Instead:

  1. `uv python install <version>` ensures a uv-managed, relocatable
     python-build-standalone CPython is available, and its install
     directory is *copied* (not linked) into `<out>/python/`.
  2. `uv export --frozen --no-dev --no-emit-project` reads services/
     transcription's committed `uv.lock` -- never re-resolving it -- to
     produce a pinned, hashed `requirements.txt`. Before that, `uv lock
     --check --offline` asserts the lock is not stale relative to
     pyproject.toml (FR-4): a lock that needs updating fails the bake
     loudly rather than silently drifting from what CI/dev tested.
  3. `uv pip install --python <out>/python/python.exe --target
     <out>/site-packages --no-deps -r requirements.txt` installs the
     pinned wheels *beside* the interpreter, not inside a venv.
  4. services/transcription's source tree is copied into `<out>/service/`.
  5. One `.pth` file is dropped into `<out>/python/Lib/site-packages/`
     containing *relative* paths to `site-packages/` and `service/src/`.
     CPython's `site` module resolves `.pth` entries relative to the
     directory that contains the `.pth` file (see `site.addsitedir` /
     `site.addpackage`), so those two directories land on `sys.path` no
     matter where the whole tree is later moved -- which is exactly what
     makes `<out>/python/python.exe -m transcription ...` work after an
     arbitrary relocation, the property this task's test proves.

CUDA runtime wheels (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, needed by
CTranslate2 for GPU inference per the spec gate's GPU-first decision) are
opt-in via `--extra cuda`, since they add roughly 700 MB-1 GB and blow past
what the vendored, 32-bit `makensis.exe` can compile (~2.33 GiB
uncompressed with them baked in -- see `docs/verification-installer.md`'s
"Blocker 1"). The release build (`scripts/build_installer.py`) does **not**
pass `--extra cuda` by default: the CUDA runtime is instead fetched at
first run into the application folder (`transcription.cuda_runtime`).
`--extra cuda` remains available here for a dev/CI build that wants it
baked in anyway (e.g. reproducing the pre-fix payload for size comparison);
this script's own local dev loop and test suite default to no extras.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = REPO_ROOT / "services" / "transcription"
DEFAULT_OUT = REPO_ROOT / "build" / "pyenv"
PYTHON_VERSION = "3.12"
MANIFEST_NAME = "pyenv-manifest.json"

IS_WINDOWS = sys.platform == "win32"


def python_install_root(python_exe: Path) -> Path:
    """The python-build-standalone install root for a managed interpreter.

    On Windows `python.exe` sits at the install root itself; on macOS/Linux
    `uv python find` returns `<root>/bin/python3`, so the root is two levels
    up.
    """
    return python_exe.parent if IS_WINDOWS else python_exe.parent.parent


def baked_python_exe_path(python_dir: Path) -> Path:
    """Where the interpreter lives inside the baked `<out>/python/` copy."""
    if IS_WINDOWS:
        return python_dir / "python.exe"
    return python_dir / "bin" / "python3"

# Directories/files never worth copying into the baked service tree: dev-only
# caches and a from-scratch venv, none of which the runtime needs.
_SERVICE_EXCLUDE = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
}

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+-]*)")


class BuildPyenvError(RuntimeError):
    """Raised when a bake step fails; the message is the operator-facing diagnostic."""


def _run(cmd: Sequence[str], *, cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BuildPyenvError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc.stdout


def find_uv(uv_exe: str | None = None) -> str:
    """Resolve the `uv` executable to drive every step below."""
    candidate = uv_exe or os.environ.get("UV_EXE") or shutil.which("uv")
    if candidate is None:
        raise BuildPyenvError("uv executable not found: pass --uv, set UV_EXE, or put uv on PATH")
    return candidate


def check_lock_fresh(service_dir: Path, uv_exe: str) -> None:
    """Fail loudly if uv.lock does not satisfy pyproject.toml (FR-4).

    `uv export --frozen` never checks this itself -- it trusts uv.lock as
    committed, by design, so a drifted lock would otherwise bake silently.
    `uv lock --check` is the actual staleness assertion (`--offline` keeps
    it fast and network-independent for a lock that is genuinely in sync).
    """
    proc = subprocess.run(
        [uv_exe, "lock", "--check", "--offline"],
        cwd=service_dir,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise BuildPyenvError(
            f"{service_dir}/uv.lock is stale relative to pyproject.toml; "
            f"run `uv lock` inside {service_dir} to refresh it.\n"
            f"{proc.stdout}\n{proc.stderr}"
        )


def locate_managed_python(uv_exe: str, version: str) -> Path:
    """Ensure a uv-managed CPython is installed and return its python.exe."""
    _run([uv_exe, "python", "install", version])
    out = _run([uv_exe, "python", "find", version, "--managed-python"])
    python_exe = Path(out.strip())
    if not python_exe.is_file():
        raise BuildPyenvError(f"uv reported a managed python at {python_exe} but it does not exist")
    return python_exe


def export_requirements(
    service_dir: Path, uv_exe: str, extras: Sequence[str], out_file: Path
) -> None:
    """Freeze services/transcription's locked dependencies to a requirements file."""
    cmd = [
        uv_exe,
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--format",
        "requirements-txt",
        "-o",
        str(out_file),
    ]
    for extra in extras:
        cmd += ["--extra", extra]
    _run(cmd, cwd=service_dir)


# llama-cpp-python ships no Windows wheel on PyPI (sdist only, needs
# CMake+MSVC); pyproject.toml pins it to the project's own CPU wheel index
# via [tool.uv.sources]. `uv export` flattens that back to a bare
# `name==version` line, so the install step below must be told about the
# index again or it would fall back to PyPI's sdist and try to compile.
LLAMA_CPP_CPU_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cpu"

# Extras every bake includes on top of whatever the caller asks for: the
# installer always ships the CPU llama.cpp runtime (the `llm-cuda` variant
# is a dev-machine convenience the NSIS compiler could not swallow anyway --
# the same reason the CUDA STT runtime is fetched at first run instead of
# baked).
BASE_EXTRAS: tuple[str, ...] = ("llm-cpu",)


def install_dependencies(
    uv_exe: str, python_exe: Path, requirements_file: Path, target_dir: Path
) -> None:
    """`uv pip install --target`, never `uv sync` -- no venv is created here."""
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        uv_exe,
        "pip",
        "install",
        "--python",
        str(python_exe),
        "--target",
        str(target_dir),
        "--no-deps",
    ]
    if IS_WINDOWS:
        cmd += ["--extra-index-url", LLAMA_CPP_CPU_INDEX]
    else:
        # No abetlen index on macOS, deliberately: its macOS arm64 wheels are
        # malformed zips (bad member sizes / trailing bytes after the
        # end-of-central-directory record — verified against both the
        # /whl/cpu and /whl/metal 0.3.34+0.3.35 assets), which uv's strict
        # archive parser refuses — and uv's first-match index strategy would
        # never fall past that index to PyPI. Build the pinned version from
        # PyPI's sdist instead (needs cmake + clang, both present on the
        # macOS runners; ~1 min on an M4). llama.cpp enables Metal by default
        # on Apple Silicon, so this is also what gives the M-series GPU path.
        # `--no-binary` guards the intent even if a (possibly still broken)
        # macOS wheel ever appears on PyPI.
        cmd += ["--no-binary", "llama-cpp-python"]
    cmd += ["-r", str(requirements_file)]
    _run(cmd)
    strip_console_script_launchers(target_dir)


def strip_console_script_launchers(site_packages_dir: Path) -> None:
    """Remove `--target`'s generated console-script launcher executables.

    `uv pip install --target` (like pip) still writes a `bin/` of
    distlib-style launcher `.exe` files for every `[project.scripts]` entry
    point in the resolved set (e.g. `litellm.exe`, `uvicorn.exe`), even
    though `--target` is not a venv. Those launchers embed the *absolute*
    path of the interpreter used at install time -- exactly R2's "console
    scripts carry absolute paths" failure mode, just surfacing here instead
    of in a venv's `Scripts/`. Nothing in this project calls them: the
    service is always launched as `python -m transcription`, so they are
    both a relocatability hazard and dead weight -- delete the directory.
    """
    bin_dir = site_packages_dir / "bin"
    if bin_dir.is_dir():
        shutil.rmtree(bin_dir)


def _ignore_service_paths(_dir_path: str, names: list[str]) -> list[str]:
    return [n for n in names if n in _SERVICE_EXCLUDE or n.endswith(".egg-info")]


def copy_service_tree(service_dir: Path, out_dir: Path) -> None:
    """Copy the F2 source tree (minus dev-only caches/venv) into `out_dir`."""
    shutil.copytree(service_dir, out_dir, ignore=_ignore_service_paths, dirs_exist_ok=True)


def write_relocatable_pth(python_dir: Path, site_packages_dir: Path, service_src_dir: Path) -> Path:
    """Point the baked interpreter at the external site-packages and service
    source directories using paths relative to the .pth file's own location,
    so the whole tree keeps working after being copied anywhere else."""
    if IS_WINDOWS:
        pth_dir = python_dir / "Lib" / "site-packages"
    else:
        # python-build-standalone's Unix layout: `lib/python3.X/site-packages`.
        # Globbed rather than derived from PYTHON_VERSION so the resolved
        # minor version can never drift from the directory actually copied.
        pth_dir = next((python_dir / "lib").glob("python3.*")) / "site-packages"
    pth_dir.mkdir(parents=True, exist_ok=True)
    pth_file = pth_dir / "_transcriber_pyenv.pth"
    lines = [
        os.path.relpath(site_packages_dir, pth_dir),
        os.path.relpath(service_src_dir, pth_dir),
    ]
    pth_file.write_text("\n".join(lines) + "\n", encoding="ascii")
    return pth_file


def compute_tree_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def read_locked_versions(requirements_file: Path) -> dict[str, str]:
    """Parse `name==version` pins out of a `uv export` requirements file."""
    versions: dict[str, str] = {}
    for line in requirements_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN_RE.match(stripped)
        if match:
            versions[match.group(1)] = match.group(2)
    return versions


def python_runtime_version(python_exe: Path) -> str:
    out = _run([str(python_exe), "--version"])
    return out.strip().removeprefix("Python").strip()


def write_manifest(
    out_dir: Path, python_version: str, extras: Sequence[str], packages: dict[str, str]
) -> dict:
    manifest = {
        "python_version": python_version,
        "extras": list(extras),
        "packages": dict(sorted(packages.items())),
        "total_bytes": compute_tree_size(out_dir),
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_manifest(out_dir: Path) -> dict:
    return json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


def bake(
    out_dir: Path,
    *,
    extras: Sequence[str] = (),
    uv_exe: str | None = None,
    python_version: str = PYTHON_VERSION,
    service_dir: Path = SERVICE_DIR,
) -> dict:
    """Run every step above in order and return the written manifest.

    Idempotent and non-interactive: any prior `out_dir` is replaced wholesale
    so re-running always yields the tree this invocation's inputs describe,
    never a stale mix of a previous run's outputs.
    """
    uv = find_uv(uv_exe)
    check_lock_fresh(service_dir, uv)

    # The always-baked extras lead; caller extras follow, deduplicated.
    extras = tuple(BASE_EXTRAS) + tuple(e for e in extras if e not in BASE_EXTRAS)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    managed_python_exe = locate_managed_python(uv, python_version)
    python_dir = out_dir / "python"
    shutil.copytree(python_install_root(managed_python_exe), python_dir)
    baked_python_exe = baked_python_exe_path(python_dir)

    # requirements.txt is a build-time intermediate only: uv's own export
    # header comment embeds the absolute path it was written to (`-o ...`),
    # which would otherwise be the one build-time absolute path left baked
    # into the relocatable tree (R2). Write it *outside* out_dir so it never
    # ships.
    with tempfile.TemporaryDirectory() as scratch:
        requirements_file = Path(scratch) / "requirements.txt"
        export_requirements(service_dir, uv, extras, requirements_file)

        site_packages_dir = out_dir / "site-packages"
        install_dependencies(uv, baked_python_exe, requirements_file, site_packages_dir)

        packages = read_locked_versions(requirements_file)

    service_out_dir = out_dir / "service"
    copy_service_tree(service_dir, service_out_dir)

    write_relocatable_pth(python_dir, site_packages_dir, service_out_dir / "src")

    resolved_version = python_runtime_version(baked_python_exe)
    return write_manifest(out_dir, resolved_version, extras, packages)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="bake output directory")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        dest="extras",
        help="uv optional-dependency-group to include (repeatable), e.g. --extra cuda",
    )
    parser.add_argument("--python-version", default=PYTHON_VERSION)
    parser.add_argument("--uv", default=None, help="path to the uv executable")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        manifest = bake(
            args.out,
            extras=args.extras,
            uv_exe=args.uv,
            python_version=args.python_version,
        )
    except BuildPyenvError as exc:
        print(f"build_pyenv: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
