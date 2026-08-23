#!/usr/bin/env python
"""Compute the next version from conventional commits and write it everywhere.

The seam between the two halves of releasing:

    git-cliff              decides *what* the next version is, and writes
    (cliff.toml)           CHANGELOG.md, from conventional commits since the
                           most recent `v*` tag.

    sync_version.py        decides *where* the version lives -- version.txt
                           plus the five manifests and the two Cargo.lock
                           workspace-member entries.

Neither knows about the other, and this script is the only thing that does.
That split is why a version bump cannot drift: nothing but `sync_version.py`
ever writes a manifest, and nothing but git-cliff ever picks a number.

git-cliff alone is not the whole answer to "is a release due": its
`--bumped-version` treats *any* non-skipped commit as at least a patch, so a
main branch that gained only `ci:` / `test:` / `docs:` commits would release
a version whose changelog names nothing a user can see. This script
therefore adds the policy the pipeline documents: a release needs at least
one bump-worthy commit -- feat, fix, perf, revert, or anything marked
breaking -- since the last tag. Everything else exits 3, quietly.

Exit codes (distinct, so CI can branch on them without parsing stderr):

    0   a release is due; the next version was printed / written
    3   nothing to release -- no bump-worthy commit since the last tag
    2   bad usage
    4   git-cliff failed

Usage:
    uv run scripts/prepare_release.py --print-next
    uv run scripts/prepare_release.py --dry-run
    uv run scripts/prepare_release.py
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Pinned exactly like every other tool this repo shells out to: a release
# pipeline that silently changed its own version-bumping rules on an
# upstream release would be a genuinely bad surprise.
GIT_CLIFF = "git-cliff@2.11.0"

EXIT_NOTHING_TO_RELEASE = 3
EXIT_GIT_CLIFF_FAILED = 4


class PrepareReleaseError(Exception):
    """git-cliff could not be run, or answered with something unusable."""


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run git-cliff through `uvx`, so no global install is required.

    `uvx` is already this repo's way of reaching a tool it does not depend
    on at runtime (see the Makefile), and it resolves the pin above to the
    same binary on a developer's machine and on a CI runner.
    """
    return subprocess.run(
        ["uvx", GIT_CLIFF, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


Runner = "callable taking list[str] -> subprocess.CompletedProcess[str]"


def next_version(runner=_default_runner) -> str | None:
    """The version a release cut right now would carry, or `None`.

    `None` means git-cliff found nothing bump-worthy since the last tag --
    the normal state of a repository between releases, not an error.
    """
    result = runner(["--bumped-version"])
    if result.returncode != 0:
        combined = (result.stdout or "") + (result.stderr or "")
        if "nothing to bump" in combined.lower():
            return None
        raise PrepareReleaseError(
            f"git-cliff --bumped-version failed ({result.returncode}): {combined.strip()}"
        )

    # git-cliff reports "there is nothing to bump" on stderr and still exits
    # 0, echoing the *current* tag back. Both shapes mean the same thing.
    if "nothing to bump" in (result.stderr or "").lower():
        return None

    bumped = (result.stdout or "").strip().splitlines()
    if not bumped:
        raise PrepareReleaseError("git-cliff --bumped-version printed nothing")
    return bumped[-1].strip().lstrip("v")


# The commit types that justify a release. `revert` is here even though the
# usual shorthand is "feat/fix/perf/breaking": a revert worth pushing to main
# is undoing something already released, and holding it back until the next
# feature would leave the bad release as the latest one.
# `Revert "..."` is `git revert`'s own default subject, kept alongside the
# conventional `revert:` spelling.
_BUMP_WORTHY_SUBJECT = re.compile(r"^(feat|fix|perf|revert)(\([^)]*\))?!?:|^Revert \"")
# Any type at all becomes bump-worthy when marked breaking, `refactor!:`
# included -- a compatibility promise broken is a release however it is
# spelled.
_BREAKING_SUBJECT = re.compile(r"^\w+(\([^)]*\))?!:")


def _commit_messages_since_last_tag() -> list[str]:
    """Full messages of every commit after the most recent `v*` tag."""
    describe = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # No tag at all (a fresh fork, say): every commit is in range, which
    # matches how git-cliff reads the same history.
    rev_range = f"{describe.stdout.strip()}..HEAD" if describe.returncode == 0 else "HEAD"
    log = subprocess.run(
        ["git", "log", "--format=%B%x00", rev_range],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [message.strip() for message in log.stdout.split("\0") if message.strip()]


def has_bump_worthy_commit(messages: list[str] | None = None) -> bool:
    """Is any commit since the last tag reason enough to cut a release?

    git-cliff cannot answer this: its bump treats every non-skipped commit
    as at least a patch. The policy lives here instead, on the raw commit
    messages, so a `ci:`- or `docs:`-only main stays unreleased.
    """
    if messages is None:
        messages = _commit_messages_since_last_tag()
    for message in messages:
        subject = message.splitlines()[0]
        if _BUMP_WORTHY_SUBJECT.match(subject) or _BREAKING_SUBJECT.match(subject):
            return True
        # The footer spelling of a breaking change, per the conventional
        # commits spec.
        if "BREAKING CHANGE:" in message or "BREAKING-CHANGE:" in message:
            return True
    return False


def write_changelog(runner=_default_runner) -> None:
    """Regenerate CHANGELOG.md with the bumped version as its newest heading."""
    result = runner(["--bump", "-o", str(CHANGELOG)])
    if result.returncode != 0:
        combined = (result.stdout or "") + (result.stderr or "")
        raise PrepareReleaseError(
            f"git-cliff --bump failed ({result.returncode}): {combined.strip()}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-next",
        action="store_true",
        help="print the next version and change nothing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written, without writing it",
    )
    args = parser.parse_args(argv)

    try:
        version = next_version()
    except PrepareReleaseError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GIT_CLIFF_FAILED

    # git-cliff said a number, but a number is not yet a reason: see
    # `has_bump_worthy_commit`.
    if version is not None and not has_bump_worthy_commit():
        version = None

    if version is None:
        print(
            "nothing to release: no feat/fix/perf/revert/breaking commit since the last tag",
            file=sys.stderr,
        )
        return EXIT_NOTHING_TO_RELEASE

    if args.print_next:
        print(version)
        return 0

    if args.dry_run:
        print(f"would write version {version} to version.txt, the manifests and Cargo.lock")
        print(f"would regenerate {CHANGELOG.relative_to(REPO_ROOT)}")
        return 0

    # Imported here, not at module scope: `--print-next` must work even from
    # a checkout where sync_version's manifests are mid-edit.
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import sync_version  # noqa: PLC0415

    try:
        write_changelog()
    except PrepareReleaseError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GIT_CLIFF_FAILED

    sync_version.set_version(version)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
