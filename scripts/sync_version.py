"""Product version single source of truth (FR-5).

``version.txt`` at the repo root is the single source of truth. This script
propagates it into the five downstream manifests that carry their own copy
of the version and never edits anything else in those files: it rewrites
only the ``version`` field, preserving key order, indentation and any other
content byte-for-byte.

The two lockfiles carry their own copies as well -- ``Cargo.lock`` has one
``version`` line per workspace member, and ``services/transcription/uv.lock``
one for the ``transcription`` project -- and both are synced here. That is
not tidiness. The release build runs ``tauri build --locked`` and
``uv export --frozen``, and both tools refuse a lockfile whose recorded
version disagrees with the manifest beside it. Without this, every version
bump breaks the installer build with an error ("the lock file needs to be
updated") that names neither this script nor the bump that caused it --
which is exactly how it was found.

Usage:
    uv run scripts/sync_version.py --check
    uv run scripts/sync_version.py --set 1.2.3
    uv run scripts/sync_version.py --print
    uv run scripts/sync_version.py --print-artifact-name
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "version.txt"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Manifest:
    path: Path
    kind: str  # "json" | "toml"
    section: str | None = None  # only meaningful for kind == "toml"


@dataclass(frozen=True)
class LockPackage:
    """One ``[[package]]`` entry in a lockfile, for a locally-owned package.

    Cargo and uv write the same two-line shape here (``name = "..."`` then
    ``version = "..."``), which is why one rewriter serves both.
    """

    path: Path
    name: str


CARGO_LOCK = REPO_ROOT / "Cargo.lock"
UV_LOCK = REPO_ROOT / "services/transcription/uv.lock"

# Only packages this repository owns. Every other entry in either lockfile
# is a third-party dependency whose version has nothing to do with the
# product version, and must never be touched.
LOCK_PACKAGES: list[LockPackage] = [
    LockPackage(CARGO_LOCK, "transcriber-desktop"),
    LockPackage(CARGO_LOCK, "vault"),
    LockPackage(UV_LOCK, "transcription"),
]


MANIFESTS: list[Manifest] = [
    Manifest(REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json", "json"),
    Manifest(REPO_ROOT / "apps/desktop/package.json", "json"),
    Manifest(REPO_ROOT / "apps/desktop/src-tauri/Cargo.toml", "toml", "package"),
    Manifest(REPO_ROOT / "crates/vault/Cargo.toml", "toml", "package"),
    Manifest(REPO_ROOT / "services/transcription/pyproject.toml", "toml", "project"),
]

_JSON_VERSION_RE = re.compile(r'("version"\s*:\s*)"([^"]*)"')
_TOML_VERSION_LINE_RE = re.compile(r'(?m)^version\s*=\s*"([^"]*)"\s*$')
_TOML_SECTION_HEADER_RE = re.compile(r"(?m)^\[(?P<name>[^\]]+)\]\s*$")


def _lock_version_re(name: str) -> re.Pattern[str]:
    """Match the ``version`` line that directly follows ``name = "<name>"``.

    Anchored on the name line rather than searched for independently, so a
    registry crate that happens to share a prefix (or a `version` line
    belonging to some other `[[package]]`) can never be rewritten. Tolerates
    either newline convention: cargo writes LF, but git checks this file out
    as CRLF on Windows.
    """
    return re.compile(rf'(?m)^(name = "{re.escape(name)}"\r?\n' r'version = ")([^"]*)(")')


def read_version() -> str:
    """Read version.txt (the single source of truth)."""
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _section_span(text: str, section: str) -> tuple[int, int]:
    """Return the (start, end) character offsets of the body of ``[section]``."""
    headers = list(_TOML_SECTION_HEADER_RE.finditer(text))
    for index, match in enumerate(headers):
        if match.group("name").strip() == section:
            start = match.end()
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            return start, end
    raise ValueError(f"no [{section}] section found")


def _detect_newline(path: Path) -> str:
    """Sniff whether ``path`` currently uses CRLF or LF line endings from its
    raw bytes (E15), so a rewrite can reproduce that same convention exactly
    instead of ``Path.write_text``'s platform-default translation --
    Windows's default silently turns every ``\\n`` this script writes into
    ``\\r\\n``, which fights Prettier's LF preference on the JSON manifests
    this script also rewrites (``package.json``, ``tauri.conf.json``) and
    flips them back to CRLF on every version bump."""
    raw = path.read_bytes()
    return "\r\n" if b"\r\n" in raw else "\n"


def _read_json_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = _JSON_VERSION_RE.search(text)
    if not match:
        raise ValueError(f'no "version" field found in {path}')
    return match.group(2)


def _write_json_version(path: Path, version: str) -> None:
    newline = _detect_newline(path)
    text = path.read_text(encoding="utf-8")
    new_text, count = _JSON_VERSION_RE.subn(rf'\g<1>"{version}"', text, count=1)
    if count == 0:
        raise ValueError(f'no "version" field found in {path}')
    path.write_text(new_text, encoding="utf-8", newline=newline)


def _read_toml_version(path: Path, section: str) -> str:
    text = path.read_text(encoding="utf-8")
    start, end = _section_span(text, section)
    match = _TOML_VERSION_LINE_RE.search(text[start:end])
    if not match:
        raise ValueError(f"no version field in [{section}] of {path}")
    return match.group(1)


def _write_toml_version(path: Path, section: str, version: str) -> None:
    newline = _detect_newline(path)
    text = path.read_text(encoding="utf-8")
    start, end = _section_span(text, section)
    body = text[start:end]
    if _TOML_VERSION_LINE_RE.search(body):
        new_body = _TOML_VERSION_LINE_RE.sub(f'version = "{version}"', body, count=1)
    else:
        # No existing version field in this section: insert one as the first
        # line of the section body, right after the header.
        new_body = f'\nversion = "{version}"' + body
    path.write_text(text[:start] + new_body + text[end:], encoding="utf-8", newline=newline)


def lock_version(package: LockPackage) -> str:
    text = package.path.read_text(encoding="utf-8")
    match = _lock_version_re(package.name).search(text)
    if not match:
        raise ValueError(f'no [[package]] entry for "{package.name}" in {package.path}')
    return match.group(2)


def set_lock_version(package: LockPackage, version: str) -> None:
    newline = _detect_newline(package.path)
    text = package.path.read_text(encoding="utf-8")
    new_text, count = _lock_version_re(package.name).subn(rf"\g<1>{version}\g<3>", text, count=1)
    if count == 0:
        raise ValueError(f'no [[package]] entry for "{package.name}" in {package.path}')
    package.path.write_text(new_text, encoding="utf-8", newline=newline)


def _lock_label(package: LockPackage) -> str:
    """How a drifting lock entry is named in ``--check``'s output."""
    return f"{package.path.relative_to(REPO_ROOT).as_posix()} ({package.name})"


