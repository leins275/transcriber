"""Path allowlist, traversal, UNC and symlink-escape defence (FR-9).

Every path this service touches -- an incoming ``audio_path`` or
``output_dir`` -- is resolved to an absolute, symlink-free real path and
checked against a configured allowlist of roots before any filesystem
operation happens. Rejections never echo more of the attacker-supplied
input than its basename, and never touch the filesystem for syntactically
malformed input (UNC paths, device paths, stray colons).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from transcription.errors import ErrorKind, ServiceError


def _basename(raw: str) -> str:
    """The last path component of ``raw``, safe to put in an error message."""
    return Path(raw).name or raw


def _reject_syntax(raw: str) -> None:
    """Reject UNC paths, device paths and stray colons before touching disk."""
    if not raw:
        raise ServiceError(ErrorKind.INVALID_REQUEST, "path must not be empty")

    if raw.startswith(("\\\\", "//")):
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            f"UNC and device paths are not allowed: {_basename(raw)}",
        )

    # A single colon at index 1 is a normal drive letter ("C:"); any other
    # colon is an NTFS alternate-data-stream separator or otherwise
    # malformed input and must be rejected outright.
    colon_positions = [i for i, ch in enumerate(raw) if ch == ":"]
    if any(pos != 1 for pos in colon_positions):
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            f"invalid path: {_basename(raw)}",
        )


def _root_containing(resolved: Path, roots: Sequence[Path]) -> Path | None:
    """Return the root ``resolved`` lies under, or ``None`` if it escapes all of them."""
    normalized_target = os.path.normcase(str(resolved))
    for root in roots:
        root_resolved = Path(root).resolve()
        normalized_root = os.path.normcase(str(root_resolved))
        try:
            common = os.path.commonpath([normalized_target, normalized_root])
        except ValueError:
            # Different drives on Windows -- definitely not contained.
            continue
        if common == normalized_root:
            return root_resolved
    return None


def resolve_under_roots(
    candidate: str | Path,
    roots: Sequence[Path],
    *,
    must_exist: bool,
) -> Path:
    """Resolve ``candidate`` and verify it lies under one of ``roots``.

    Raises ``ServiceError(ErrorKind.INVALID_REQUEST)`` for empty input, UNC
    or device paths, stray colons, a missing file when ``must_exist`` is
    True, or a resolved target (post symlink-resolution) that escapes every
    configured root. An empty ``roots`` sequence rejects everything.
    """
    raw = str(candidate)
    _reject_syntax(raw)

    if not roots:
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            "no allowed roots are configured",
        )

    try:
        resolved = Path(raw).resolve(strict=must_exist)
    except OSError as exc:
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            f"path does not exist: {_basename(raw)}",
        ) from exc

    if _root_containing(resolved, roots) is None:
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            f"path escapes the allowed roots: {_basename(raw)}",
        )

    return resolved


def ensure_output_dir(candidate: str | Path, roots: Sequence[Path]) -> Path:
    """Resolve ``candidate`` under ``roots`` and create it if missing.

    The containment check happens before any directory is created, so a
    path outside every root is rejected without creating anything.
    """
    raw = str(candidate)
    _reject_syntax(raw)

    if not roots:
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            "no allowed roots are configured",
        )

    resolved = Path(raw).resolve(strict=False)

    if _root_containing(resolved, roots) is None:
        raise ServiceError(
            ErrorKind.INVALID_REQUEST,
            f"output directory escapes the allowed roots: {_basename(raw)}",
        )

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
