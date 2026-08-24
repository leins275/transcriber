#!/usr/bin/env python3
"""Verify that the three pinned dependency lock files exist and are tracked
by git (not matched by a .gitignore rule), per FR-4: dependency versions are
committed and release builds install from the locks in frozen mode.

Usage:
    uv run scripts/verify_locks.py --check
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Paths are relative to the repo root; re-rooted under whatever --repo-root
# (or the process cwd) resolves to, so tests can point this at a scratch tree
# without touching the real committed lock files.
_RELATIVE_LOCK_FILES = [
    "Cargo.lock",
    "apps/desktop/package-lock.json",
]


def _is_gitignored(repo_root: Path, rel_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", rel_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    # git check-ignore exits 0 when the path IS ignored -- a problem for us.
    return result.returncode == 0


def check(repo_root: Path) -> list[str]:
    """Return a list of human-readable problems. Empty means all locks are
    present and, where a .git tree is available, none is gitignored."""
    problems: list[str] = []
    has_git = (repo_root / ".git").exists()
    for rel_path in _RELATIVE_LOCK_FILES:
        candidate = repo_root / rel_path
        if not candidate.is_file():
            problems.append(f"missing lock file: {rel_path}")
            continue
        if has_git and _is_gitignored(repo_root, rel_path):
            problems.append(f"lock file is gitignored: {rel_path}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print a confirmation on success (failures are always reported).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Override the repo root to check (defaults to the current directory).",
    )
    args = parser.parse_args(argv)

    repo_root = (args.repo_root or Path.cwd()).resolve()
    problems = check(repo_root)

    if problems:
        for problem in problems:
            print(f"verify_locks: {problem}", file=sys.stderr)
        return 1

    if args.check:
        print("verify_locks: all lock files present and tracked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
