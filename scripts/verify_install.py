#!/usr/bin/env python
"""Empirical installer verification (T14, FR-8, FR-10/11, NFR-1, NFR-4).

Reads a *real*, already-installed application folder (and, optionally, the
built installer artifact) and prints a pass/fail table, exiting non-zero on
any failure. This is the automated half of T14's evidence -- the checker
`docs/manual-smoke-checklist.md`'s installer section and
`docs/verification-installer.md` cite as having actually been run against
the real install produced by `make installer`.

Every check below is a small, pure function taking plain paths/strings so
it can be exercised in `scripts/tests/test_verify_install.py` against a
synthetic tree under `tmp_path`, with no real installer and no real
Windows install anywhere near the test suite. `main()` is the only place
that touches a real filesystem path supplied on the command line or makes
a real HTTP request (`GET /health`); nothing else here is network- or
install-dependent.

Usage (a real, install-time invocation):

    uv run scripts/verify_install.py \\
        --install-dir "%LOCALAPPDATA%\\Programs\\Transcriber" \\
        --artifact dist\\Transcriber_0.1.0_x64-setup.exe \\
        --checksum dist\\Transcriber_0.1.0_x64-setup.exe.sha256 \\
        --config "%APPDATA%\\com.transcriber.desktop\\config.json" \\
        --expected-vault-root D:\\Meetings \\
        --health-url http://127.0.0.1:<port>/health
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

# NFR-1: the same 1.5 GB budget `scripts/build_installer.py`'s size gate
# enforces at build time -- this script re-checks it against the artifact
# actually produced, independent of that pipeline run.
SIZE_LIMIT_BYTES = int(1.5 * 1024**3)

SKELETON_DIRS = ("models", "logs", "data")

# The bundled runtime tree T6's `bundle.resources` mapping deposits at the
# install root (see `apps/desktop/src-tauri/src/app_paths.rs`): directly
# The engine's own files, staged by `stage_engine_payload.py` and shipped into
# the application folder's root. An install missing any of them starts but
# cannot transcribe, which is a worse failure than not starting at all.
ENGINE_FILES = (
    "whisper.dll",
    "llama.dll",
    "ggml.dll",
    "ggml-base.dll",
    "ffmpeg.exe",
    "runtime/onnx/onnxruntime.dll",
)
ENGINE_DIRS = ("runtime/engine/backends", "models/diarization")

DEFAULT_APP_EXE_NAME = "Transcriber.exe"

_PROBE_FILE_NAME = ".verify_install_write_probe"


# --- app-folder skeleton (FR-8) -------------------------------------------


def check_directory_skeleton(install_dir: Path) -> list[str]:
    """Flags a missing `models\\`/`logs\\`/`data\\` under `install_dir`."""
    problems: list[str] = []
    for name in SKELETON_DIRS:
        if not (install_dir / name).is_dir():
            problems.append(f"missing directory: {install_dir / name}")
    return problems


def check_engine_payload(install_dir: Path) -> list[str]:
    """Flags anything the engine needs that the installer did not put there."""
    problems: list[str] = []
    for name in ENGINE_FILES:
        if not (install_dir / name).is_file():
            problems.append(f"missing engine file: {name}")
    for name in ENGINE_DIRS:
        directory = install_dir / name
        if not directory.is_dir() or not any(directory.iterdir()):
            problems.append(f"missing or empty engine directory: {name}")
    return problems

def check_app_executable(install_dir: Path, exe_name: str = DEFAULT_APP_EXE_NAME) -> list[str]:
    """Flags a missing app executable at the install root."""
    if not (install_dir / exe_name).is_file():
        return [f"missing app executable: {install_dir / exe_name}"]
    return []


# --- writability (FR-8 acceptance) ----------------------------------------


def check_writable_dir(dir_path: Path) -> list[str]:
    """Flags `dir_path` as not writable by a non-elevated process.

    Writes and removes a small probe file; any `OSError` (missing parent,
    permission denied, read-only volume, ...) is reported as a problem
    rather than raised, so callers can compose several such checks into one
    pass/fail table without a try/except at every call site.
    """
    probe = dir_path / _PROBE_FILE_NAME
    try:
        probe.write_text("verify_install probe\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return [f"not writable: {dir_path} ({exc})"]
    return []


# --- artifact size + checksum (NFR-1, FR-15) ------------------------------


def check_artifact_size(artifact_path: Path, limit_bytes: int = SIZE_LIMIT_BYTES) -> list[str]:
    """Flags an installer artifact over `limit_bytes` (inclusive pass)."""
    size = artifact_path.stat().st_size
    if size > limit_bytes:
        over_by = size - limit_bytes
        return [
            f"installer artifact {artifact_path} is {size} bytes, exceeding the "
            f"NFR-1 budget of {limit_bytes} bytes (1.5 GB) by {over_by} bytes"
        ]
    return []


_HASH_CHUNK_SIZE = 1024 * 1024


def _sha256_file(path: Path) -> str:
    """Chunked digest (E11) -- avoids materialising the whole (up to 1.5 GB,
    NFR-1) artifact in memory just to hash it. Mirrors
    `build_installer._sha256_file`/`transcription.cuda_runtime._sha256_file`."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_checksum(artifact_path: Path, checksum_path: Path) -> list[str]:
    """Flags a `.sha256` file whose recorded digest does not match the
    artifact's real bytes."""
    recorded = checksum_path.read_text(encoding="utf-8").split()[0]
    actual = _sha256_file(artifact_path)
    if recorded != actual:
        return [f"checksum mismatch for {artifact_path}: recorded {recorded}, actual {actual}"]
    return []


