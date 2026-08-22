#!/usr/bin/env python
"""One-command release build orchestration (FR-6, FR-15, NFR-1, R4).

`make installer` (and the direct equivalent `uv run scripts/build_installer.py`,
per R6) runs this as the single non-interactive entry point that turns a
clean, bootstrapped clone into a signed-nothing NSIS installer at a
deterministic path. Per the pipeline documented in plan.md:

    1. version check   scripts/sync_version.py --check      (FR-5)
    2. lock check      scripts/verify_locks.py --check       (FR-4)
    3. pyenv bake       scripts/build_pyenv.py               (FR-8, NFR-1)
    4. tauri build      npm --prefix apps/desktop run tauri -- build -- --locked   (frozen, E5)
    5. collect          dist/Transcriber_<version>_x64-setup.exe
                        dist/Transcriber_<version>_x64-setup.exe.sha256
                        dist/build-manifest.json               (FR-15)
    6. gate             installer size <= 1.5 GB                (NFR-1, R4)

No `--extra cuda` by default (Defect 1, `docs/verification-installer.md`
"Blocker 1"): the vendored, 32-bit `makensis.exe` empirically cannot
compile the ~2.3 GiB payload that extra produces. The CUDA runtime wheels
are fetched at first run instead (`transcription.cuda_runtime`), mirroring
the existing model-download pattern. Pass `--extra cuda` explicitly for a
dev/CI build that still wants it baked in (that build cannot go through
NSIS packaging today).

Every stage is a plain module-level function looked up by name off `STAGES`
so failure injection in tests (monkeypatching `stage_pyenv_bake`, etc.) is
possible without touching the pipeline machinery itself. Each stage aborts
the whole run with its own fixed, distinct exit code (FR-6: "exits non-zero
if any payload fails"), so a caller (or a human) can tell which payload
broke without parsing stderr.

`--dry-run` prints the exact commands this invocation would run and performs
no filesystem or subprocess side effect at all (FR-6, NFR-5: no interactive
command anywhere in the pipeline).

Usage:
    uv run scripts/build_installer.py
    uv run scripts/build_installer.py --dry-run
    uv run scripts/build_installer.py --extra cuda --extra cpu-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
APP_DIR = REPO_ROOT / "apps" / "desktop"
# `Cargo.toml` at the repo root is a workspace (`members = ["crates/vault",
# "apps/desktop/src-tauri"]`), so Cargo places one shared `target/` at the
# *workspace root* -- never a separate `apps/desktop/src-tauri/target/` --
# confirmed empirically against the real `tauri build` output. Found on the
# first real end-to-end build that got far enough to reach this stage.
NSIS_BUNDLE_DIR = REPO_ROOT / "target" / "release" / "bundle" / "nsis"
DEFAULT_DIST_DIR = REPO_ROOT / "dist"

# tauri.conf.json's `bundle.resources` maps
# "apps/desktop/src-tauri/resources/pyenv/" -> "pyenv/" at bundle time (T6).
# A plain `make installer` (no --pyenv-out override) must bake straight into
# that directory -- build_pyenv.DEFAULT_OUT (repo-root build/pyenv/) is a
# generic default for standalone `build_pyenv.py` invocations, not what the
# bundler reads, and baking there would ship the tracked resources/pyenv/
# .gitkeep placeholder instead of the freshly-baked runtime (found by T12,
# fixed by T14).
DEFAULT_PYENV_OUT = APP_DIR / "src-tauri" / "resources" / "pyenv"

# The tracked placeholder's exact committed contents. `build_pyenv.bake`
# rmtrees the directory this lives in, so the release build has to put it
# back byte-for-byte or leave the working tree dirty.
GITKEEP_CONTENTS = (
    "# Placeholder so this directory exists on a clean checkout -- tauri-build's\n"
    "# bundleResources errors if the configured resource path is missing.\n"
    "# The release build (scripts/build_pyenv.py, invoked by `make installer`)\n"
    "# replaces this directory's contents with the baked Python runtime.\n"
)

# scripts/ is a sibling-module directory (no package, no conftest.py, per the
# plan's "each test module derives the repo root itself" contract) -- make
# sure the other build-system modules are importable regardless of whether
# this file was imported directly (uv run scripts/build_installer.py, which
# already puts scripts/ at sys.path[0]) or from a test module that inserted
# scripts/ onto sys.path itself.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_pyenv  # noqa: E402
import sync_version  # noqa: E402
import verify_locks  # noqa: E402

# NFR-1, revised at the spec gate for GPU-first CUDA inference (R4): the
# whole 1.5 GB budget is spent once cuBLAS/cuDNN ship.
SIZE_LIMIT_BYTES = int(1.5 * 1024**3)

# Rust (the Tauri app crate), Node (the desktop UI) and Python (the service)
# each carry their own copy of the product version, kept in sync by T3.
# FR-15 asks the manifest to record "the resolved versions of the three
# payloads" -- reusing sync_version's own manifest table means this never
# drifts from what --check actually verified.
PAYLOAD_LABELS: dict[str, Path] = {
    "rust": REPO_ROOT / "apps/desktop/src-tauri/Cargo.toml",
    "node": REPO_ROOT / "apps/desktop/package.json",
    "python": REPO_ROOT / "services/transcription/pyproject.toml",
}


class BuildInstallerError(RuntimeError):
    """Raised by a stage to abort the pipeline; the message is operator-facing."""


class SizeGateError(BuildInstallerError):
    """Raised when the produced installer exceeds NFR-1's 1.5 GB budget."""


