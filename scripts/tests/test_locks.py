"""Tests for lock-file presence and scripts/verify_locks.py.

Repo root is derived relative to this file (parents[2]: scripts/tests/ -> scripts/ -> root),
per plan.md's rule that no shared conftest.py exists across scripts/tests/.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_LOCKS = REPO_ROOT / "scripts" / "verify_locks.py"

LOCK_FILES = [
    REPO_ROOT / "Cargo.lock",
    REPO_ROOT / "apps" / "desktop" / "package-lock.json",
]


def _run_check(cwd: Path = REPO_ROOT, extra_args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(VERIFY_LOCKS), "--check", *(extra_args or [])]
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def test_all_lock_files_exist() -> None:
    for lock in LOCK_FILES:
        assert lock.is_file(), f"expected lock file to exist: {lock}"


def test_no_lock_file_is_gitignored() -> None:
    for lock in LOCK_FILES:
        rel = lock.relative_to(REPO_ROOT).as_posix()
        result = subprocess.run(
            ["git", "check-ignore", rel],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        # git check-ignore exits 1 when the path is NOT ignored (what we want).
        # Exit 0 means it IS ignored -- a failure for this test.
        assert result.returncode != 0, f"{rel} is matched by a .gitignore rule: {result.stdout}"


def test_verify_locks_check_exits_zero_on_committed_tree() -> None:
    result = _run_check()
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_verify_locks_check_exits_nonzero_when_a_lock_is_missing(tmp_path: Path) -> None:
    # Copy the repo's relevant lock-bearing directories into a scratch tree,
    # then delete one lock file, so we never touch the real committed tree.
    import shutil

    scratch = tmp_path / "repo"
    (scratch / "apps" / "desktop").mkdir(parents=True)

    # Cargo.lock only: the frontend's lock is deliberately left out, which is
    # the missing file this asserts the check refuses to pass.
    shutil.copy(REPO_ROOT / "Cargo.lock", scratch / "Cargo.lock")

    result = _run_check(cwd=scratch)
    assert result.returncode != 0
