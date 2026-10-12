#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["packaging>=24"]
# ///
"""Generate the pinned wheel manifest for the first-run diarization runtime.

The installer never bakes the `diarization` extra (pyannote + the torch
stack, gigabytes of GPU-specific payload); the service fetches it on the
operator's request into `<app_dir>/runtime/diarization/` instead, the same
way it fetches the CUDA STT runtime and the CUDA llama.cpp build. This
script derives the wheel list from `services/transcription/uv.lock` so the
runtime the app fetches is exactly the dependency set the lock resolved:

- every package the `diarization` extra adds on top of what the installer
  bakes (`build_pyenv.BASE_EXTRAS`), for CPython 3.12 on `win_amd64`;
- in two variants that differ only in the torch pair: `cuda` swaps `torch`
  and `torchaudio` for the CUDA builds PyTorch publishes on its own index
  (`CUDA_OVERRIDES` below), `cpu` keeps the PyPI builds the lock pins. The
  override must carry the *same* version so the rest of the closure stays
  valid; a drift fails this script loudly.

Output: `services/transcription/src/transcription/diarization_runtime_packages.py`
(committed; `--check` verifies it is up to date, which `make lint` runs).

Usage:
    uv run scripts/gen_diarization_runtime.py           # rewrite the module
    uv run scripts/gen_diarization_runtime.py --check   # verify, exit 1 on drift
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from packaging import tags
from packaging.markers import Marker
from packaging.utils import canonicalize_name, parse_wheel_filename

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = REPO_ROOT / "services" / "transcription" / "uv.lock"
OUTPUT_FILE = (
    REPO_ROOT
    / "services"
    / "transcription"
    / "src"
    / "transcription"
    / "diarization_runtime_packages.py"
)

PROJECT_NAME = "transcription"
# What the installer bakes (`scripts/build_pyenv.py::BASE_EXTRAS`); the
# diarization runtime is everything the extra adds on top of it.
BASE_EXTRAS: tuple[str, ...] = ("llm-cpu",)
RUNTIME_EXTRA = "diarization"

# The target interpreter/platform of the baked environment.
PYTHON_VERSION = (3, 12)
PLATFORM = "win_amd64"
MARKER_ENV: dict[str, str] = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.0",
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Windows",
    "platform_version": "",
    "python_full_version": "3.12.0",
    "python_version": "3.12",
    "sys_platform": "win32",
}


@dataclass(frozen=True)
class Wheel:
    name: str
    version: str
    filename: str
    url: str
    size: int
    sha256: str
    # For a source tarball: the in-archive directory holding the importable
    # tree, and (optionally) the one member to keep out of it. Empty for a
    # wheel (`cuda_runtime.CudaPackage.archive_root` / `extract_prefix`).
    archive_root: str = ""
    extract_prefix: str = ""


# Pure-Python packages PyPI ships only as source tarballs: the directory
# inside the tarball (after its `<name>-<version>/` top level) that holds
# the importable tree, plus a member filter. Anything else without a wheel
# is an error -- an sdist needing a build step cannot be fetched at first
# run.
SDIST_ROOTS: dict[str, tuple[str, str]] = {
    "antlr4-python3-runtime": ("src/", ""),
    "docopt": ("", "docopt.py"),
}


# The two runtime variants the service can fetch: `cuda` on a machine with
# an NVIDIA GPU, `cpu` otherwise. Only the torch pair differs; the service
# picks the variant at fetch time (`diarization_runtime.select_runtime_variant`).
RUNTIME_VARIANTS: tuple[str, ...] = ("cuda", "cpu")

# The CUDA builds of the two torch packages, from PyTorch's own wheel index.
# Same versions as `uv.lock` pins from PyPI (asserted below), only the build
# differs: `+cu126` bundles the CUDA 12.6 runtime inside `torch/lib`, which
# every driver from the 525 series up can run, on every GPU generation
# from Maxwell to Ada. Size and digest are what the index reports for these
# exact artifacts; the download verifies both.
TORCH_CUDA_VARIANT = "cu126"
CUDA_OVERRIDES: dict[str, Wheel] = {
    # 2.8 is the last torch/torchaudio pair with the `torchaudio.info` /
    # `AudioMetaData` / `list_audio_backends` API pyannote 3.x imports
    # (removed in 2.9; pyproject's `diarization` extra pins the same bound).
    "torch": Wheel(
        name="torch",
        version="2.8.0+cu126",
        filename="torch-2.8.0+cu126-cp312-cp312-win_amd64.whl",
        url=(
            "https://download.pytorch.org/whl/cu126/"
            "torch-2.8.0%2Bcu126-cp312-cp312-win_amd64.whl"
        ),
        size=2915418348,
        sha256="f6c79eac0018f9d131479ee1b7a68edb030619a316bfbc69275043aa4f338e4c",
    ),
    "torchaudio": Wheel(
        name="torchaudio",
        version="2.8.0+cu126",
        filename="torchaudio-2.8.0+cu126-cp312-cp312-win_amd64.whl",
        url=(
            "https://download.pytorch.org/whl/cu126/"
            "torchaudio-2.8.0%2Bcu126-cp312-cp312-win_amd64.whl"
        ),
        size=4237058,
        sha256="4098eadd03a8d283b229677d1e02114fcf6a8bc331941a08232499e43ae8a497",
    ),
}

_EXTRA_CLAUSE = re.compile(r"extra\s*(==|!=)\s*(['\"])([^'\"]+)\2")


def _evaluate_marker(marker: str | None, active_extras: frozenset[str]) -> bool:
    """Evaluate a lock-file marker for the target environment.

    uv spells the project's conflicting extras as `extra == 'extra-<n>-
    <project>-<extra>'` clauses. `packaging` can only evaluate one `extra`
    value at a time, so those clauses are rewritten to literal truths first:
    true when that extra is among the active ones, false otherwise.
    """
    if not marker:
        return True

    def substitute(match: re.Match[str]) -> str:
        operator, _quote, value = match.groups()
        active = value in active_extras
        truth = active if operator == "==" else not active
        # A marker needs an environment name on one side; `os_name` is
        # pinned to `nt` in MARKER_ENV, so these are constant truths.
        return "os_name == 'nt'" if truth else "os_name == 'never'"

    rewritten = _EXTRA_CLAUSE.sub(substitute, marker)
    return bool(Marker(rewritten).evaluate(MARKER_ENV))


class Lock:
    """`uv.lock` with the lookups the closure walk needs."""

    def __init__(self, path: Path) -> None:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        self._entries: dict[str, list[dict[str, Any]]] = {}
        for package in data.get("package", []):
            self._entries.setdefault(canonicalize_name(package["name"]), []).append(package)

    def entry(self, name: str, source: Any | None, active_extras: frozenset[str]) -> dict[str, Any]:
        candidates = self._entries.get(canonicalize_name(name))
        if not candidates:
            raise SystemExit(f"uv.lock has no package named {name!r}")
        if len(candidates) == 1:
            return candidates[0]
        if source is not None:
            by_source = [c for c in candidates if c.get("source") == source]
            if len(by_source) == 1:
                return by_source[0]
        by_markers = [
            c
            for c in candidates
            if any(_evaluate_marker(m, active_extras) for m in c.get("resolution-markers", []))
        ]
        if len(by_markers) == 1:
            return by_markers[0]
        raise SystemExit(
            f"uv.lock holds {len(candidates)} entries for {name!r} and none is unambiguous "
            f"for {PLATFORM}/cp{PYTHON_VERSION[0]}{PYTHON_VERSION[1]}"
        )


def _closure(lock: Lock, project_extras: frozenset[str]) -> dict[str, dict[str, Any]]:
    """Every package the project's dependencies (plus `project_extras`)
    pull in for the target environment, keyed by canonical name."""
    # uv names the conflict-marker extras `extra-<len>-<project>-<extra>`.
    active = frozenset(
        {f"extra-{len(PROJECT_NAME)}-{PROJECT_NAME}-{extra}" for extra in project_extras}
    )
    root = lock.entry(PROJECT_NAME, None, active)
    resolved: dict[str, dict[str, Any]] = {}
    queue: list[tuple[str, Any | None, tuple[str, ...]]] = []

    def push_deps(package: dict[str, Any], extras: tuple[str, ...]) -> None:
        deps = list(package.get("dependencies", []))
        optional = package.get("optional-dependencies", {})
        for extra in extras:
            deps.extend(optional.get(extra, []))
        for dep in deps:
            if not _evaluate_marker(dep.get("marker"), active):
                continue
            queue.append((dep["name"], dep.get("source"), tuple(dep.get("extra", []))))

    push_deps(root, tuple(sorted(project_extras)))
    seen_with_extras: set[tuple[str, tuple[str, ...]]] = set()
    while queue:
        name, source, extras = queue.pop()
        key = (canonicalize_name(name), extras)
        if key in seen_with_extras:
            continue
        seen_with_extras.add(key)
        package = lock.entry(name, source, active)
        resolved[canonicalize_name(name)] = package
        push_deps(package, extras)
    return resolved


def _supported_tags() -> list[tags.Tag]:
    interpreter = f"cp{PYTHON_VERSION[0]}{PYTHON_VERSION[1]}"
    supported = list(
        tags.cpython_tags(python_version=PYTHON_VERSION, abis=[interpreter], platforms=[PLATFORM])
    )
    supported.extend(
        tags.compatible_tags(
            python_version=PYTHON_VERSION, interpreter=interpreter, platforms=[PLATFORM]
        )
    )
    return supported


def _pick_wheel(package: dict[str, Any], supported: list[tags.Tag]) -> Wheel:
    name = package["name"]
    version = package["version"]
    best: tuple[int, dict[str, Any]] | None = None
    for wheel in package.get("wheels", []):
        filename = unquote(wheel["url"].rsplit("/", 1)[-1])
        _, _, _, wheel_tags = parse_wheel_filename(filename)
        ranks = [i for i, tag in enumerate(supported) if tag in wheel_tags]
        if not ranks:
            continue
        rank = min(ranks)
        if best is None or rank < best[0]:
            best = (rank, wheel)
    if best is None:
        return _pick_sdist(package)
    wheel = best[1]
    return Wheel(
        name=name,
        version=version,
        filename=unquote(wheel["url"].rsplit("/", 1)[-1]),
        url=wheel["url"],
        size=int(wheel["size"]),
        sha256=_sha256_of(name, version, wheel["hash"]),
    )


def _sha256_of(name: str, version: str, digest: str) -> str:
    if not digest.startswith("sha256:"):
        raise SystemExit(f"{name}=={version}: unsupported archive hash {digest!r}")
    return digest.removeprefix("sha256:")


def _pick_sdist(package: dict[str, Any]) -> Wheel:
    name = package["name"]
    version = package["version"]
    roots = SDIST_ROOTS.get(canonicalize_name(name))
    sdist = package.get("sdist")
    if roots is None or sdist is None:
        raise SystemExit(
            f"{name}=={version} has no wheel for cp{PYTHON_VERSION[0]}{PYTHON_VERSION[1]}/"
            f"{PLATFORM} in uv.lock; a source-only package cannot be fetched at first run"
        )
    filename = unquote(sdist["url"].rsplit("/", 1)[-1])
    if not filename.endswith(".tar.gz"):
        raise SystemExit(f"{name}=={version}: sdist {filename!r} is not a .tar.gz")
    top_dir = filename.removesuffix(".tar.gz")
    subdir, extract_prefix = roots
    return Wheel(
        name=name,
        version=version,
        filename=filename,
        url=sdist["url"],
        size=int(sdist["size"]),
        sha256=_sha256_of(name, version, sdist["hash"]),
        archive_root=f"{top_dir}/{subdir}",
        extract_prefix=extract_prefix,
    )


def compute_wheels(lock_path: Path = LOCK_FILE, variant: str = "cuda") -> list[Wheel]:
    """The runtime's wheels for one variant: `cuda` carries the
    `CUDA_OVERRIDES` torch pair, `cpu` the PyPI builds the lock pins."""
    if variant not in RUNTIME_VARIANTS:
        raise SystemExit(f"unknown runtime variant {variant!r}; one of {RUNTIME_VARIANTS}")
    lock = Lock(lock_path)
    base = _closure(lock, frozenset(BASE_EXTRAS))
    full = _closure(lock, frozenset(BASE_EXTRAS) | {RUNTIME_EXTRA})
    added = {name: package for name, package in full.items() if name not in base}
    for required in ("pyannote-audio", "torch", "torchaudio"):
        if required not in added:
            raise SystemExit(f"expected {required!r} in the diarization closure; got none")

    supported = _supported_tags()
    wheels: list[Wheel] = []
    for name in sorted(added):
        package = added[name]
        override = CUDA_OVERRIDES.get(name)
        if override is not None:
            locked = package["version"]
            if override.version.split("+", 1)[0] != locked:
                raise SystemExit(
                    f"{name}: uv.lock pins {locked} but CUDA_OVERRIDES carries "
                    f"{override.version}; re-pin the override to the locked version"
                )
            if variant == "cuda":
                wheels.append(override)
                continue
        wheels.append(_pick_wheel(package, supported))
    return wheels


def compute_manifest(lock_path: Path = LOCK_FILE) -> dict[str, list[Wheel]]:
    """Every variant's wheels, keyed by variant name."""
    return {variant: compute_wheels(lock_path, variant) for variant in RUNTIME_VARIANTS}


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_wheel(lines: list[str], wheel: Wheel, indent: str) -> None:
    lines.append(f"{indent}Wheel(")
    lines.append(f"{indent}    name={_quoted(wheel.name)},")
    lines.append(f"{indent}    version={_quoted(wheel.version)},")
    lines.append(f"{indent}    filename={_quoted(wheel.filename)},")
    lines.append(f"{indent}    url={_quoted(wheel.url)},")
    lines.append(f"{indent}    size={wheel.size},")
    lines.append(f"{indent}    sha256={_quoted(wheel.sha256)},")
    if wheel.archive_root or wheel.extract_prefix:
        lines.append(f"{indent}    archive_root={_quoted(wheel.archive_root)},")
        lines.append(f"{indent}    extract_prefix={_quoted(wheel.extract_prefix)},")
    lines.append(f"{indent}),")