@dataclass
class Stage:
    name: str
    exit_code: int
    func_name: str


# The documented order, byte-for-byte: version check -> lock check ->
# pyenv bake -> tauri build -> collect -> gate. Each exit code is unique so
# a caller can tell which payload broke without parsing stderr (FR-6).
STAGES: list[Stage] = [
    Stage("version_check", 1, "stage_version_check"),
    Stage("lock_check", 2, "stage_lock_check"),
    Stage("pyenv_bake", 3, "stage_pyenv_bake"),
    Stage("tauri_build", 4, "stage_tauri_build"),
    Stage("collect", 5, "stage_collect"),
    Stage("gate", 6, "stage_gate"),
]


@dataclass
class BuildContext:
    """Mutable state threaded through the stages of a single run."""

    repo_root: Path = REPO_ROOT
    dist_dir: Path = DEFAULT_DIST_DIR
    pyenv_out: Path = field(default_factory=lambda: DEFAULT_PYENV_OUT)
    # No CUDA wheels baked in by default (Defect 1,
    # `docs/verification-installer.md` "Blocker 1"): the vendored `makensis`
    # is a 32-bit, non-large-address-aware compiler that cannot compile the
    # ~2.3 GiB payload `--extra cuda` produces (`pyenv-manifest.json`:
    # `total_bytes: 2496449478`) -- confirmed empirically, not merely by
    # inspection, against the real toolchain. The CUDA runtime is now
    # acquired at first run instead (`transcription.cuda_runtime`); the
    # non-CUDA bake is ~414 MB and NSIS-compilable. Pass `--extra cuda`
    # explicitly for a dev/CI build that still wants it baked in.
    extras: tuple[str, ...] = ()
    dry_run: bool = False

    # populated as stages run
    pyenv_manifest: dict | None = None
    installer_src: Path | None = None
    collect_result: "CollectResult | None" = None


@dataclass(frozen=True)
class CollectResult:
    installer_path: Path
    checksum_path: Path
    manifest_path: Path
    sha256: str