# --- config.json validity + vault-root resolution (FR-10/11) -------------


def _read_config_text(config_path: Path) -> str:
    """Reads `config_path` tolerating a leading UTF-8 BOM -- the same
    tolerance `apps/desktop/src-tauri/src/config.rs::load` applies, so this
    script never flags a BOM'd-but-otherwise-valid file as malformed."""
    raw = config_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8")


def check_config_json(config_path: Path) -> list[str]:
    """Flags a `config.json` that is invalid JSON, lacks `meetings_root`,
    or lacks a schema version key (FR-10/11)."""
    try:
        text = _read_config_text(config_path)
    except OSError as exc:
        return [f"cannot read config: {config_path} ({exc})"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"config.json is not valid JSON: {config_path} ({exc})"]

    if not isinstance(data, dict):
        return [f"config.json does not contain a JSON object: {config_path}"]

    problems: list[str] = []
    if "meetings_root" not in data:
        problems.append(f"config.json lacks meetings_root: {config_path}")
    if "schema_version" not in data:
        problems.append(f"config.json lacks a schema_version key: {config_path}")
    return problems


def resolve_vault_root(config_path: Path) -> str | None:
    """Resolves `meetings_root` from `config_path` the same way the app
    reads it (FR-11 acceptance: "a script reading the config the way the
    service will read it resolves the same vault root"). Returns `None` for
    a missing key, an absent file, or invalid JSON, rather than raising --
    callers that need to distinguish those cases use `check_config_json`."""
    try:
        text = _read_config_text(config_path)
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("meetings_root")
    return value if isinstance(value, str) else None


def check_vault_root_matches(config_path: Path, expected: str) -> list[str]:
    """Flags a resolved vault root that disagrees with the value the
    operator actually picked (e.g. via `/VAULT=` at silent-install time)."""
    resolved = resolve_vault_root(config_path)
    if resolved != expected:
        return [
            f"resolved vault root {resolved!r} does not match the expected "
            f"root {expected!r} (config: {config_path})"
        ]
    return []


# --- GET /health (NFR-1 acceptance: device == cuda) -----------------------


def check_health_reports_cuda(health_url: str, timeout: float = 5.0) -> list[str]:
    """Flags a `/health` response that does not report `device: cuda`.

    This is the one check that makes a real network call (loopback only,
    per F2's contract) -- it is deliberately not covered by the pytest unit
    suite; it is exercised for real by `docs/verification-installer.md`'s
    manual pass with a real model and a real running sidecar.
    """
    try:
        with urlopen(Request(health_url), timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - report, never raise, from a CLI check
        return [f"GET {health_url} failed: {exc}"]
    device = body.get("device")
    if device != "cuda":
        return [f"GET {health_url} reported device={device!r}, expected 'cuda'"]
    return []


# --- CLI -------------------------------------------------------------------


def run_checks(
    *,
    install_dir: Path | None,
    exe_name: str,
    artifact: Path | None,
    checksum: Path | None,
    config: Path | None,
    expected_vault_root: str | None,
    health_url: str | None,
) -> dict[str, list[str]]:
    """Runs every check whose required inputs were supplied; returns a
    `{check name: [problems]}` map (empty list == pass)."""
    results: dict[str, list[str]] = {}

    if install_dir is not None:
        results["app executable"] = check_app_executable(install_dir, exe_name)
        results["engine payload"] = check_engine_payload(install_dir)
        results["app-folder skeleton (models/logs/data)"] = check_directory_skeleton(install_dir)
        for name in SKELETON_DIRS:
            target = install_dir / name
            if target.is_dir():
                results[f"writable: {name}"] = check_writable_dir(target)

    if artifact is not None:
        results["artifact size (<= 1.5 GB)"] = check_artifact_size(artifact)
        if checksum is not None:
            results["artifact checksum"] = check_checksum(artifact, checksum)

    if config is not None:
        results["config.json validity"] = check_config_json(config)
        if expected_vault_root is not None:
            results["vault root resolution"] = check_vault_root_matches(config, expected_vault_root)

    if health_url is not None:
        results["GET /health device=cuda"] = check_health_reports_cuda(health_url)

    return results


def _print_report(results: dict[str, list[str]]) -> bool:
    """Prints a pass/fail table; returns True iff every check passed."""
    all_passed = True
    width = max((len(name) for name in results), default=0)
    for name, problems in results.items():
        status = "PASS" if not problems else "FAIL"
        if problems:
            all_passed = False
        print(f"[{status}] {name.ljust(width)}")
        for problem in problems:
            print(f"       - {problem}")
    return all_passed


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install-dir", type=Path, default=None)
    parser.add_argument("--exe-name", type=str, default=DEFAULT_APP_EXE_NAME)
    parser.add_argument("--artifact", type=Path, default=None)
    parser.add_argument("--checksum", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--expected-vault-root", type=str, default=None)
    parser.add_argument("--health-url", type=str, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    results = run_checks(
        install_dir=args.install_dir,
        exe_name=args.exe_name,
        artifact=args.artifact,
        checksum=args.checksum,
        config=args.config,
        expected_vault_root=args.expected_vault_root,
        health_url=args.health_url,
    )
    if not results:
        print("verify_install: no checks selected (pass at least one of --install-dir/--artifact/--config)")
        return 1
    passed = _print_report(results)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
