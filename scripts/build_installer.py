#!/usr/bin/env python
"""One-command release build orchestration (FR-6, FR-15, NFR-1, R4).

`make installer` (and the direct equivalent `uv run scripts/build_installer.py`,
per R6) runs this as the single non-interactive entry point that turns a
clean, bootstrapped clone into a signed-nothing NSIS installer at a
deterministic path. Per the pipeline documented in plan.md:

    1. version check   scripts/sync_version.py --check      (FR-5)
    2. lock check      scripts/verify_locks.py --check       (FR-4)
    3. engine payload  scripts/stage_engine_payload.py       (FR-8, NFR-1)
    4. tauri build      npm --prefix apps/desktop run tauri -- build -- --locked   (frozen, E5)
                        (on macOS: `build --bundles app,dmg`, since
                        tauri.conf.json's committed `targets` list is the
                        Windows NSIS contract)
    5. collect          dist/Transcriber_<version>_x64-setup.exe (Windows)
                        or dist/Transcriber_<version>_aarch64.dmg (macOS)
                        + its .sha256 and dist/build-manifest.json (FR-15)
    6. gate             installer size <= 300 MB                (NFR-1, R4)

The speech and assistant models are not in the installer: they are
gigabytes, and they are fetched on first run instead (`crates/fetcher`).
What ships is only what the app cannot start without.

Every stage is a plain module-level function looked up by name off `STAGES`
so failure injection in tests (monkeypatching `stage_engine_payload`, etc.) is
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
# The Tauri bundle kind (and the installer extension it produces) per
# platform: the NSIS `.exe` on Windows, the `.dmg` on macOS. tauri.conf.json
# only lists `nsis` (a committed contract `test_bundle_config.py` pins), so
# the macOS build passes its bundle kinds on the CLI instead -- see
# `stage_tauri_build`.
BUNDLE_KIND = "nsis" if sys.platform == "win32" else "dmg"
INSTALLER_GLOB = "*.exe" if sys.platform == "win32" else "*.dmg"


def bundle_dir(repo_root: Path = REPO_ROOT) -> Path:
    """Where `tauri build` leaves this platform's installer.

    `Cargo.toml` at the repo root is a workspace (`members = ["crates/vault",
    "apps/desktop/src-tauri"]`), so Cargo places one shared `target/` at the
    *workspace root* -- never a separate `apps/desktop/src-tauri/target/` --
    confirmed empirically against the real `tauri build` output. Found on the
    first real end-to-end build that got far enough to reach this stage.
    """
    return repo_root / "target" / "release" / "bundle" / BUNDLE_KIND


DEFAULT_DIST_DIR = REPO_ROOT / "dist"

# tauri.conf.json's `bundle.resources` maps
# scripts/ is a sibling-module directory (no package, no conftest.py, per the
# plan's "each test module derives the repo root itself" contract) -- make
# sure the other build-system modules are importable regardless of whether
# this file was imported directly (uv run scripts/build_installer.py, which
# already puts scripts/ at sys.path[0]) or from a test module that inserted
# scripts/ onto sys.path itself.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_version  # noqa: E402
import verify_locks  # noqa: E402

# NFR-1, revised at the spec gate for GPU-first CUDA inference (R4): the
# whole 1.5 GB budget is spent once cuBLAS/cuDNN ship.
# The pyenv bake made this 1.5 GB. The engine payload is ~184 MB before
# compression, so a budget that would have been generous then is now a gate
# that would not notice an accidental doubling.
SIZE_LIMIT_BYTES = int(300 * 1024**2)

# Rust (the Tauri app crate) and Node (the desktop UI) each carry their own
# copy of the product version, kept in sync by T3. Reusing sync_version's own
# manifest table means this never drifts from what --check actually verified.
# The third payload used to be the Python service, which no longer exists.
PAYLOAD_LABELS: dict[str, Path] = {
    "rust": REPO_ROOT / "apps/desktop/src-tauri/Cargo.toml",
    "node": REPO_ROOT / "apps/desktop/package.json",
}


class BuildInstallerError(RuntimeError):
    """Raised by a stage to abort the pipeline; the message is operator-facing."""


class SizeGateError(BuildInstallerError):
    """Raised when the produced installer exceeds its size budget."""


@dataclass
class Stage:
    name: str
    exit_code: int
    func_name: str


# The documented order: version check -> lock check -> engine payload ->
# tauri build -> collect -> gate. Each exit code is unique so
# a caller can tell which payload broke without parsing stderr (FR-6).
STAGES: list[Stage] = [
    Stage("version_check", 1, "stage_version_check"),
    Stage("lock_check", 2, "stage_lock_check"),
    Stage("engine_payload", 3, "stage_engine_payload"),
    Stage("tauri_build", 4, "stage_tauri_build"),
    Stage("collect", 5, "stage_collect"),
    Stage("gate", 6, "stage_gate"),
]


@dataclass
class BuildContext:
    """Mutable state threaded through the stages of a single run."""

    repo_root: Path = REPO_ROOT
    dist_dir: Path = DEFAULT_DIST_DIR
    # Which cargo profile the engine libraries are collected from, and where
    # the pinned third-party payload is cached between builds.
    engine_profile: str = "release"
    payload_cache: Path = field(
        default_factory=lambda: REPO_ROOT / ".cache/engine-payload"
    )
    dry_run: bool = False

    # populated as stages run
    payload_manifest: dict | None = None
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
    """Locate the installer the Tauri bundle stage just produced.

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
    search_dir = bundle_dir(repo_root)
    expected = sync_version.artifact_name(version)
    candidate = search_dir / expected
    if candidate.is_file():
        return candidate

    present = sorted(path.name for path in search_dir.glob(INSTALLER_GLOB))
    found = f" (found: {', '.join(present)})" if present else ""
    raise BuildInstallerError(f"no {expected} under {search_dir}{found}")


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
    payload_manifest: dict,
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
        "engine_payload": {
            "files": payload_manifest.get("files", []),
            "total_bytes": payload_manifest.get("total_bytes"),
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


def stage_engine_payload(ctx: BuildContext) -> None:
    """Collect the engine libraries, ffmpeg, ONNX Runtime and the diarization
    models the installer ships.

    Replaces the pyenv bake. That staged a ~420 MB Python runtime because half
    the product was a Python service; this stages ~184 MB of native pieces
    because the product is one binary plus the libraries it loads.
    """
    try:
        ctx.payload_manifest = stage_engine_payload_impl.stage(
            ctx.engine_profile, ctx.payload_cache
        )
    except stage_engine_payload_impl.StageError as exc:
        raise BuildInstallerError(f"engine payload staging failed: {exc}") from exc


def _tauri_build_args() -> list[str]:
    """The `tauri <args>` this platform's bundle build needs.

    tauri.conf.json commits to `targets: ["nsis"]` (a Windows-only list
    `test_bundle_config.py` pins), so on macOS the bundle kinds come from
    the CLI instead: `app` (which `createUpdaterArtifacts` turns into the
    updater's `Transcriber.app.tar.gz` + `.sig`) and `dmg` (first-install
    media).
    """
    args = ["build"]
    if sys.platform == "darwin":
        args += ["--bundles", "app,dmg"]
    return args


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
        ["npm", "--prefix", str(APP_DIR), "run", "tauri", "--", *_tauri_build_args(), "--", "--locked"]
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
        payload_manifest=ctx.payload_manifest or {},
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
    return [
        f"uv run {SCRIPTS_DIR / 'sync_version.py'} --check",
        f"uv run {SCRIPTS_DIR / 'verify_locks.py'} --check",
        f"uv run {SCRIPTS_DIR / 'stage_engine_payload.py'} --profile {ctx.engine_profile}",
        f"npm --prefix {APP_DIR} ci",
        f"npm --prefix {APP_DIR} run tauri -- {' '.join(_tauri_build_args())} -- --locked",
        f"collect -> {ctx.dist_dir}/{sync_version.artifact_name('<version>')} (+ .sha256, build-manifest.json)",
        f"gate: installer size <= {SIZE_LIMIT_BYTES} bytes (300 MB)",
    ]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the plan; touch nothing")
    parser.add_argument(
        "--engine-profile",
        default="release",
        help="cargo profile to collect the engine libraries from",
    )
    parser.add_argument("--dist-dir", type=Path, default=DEFAULT_DIST_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ctx = BuildContext(
        dist_dir=args.dist_dir,
        engine_profile=args.engine_profile,
        dry_run=args.dry_run,
    )

    if ctx.dry_run:
        for line in describe_plan(ctx):
            print(f"[dry-run] {line}")
        return 0

    return run_stages(ctx)


if __name__ == "__main__":
    sys.exit(main())