def manifest_version(manifest: Manifest) -> str:
    if manifest.kind == "json":
        return _read_json_version(manifest.path)
    assert manifest.section is not None
    return _read_toml_version(manifest.path, manifest.section)


def set_manifest_version(manifest: Manifest, version: str) -> None:
    if manifest.kind == "json":
        _write_json_version(manifest.path, version)
    else:
        assert manifest.section is not None
        _write_toml_version(manifest.path, manifest.section, version)


def check(version: str | None = None) -> list[str]:
    """Return the manifests (relative paths) whose version drifts, or []."""
    expected = version if version is not None else read_version()
    drifting: list[str] = []
    for manifest in MANIFESTS:
        try:
            actual = manifest_version(manifest)
        except (OSError, ValueError):
            drifting.append(str(manifest.path.relative_to(REPO_ROOT)))
            continue
        if actual != expected:
            drifting.append(str(manifest.path.relative_to(REPO_ROOT)))
    for package in LOCK_PACKAGES:
        try:
            actual = lock_version(package)
        except (OSError, ValueError):
            drifting.append(_lock_label(package))
            continue
        if actual != expected:
            drifting.append(_lock_label(package))
    return drifting


def set_version(version: str) -> None:
    if not SEMVER_RE.match(version):
        raise ValueError(f"not a semver: {version!r}")
    # E15: preserve version.txt's own existing newline convention too (it is
    # committed CRLF on Windows), falling back to LF the one time it does
    # not exist yet.
    newline = _detect_newline(VERSION_FILE) if VERSION_FILE.is_file() else "\n"
    VERSION_FILE.write_text(version + "\n", encoding="utf-8", newline=newline)
    for manifest in MANIFESTS:
        set_manifest_version(manifest, version)
    for package in LOCK_PACKAGES:
        set_lock_version(package, version)


def artifact_name(version: str | None = None) -> str:
    resolved = version if version is not None else read_version()
    return f"Transcriber_{resolved}_x64-setup.exe"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="exit non-zero on drift")
    group.add_argument("--set", metavar="X.Y.Z", help="write this version everywhere")
    group.add_argument("--print", action="store_true", help="print version.txt")
    group.add_argument(
        "--print-artifact-name",
        action="store_true",
        help="print the installer filename",
    )
    args = parser.parse_args(argv)

    if args.set is not None:
        if not SEMVER_RE.match(args.set):
            print(f"not a semver: {args.set!r}", file=sys.stderr)
            return 2
        set_version(args.set)
        print(read_version())
        return 0

    if args.print:
        print(read_version())
        return 0

    if args.print_artifact_name:
        print(artifact_name())
        return 0

    # --check
    drifting = check()
    if drifting:
        print("version drift detected against version.txt in:", file=sys.stderr)
        for path in drifting:
            print(f"  {path}", file=sys.stderr)
        return 1
    print(f"all manifests synced at {read_version()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
