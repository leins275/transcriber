"""Tests for the root Makefile's QA fanout (FR-2, NFR-6) and the `make -n`
detection probe used by the SDD pipeline (R6).

Repo root is derived relative to this file (parents[2]: scripts/tests/ -> scripts/ -> root),
per plan.md's rule that no shared conftest.py exists across scripts/tests/.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

FANOUT_TARGETS = ["format", "lint", "type", "test"]
PHONY_TARGETS = [*FANOUT_TARGETS, "installer", "bootstrap"]


def _makefile_text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _target_recipe(text: str, target: str) -> str:
    """Return the recipe block (indented lines) following a `target:` line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(target)}\s*:", line):
            recipe_lines = []
            for follow in lines[i + 1 :]:
                if follow.startswith("\t"):
                    recipe_lines.append(follow)
                elif follow.strip() == "":
                    continue
                else:
                    break
            return "\n".join(recipe_lines)
    raise AssertionError(f"target {target!r} not found in Makefile")


def test_makefile_exists() -> None:
    assert MAKEFILE.is_file()


def test_all_targets_declared_phony() -> None:
    text = _makefile_text()
    phony_lines = [line for line in text.splitlines() if line.strip().startswith(".PHONY")]
    declared = " ".join(phony_lines)
    for target in PHONY_TARGETS:
        assert re.search(rf"(?<![\w-]){re.escape(target)}(?![\w-])", declared), (
            f"{target!r} is not declared .PHONY"
        )


def test_all_targets_have_a_rule() -> None:
    text = _makefile_text()
    for target in PHONY_TARGETS:
        assert re.search(rf"^{re.escape(target)}\s*:", text, re.MULTILINE), (
            f"no rule found for target {target!r}"
        )


@pytest.mark.parametrize("target", FANOUT_TARGETS)
def test_fanout_target_invokes_all_three_languages(target: str) -> None:
    text = _makefile_text()
    recipe = _target_recipe(text, target)
    assert "cargo" in recipe, f"{target} recipe does not invoke cargo (Rust)"
    assert "npm --prefix apps/desktop" in recipe, f"{target} recipe does not invoke the app's npm scripts"
    assert "uv run --directory services/transcription" in recipe, (
        f"{target} recipe does not invoke uv for the Python service"
    )


@pytest.mark.parametrize("target", PHONY_TARGETS)
def test_make_dry_run_resolves(target: str) -> None:
    make = shutil.which("make") or shutil.which("mingw32-make")
    if make is None:
        pytest.skip("make is not on PATH on this machine (R6 -- bootstrap installs it)")
    result = subprocess.run(
        [make, "-n", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"make -n {target} failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
