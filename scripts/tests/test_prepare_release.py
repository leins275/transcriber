"""Tests for scripts/prepare_release.py (conventional commits -> next version).

Self-contained by design, like `test_version.py`: no shared conftest, and no
test here runs the real git-cliff or touches the real repository. Every case
injects a fake runner, because what is worth pinning is how this script reads
git-cliff's answers — including the two different shapes it uses to say
"nothing to bump" — not git-cliff's own behaviour.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "prepare_release.py"

_spec = importlib.util.spec_from_file_location("prepare_release", SCRIPT)
prepare_release = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["prepare_release"] = prepare_release
_spec.loader.exec_module(prepare_release)


def _runner(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    """A fake git-cliff that answers with a fixed result, recording its args."""
    calls: list[list[str]] = []

    def run(args: list[str]) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_next_version_strips_the_tag_prefix():
    run = _runner(stdout="v0.2.0\n")

    assert prepare_release.next_version(run) == "0.2.0"
    assert run.calls == [["--bumped-version"]]


def test_next_version_handles_a_version_without_a_prefix():
    assert prepare_release.next_version(_runner(stdout="1.4.2\n")) == "1.4.2"


def test_nothing_to_bump_on_stderr_with_a_zero_exit_is_not_a_release():
    # git-cliff's usual shape: exit 0, a warning on stderr, and the *current*
    # tag echoed back on stdout. Read as a version, that would silently
    # re-release the version already out.
    run = _runner(stdout="v0.1.0\n", stderr=" WARN  git_cliff > There is nothing to bump\n")

    assert prepare_release.next_version(run) is None


def test_nothing_to_bump_with_a_nonzero_exit_is_not_a_release():
    run = _runner(stderr="Error: there is nothing to bump\n", returncode=1)

    assert prepare_release.next_version(run) is None


def test_a_real_git_cliff_failure_is_raised_not_swallowed():
    run = _runner(stderr="Changelog error: does not match the tag pattern\n", returncode=1)

    with pytest.raises(prepare_release.PrepareReleaseError) as excinfo:
        prepare_release.next_version(run)

    assert "tag pattern" in str(excinfo.value)


def test_empty_output_is_an_error_rather_than_an_empty_version():
    with pytest.raises(prepare_release.PrepareReleaseError):
        prepare_release.next_version(_runner(stdout="   \n"))


def test_write_changelog_targets_the_repo_changelog():
    run = _runner()

    prepare_release.write_changelog(run)

    assert run.calls == [["--bump", "-o", str(prepare_release.CHANGELOG)]]


def test_write_changelog_raises_on_failure():
    with pytest.raises(prepare_release.PrepareReleaseError):
        prepare_release.write_changelog(_runner(stderr="boom", returncode=1))


def test_print_next_prints_only_the_version(monkeypatch, capsys):
    monkeypatch.setattr(prepare_release, "next_version", lambda: "0.2.0")
    monkeypatch.setattr(prepare_release, "has_bump_worthy_commit", lambda: True)

    assert prepare_release.main(["--print-next"]) == 0
    assert capsys.readouterr().out.strip() == "0.2.0"


def test_nothing_to_release_exits_with_its_own_documented_code(monkeypatch):
    monkeypatch.setattr(prepare_release, "next_version", lambda: None)

    assert prepare_release.main(["--print-next"]) == prepare_release.EXIT_NOTHING_TO_RELEASE


def test_dry_run_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(prepare_release, "next_version", lambda: "0.2.0")
    monkeypatch.setattr(prepare_release, "has_bump_worthy_commit", lambda: True)
    monkeypatch.setattr(
        prepare_release,
        "write_changelog",
        lambda *a, **k: pytest.fail("--dry-run must not write the changelog"),
    )

    assert prepare_release.main(["--dry-run"]) == 0
    assert "0.2.0" in capsys.readouterr().out


def test_a_git_cliff_failure_exits_with_its_own_documented_code(monkeypatch):
    def boom():
        raise prepare_release.PrepareReleaseError("git-cliff exploded")

    monkeypatch.setattr(prepare_release, "next_version", boom)

    assert prepare_release.main([]) == prepare_release.EXIT_GIT_CLIFF_FAILED


@pytest.mark.parametrize(
    "subject",
    [
        "feat: add a settings page",
        "feat(desktop): add a settings page",
        "fix(updater): stop the sidecar before installing",
        "perf: batch the inference pipeline",
        "revert: feat: add a settings page",
        'Revert "feat: add a settings page"',
        "refactor!: rename the vault manifest keys",
        "chore(deps)!: drop the legacy updater layout",
    ],
)
def test_bump_worthy_subjects_release(subject):
    assert prepare_release.has_bump_worthy_commit([subject])


@pytest.mark.parametrize(
    "subject",
    [
        "ci: gate direct pushes to main",
        "test(vault): steady the timing test",
        "docs: move README content to docs/overview.md",
        "chore(release): 0.4.0",
        "refactor(desktop): split the settings page",
        "build: pin git-cliff",
        "wip(vault): spike the parser",
        "Merge pull request #13 from leins275/feat/settings-page-updater-fix",
        # A bump-worthy *word* inside the description must not count -- only
        # the type prefix does.
        "docs: describe the fix: prefix convention",
    ],
)
def test_everything_else_is_not_a_release(subject):
    assert not prepare_release.has_bump_worthy_commit([subject])


def test_a_breaking_change_footer_releases_whatever_the_type():
    message = "chore: swap the update manifest format\n\nBREAKING CHANGE: old clients cannot parse it"

    assert prepare_release.has_bump_worthy_commit([message])


def test_no_commits_at_all_is_not_a_release():
    assert not prepare_release.has_bump_worthy_commit([])


def test_a_version_without_a_bump_worthy_commit_exits_nothing_to_release(monkeypatch):
    # The exact incident this guards: git-cliff happily bumps a patch for a
    # ci:-only main, and the policy layer must overrule it.
    monkeypatch.setattr(prepare_release, "next_version", lambda: "0.4.1")
    monkeypatch.setattr(prepare_release, "has_bump_worthy_commit", lambda: False)

    assert prepare_release.main(["--print-next"]) == prepare_release.EXIT_NOTHING_TO_RELEASE


def test_git_cliff_is_pinned_to_an_exact_version():
    # An unpinned release tool could change its own bump rules underneath
    # this pipeline between two releases.
    assert "@" in prepare_release.GIT_CLIFF
    assert prepare_release.GIT_CLIFF.split("@", 1)[1][0].isdigit()