def _run(cmd: Sequence[str], *, cwd: Path | None = None) -> str:
    """Run a non-interactive subprocess; raise BuildInstallerError on failure.

    Resolves `cmd[0]` through `PATH` first (`shutil.which`). On Windows,
    `subprocess.run([...], shell=False)` goes straight to `CreateProcess`,
    which does not apply `PATHEXT`/shell resolution to a bare command name --
    `npm` (a `.CMD` shim, not a `.exe`) fails with
    `FileNotFoundError: [WinError 2]` unless resolved to its full path first.
    Found empirically running this pipeline for real (T14): every `npm`/
    `tauri` invocation in `stage_tauri_build` failed this way before this
    fix. Falls back to the unresolved name (unchanged behaviour) when
    `which` cannot find it, so the original, unhelpful-but-honest error
    still surfaces instead of a new one about resolution itself.
    """
    resolved = list(cmd)
    if resolved:
        found = shutil.which(resolved[0])
        if found:
            resolved[0] = found
    proc = subprocess.run(resolved, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BuildInstallerError(
            f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc.stdout


def get_git_commit(repo_root: Path = REPO_ROOT) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip()


def resolve_payload_versions(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """The resolved version string of each of the three payloads (FR-15)."""
    versions: dict[str, str] = {}
    for label, path in PAYLOAD_LABELS.items():
        manifest = next(
            (m for m in sync_version.MANIFESTS if m.path == path),
            None,
        )
        if manifest is None:
            continue
        versions[label] = sync_version.manifest_version(manifest)
    return versions


_HASH_CHUNK_SIZE = 1024 * 1024


def _sha256_file(path: Path) -> str:
    """Chunked digest (E11): NFR-1's own budget is 1.5 GB, so a whole-file
    `path.read_bytes()` (the pattern this replaces, used up to three times
    across `collect()`/`verify_checksum_file()`) is up to 1.5 GB of avoidable
    peak RSS per read at that ceiling. Mirrors
    `transcription.cuda_runtime._sha256_file`'s existing pattern."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_built_installer(repo_root: Path = REPO_ROOT, version: str | None = None) -> Path:
    """Locate the `.exe` the Tauri NSIS bundle stage just produced.

    Matched by its exact expected name -- `sync_version.artifact_name`, the
    same function `collect` names the released file with -- rather than by
    picking from whatever `.exe` files are in the bundle directory.

    This used to take `sorted(glob("*.exe"))[0]`, and that is a genuinely
    dangerous way to find a build artifact: `target/` is not cleaned between
    builds, so after a version bump the directory holds both the new
    installer and every older one, and the *alphabetically first* is the
    oldest. The result was an installer copied out under the new version's
    name carrying the previous version's bytes -- caught building 0.2.0 on a
    tree that had built 0.1.0, where the two artifacts had identical
    checksums. A CI runner with a warm `target/` cache would have done the
    same thing.

    Refusing outright when the expected artifact is absent is the point: a
    build that did not produce what it was asked for must fail, not fall
    back to something that happens to be lying nearby.
    """
    bundle_dir = (
        NSIS_BUNDLE_DIR
        if repo_root == REPO_ROOT
        else repo_root / NSIS_BUNDLE_DIR.relative_to(REPO_ROOT)
    )
    expected = sync_version.artifact_name(version)
    candidate = bundle_dir / expected
    if candidate.is_file():
        return candidate

    present = sorted(path.name for path in bundle_dir.glob("*.exe"))
    found = f" (found: {', '.join(present)})" if present else ""
    raise BuildInstallerError(f"no {expected} under {bundle_dir}{found}")


def gate_artifact_size(path: Path, limit_bytes: int = SIZE_LIMIT_BYTES) -> int:
    """Raise SizeGateError when `path` exceeds `limit_bytes` (NFR-1).

    Exactly `limit_bytes` passes -- the budget is inclusive.
    """
    size = path.stat().st_size
    if size > limit_bytes:
        over_by = size - limit_bytes
        raise SizeGateError(
            f"installer artifact is {size} bytes, exceeding the NFR-1 budget of "
            f"{limit_bytes} bytes (1.5 GB) by {over_by} bytes"
        )
    return size


def collect(
    *,
    installer_src: Path,
    version: str,
    git_commit: str,
    pyenv_manifest: dict,
    payload_versions: dict[str, str],
    dist_dir: Path,
) -> CollectResult:
    """Copy the built installer to `dist_dir`, emit its checksum and the
    build manifest (FR-15). Deterministic filenames, no prompts."""
    dist_dir.mkdir(parents=True, exist_ok=True)

    dest_name = sync_version.artifact_name(version)
    dest_path = dist_dir / dest_name
    shutil.copyfile(installer_src, dest_path)

    digest = _sha256_file(dest_path)
    checksum_path = dist_dir / f"{dest_name}.sha256"
    checksum_path.write_text(f"{digest}  {dest_name}\n", encoding="utf-8")

    manifest = {
        "product_version": version,
        "git_commit": git_commit,
        "payload_versions": dict(sorted(payload_versions.items())),
        "pyenv": {
            "python_version": pyenv_manifest.get("python_version"),
            "packages": pyenv_manifest.get("packages", {}),
            "total_bytes": pyenv_manifest.get("total_bytes"),
        },
        "artifact": {
            "name": dest_name,
            "sha256": digest,
            "size_bytes": dest_path.stat().st_size,
        },
    }
    manifest_path = dist_dir / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return CollectResult(dest_path, checksum_path, manifest_path, digest)


def verify_checksum_file(installer_path: Path, checksum_path: Path) -> bool:
    """True when `checksum_path`'s digest matches the installer's real bytes."""
    recorded = checksum_path.read_text(encoding="utf-8").split()[0]
    actual = _sha256_file(installer_path)
    return recorded == actual


# --- stage functions ---------------------------------------------------
# Each takes the shared BuildContext, mutates it with whatever the next
# stage needs, and raises BuildInstallerError to abort. STAGES looks these
# up by name at call time (never binding the function object ahead of
# time), so tests can monkeypatch any one of them in isolation.


def stage_version_check(ctx: BuildContext) -> None:
    drifting = sync_version.check()
    if drifting:
        raise BuildInstallerError(
            "version drift detected against version.txt in: " + ", ".join(drifting)
        )


def stage_lock_check(ctx: BuildContext) -> None:
    problems = verify_locks.check(ctx.repo_root)
    if problems:
        raise BuildInstallerError("lock check failed: " + "; ".join(problems))


def stage_pyenv_bake(ctx: BuildContext) -> None:
    try:
        ctx.pyenv_manifest = build_pyenv.bake(ctx.pyenv_out, extras=ctx.extras)
    except build_pyenv.BuildPyenvError as exc:
        raise BuildInstallerError(f"pyenv bake failed: {exc}") from exc
    _restore_gitkeep(ctx.pyenv_out)


def _restore_gitkeep(pyenv_out: Path) -> None:
    """Put back the tracked `.gitkeep` the bake just deleted.

    `build_pyenv.bake` replaces its output directory wholesale
    (`shutil.rmtree`), which is right for a build artifact -- but when that
    directory is `apps/desktop/src-tauri/resources/pyenv/` it also removes a
    *tracked* file. The .gitignore there ignores the directory's contents and
    keeps exactly this one placeholder, because `tauri-build`'s
    `bundleResources` errors out if the source path does not exist on a clean
    checkout. Without this, every release build left `git status` reporting a
    deleted file nobody deleted, and a clean clone that had built once could
    no longer build.

    Only ever recreated inside the tracked resources directory: a standalone
    `build_pyenv.py --out somewhere/else` has no placeholder to restore.

    Restored with its committed *contents*, not as an empty file. The
    placeholder explains why it exists, and a build that silently emptied it
    would still leave `git status` dirty -- which is the whole thing this is
    supposed to stop.
    """
    if pyenv_out != DEFAULT_PYENV_OUT:
        return
    gitkeep = pyenv_out / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text(GITKEEP_CONTENTS, encoding="utf-8", newline="\n")


def stage_tauri_build(ctx: BuildContext) -> None:
    _run(["npm", "--prefix", str(APP_DIR), "ci"])
    # E5/FR-4: `--locked` must reach `cargo build` itself, not just `npm
    # ci`/`uv export --frozen` -- otherwise a drifted `Cargo.toml` silently
    # re-resolves and rewrites `Cargo.lock` mid-release-build instead of
    # failing loudly. Getting it there through two layers of `--`
    # empirically verified (a fake `cargo` runner via `tauri build
    # --runner`, both directly and through this exact `npm run` invocation
    # shape): the first `--`, immediately after the `tauri` script name,
    # stops `npm` from parsing anything past it as its own CLI flags (a
    # bare `--locked` there would otherwise be swallowed as an unrecognised
    # `npm` config); the second is Tauri CLI's own documented marker for
    # "everything after this is forwarded to the runner (`cargo` by
    # default)". Without both, `--locked` never leaves the `npm`/`tauri`
    # layer.
    _run(
        ["npm", "--prefix", str(APP_DIR), "run", "tauri", "--", "build", "--", "--locked"]
    )
    ctx.installer_src = find_built_installer(ctx.repo_root)


def stage_collect(ctx: BuildContext) -> None:
    if ctx.installer_src is None:
        raise BuildInstallerError("collect ran before an installer artifact was produced")
    version = sync_version.read_version()
    git_commit = get_git_commit(ctx.repo_root)
    payload_versions = resolve_payload_versions(ctx.repo_root)
    ctx.collect_result = collect(
        installer_src=ctx.installer_src,
        version=version,
        git_commit=git_commit,
        pyenv_manifest=ctx.pyenv_manifest or {},
        payload_versions=payload_versions,
        dist_dir=ctx.dist_dir,
    )


def stage_gate(ctx: BuildContext) -> None:
    if ctx.collect_result is None:
        raise BuildInstallerError("gate ran before collect produced an artifact")
    try:
        gate_artifact_size(ctx.collect_result.installer_path)
    except SizeGateError as exc:
        raise BuildInstallerError(str(exc)) from exc


def run_stages(ctx: BuildContext, stages: Sequence[Stage] = STAGES) -> int:
    module = sys.modules[__name__]
    for stage in stages:
        func = getattr(module, stage.func_name)
        try:
            func(ctx)
        except BuildInstallerError as exc:
            print(f"build_installer: [{stage.name}] failed: {exc}", file=sys.stderr)
            return stage.exit_code
    print(
        f"build_installer: {ctx.collect_result.installer_path if ctx.collect_result else '(nothing collected)'}"
    )
    return 0


def describe_plan(ctx: BuildContext) -> list[str]:
    """Human-readable command list for `--dry-run` (FR-6, NFR-5)."""
    extras_flags = " ".join(f"--extra {extra}" for extra in ctx.extras)
    return [
        f"uv run {SCRIPTS_DIR / 'sync_version.py'} --check",
        f"uv run {SCRIPTS_DIR / 'verify_locks.py'} --check",
        f"uv run {SCRIPTS_DIR / 'build_pyenv.py'} --out {ctx.pyenv_out}"
        + (f" {extras_flags}" if extras_flags else ""),
        f"npm --prefix {APP_DIR} ci",
        f"npm --prefix {APP_DIR} run tauri -- build -- --locked",
        f"collect -> {ctx.dist_dir}/Transcriber_<version>_x64-setup.exe (+ .sha256, build-manifest.json)",
        f"gate: installer size <= {SIZE_LIMIT_BYTES} bytes (1.5 GB, NFR-1)",
    ]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the plan; touch nothing")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        dest="extras",
        help=(
            "uv optional-dependency-group passed through to build_pyenv.py "
            "(repeatable); default: none (Defect 1 -- pass '--extra cuda' "
            "explicitly for a dev/CI build that still bakes it in; NSIS "
            "cannot compile that payload for a real release build)"
        ),
    )
    parser.add_argument("--dist-dir", type=Path, default=DEFAULT_DIST_DIR)
    parser.add_argument("--pyenv-out", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Defect 1: no CUDA wheels baked in by default -- see BuildContext.extras.
    extras = tuple(args.extras) if args.extras else ()
    ctx = BuildContext(
        dist_dir=args.dist_dir,
        pyenv_out=args.pyenv_out if args.pyenv_out is not None else DEFAULT_PYENV_OUT,
        extras=extras,
        dry_run=args.dry_run,
    )

    if ctx.dry_run:
        for line in describe_plan(ctx):
            print(f"[dry-run] {line}")
        return 0

    return run_stages(ctx)


if __name__ == "__main__":
    sys.exit(main())