def render(manifest: dict[str, list[Wheel]]) -> str:
    """The generated module: the wheels every variant shares, written once,
    then the torch pair per variant, then the per-variant totals."""
    if tuple(manifest) != RUNTIME_VARIANTS:
        raise SystemExit(f"manifest variants {tuple(manifest)} != {RUNTIME_VARIANTS}")
    reference = manifest[RUNTIME_VARIANTS[0]]
    shared = [wheel for wheel in reference if wheel.name not in CUDA_OVERRIDES]
    per_variant = {
        variant: [wheel for wheel in wheels if wheel.name in CUDA_OVERRIDES]
        for variant, wheels in manifest.items()
    }
    for variant, wheels in manifest.items():
        if [wheel for wheel in wheels if wheel.name not in CUDA_OVERRIDES] != shared:
            raise SystemExit(f"variant {variant!r} differs from {RUNTIME_VARIANTS[0]!r} beyond torch")
    totals = {variant: sum(w.size for w in wheels) for variant, wheels in manifest.items()}
    lines = [
        '"""The pinned wheels of the first-run diarization runtime.',
        "",
        "GENERATED by `scripts/gen_diarization_runtime.py` from",
        "`services/transcription/uv.lock` -- do not edit by hand; `make lint`",
        "fails when this file drifts from the lock. Every package the",
        f"`{RUNTIME_EXTRA}` extra adds on top of the baked environment, for",
        f"CPython {PYTHON_VERSION[0]}.{PYTHON_VERSION[1]} on {PLATFORM}, in two variants that",
        "differ only in the torch pair: `cuda` swaps torch/torchaudio for their",
        f"`{TORCH_CUDA_VARIANT}` builds (see the generator's `CUDA_OVERRIDES`), `cpu` keeps",
        "the PyPI builds the lock pins. Consumed by",
        "`transcription.diarization_runtime`.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import NamedTuple",
        "",
        "",
        "class Wheel(NamedTuple):",
        "    name: str",
        "    version: str",
        "    filename: str",
        "    url: str",
        "    size: int",
        "    sha256: str",
        "    # A source tarball's importable-tree directory and member filter",
        "    # (`cuda_runtime.CudaPackage.archive_root` / `extract_prefix`).",
        '    archive_root: str = ""',
        '    extract_prefix: str = ""',
        "",
        "",
        f"TORCH_CUDA_VARIANT = {_quoted(TORCH_CUDA_VARIANT)}",
        "RUNTIME_VARIANTS: tuple[str, ...] = ("
        + ", ".join(_quoted(v) for v in RUNTIME_VARIANTS)
        + ")",
        "",
        "# Every package but the torch pair: the same files in both variants.",
        "SHARED_WHEELS: tuple[Wheel, ...] = (",
    ]
    for wheel in shared:
        _render_wheel(lines, wheel, "    ")
    lines.append(")")
    lines.append("")
    lines.append("# The torch pair per variant.")
    lines.append("TORCH_WHEELS: dict[str, tuple[Wheel, ...]] = {")
    for variant in RUNTIME_VARIANTS:
        lines.append(f"    {_quoted(variant)}: (")
        for wheel in per_variant[variant]:
            _render_wheel(lines, wheel, "        ")
        lines.append("    ),")
    lines.append("}")
    lines.append("")
    lines.append("DIARIZATION_WHEELS: dict[str, tuple[Wheel, ...]] = {")
    lines.append("    variant: SHARED_WHEELS + TORCH_WHEELS[variant] for variant in RUNTIME_VARIANTS")
    lines.append("}")
    lines.append("TOTAL_BYTES: dict[str, int] = {")
    for variant in RUNTIME_VARIANTS:
        lines.append(f"    {_quoted(variant)}: {totals[variant]},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed module matches the lock; exit 1 on drift.",
    )
    parser.add_argument("--lock", type=Path, default=LOCK_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    manifest = compute_manifest(args.lock)
    rendered = render(manifest)
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != rendered:
            print(
                f"{args.output.relative_to(REPO_ROOT) if args.output.is_relative_to(REPO_ROOT) else args.output}"
                " is out of date; run: uv run scripts/gen_diarization_runtime.py",
                file=sys.stderr,
            )
            return 1
        print("diarization runtime manifest matches uv.lock")
        return 0

    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    summary = ", ".join(
        f"{variant}: {len(wheels)} wheels, {sum(w.size for w in wheels) / 1e9:.2f} GB"
        for variant, wheels in manifest.items()
    )
    print(f"wrote {args.output} ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
