# Releasing

How a change gets from a commit to an installer someone can download.

## The short version

1. Open a PR; CI runs the checks.
2. Merge it into `main` with a [conventional commit](#commit-messages)
   subject.
3. If anything since the last tag is bump-worthy, CI writes the new version
   everywhere, regenerates `CHANGELOG.md`, commits `chore(release): X.Y.Z`
   to `main` and tags it `vX.Y.Z`.
4. The tag triggers the release build: the Windows installer (NSIS `.exe`)
   and the macOS Apple Silicon installer (`.dmg`) are built in parallel and
   one GitHub Release is published with both attached.

There is no release PR and no separate "ship it" step: **merging to `main`
is the ship decision.** A merge that contains only `docs:`/`chore:`-class
commits moves nothing; a merge with a `feat:` or `fix:` releases. To batch
several changes into one release, stack them in one branch and merge once.

## Commit messages

The version number is derived from commit subjects, so the subject line is
load-bearing:

| Subject starts with | Effect on the next version |
|---|---|
| `feat:` / `feat(scope):` | minor bump — `0.1.0` → `0.2.0` |
| `fix:`, `perf:` | patch bump — `0.1.0` → `0.1.1` |
| `feat!:` or a `BREAKING CHANGE:` footer | major bump — `0.1.0` → `1.0.0` |
| `docs:`, `test:`, `refactor:`, `chore:`, `ci:` | no bump; still appears in the changelog |

A subject that follows no convention at all is kept in the changelog under
**Other** and never moves the version — a subject nobody wrote to a
convention cannot be read as a promise about compatibility.

`feat` bumps the *minor* even below 1.0. That is deliberate: this app is
pre-1.0 and ships user-visible features regularly, and burying those in a
patch number would make the version say nothing.

## Who owns what

The pipeline has two halves that deliberately do not know about each other:

| Concern | Owner |
|---|---|
| *What* the next version is, and the changelog | `cliff.toml` (git-cliff, pinned in `scripts/prepare_release.py`) |
| *Where* the version is written | `version.txt` + `scripts/sync_version.py` |
| Joining the two | `scripts/prepare_release.py` |

`version.txt` remains the single source of truth. `sync_version.py`
propagates it into five manifests **and** both `Cargo.lock` workspace-member
entries — that last one matters, because the release build runs
`tauri build --locked` and cargo refuses a lockfile whose recorded member
version disagrees with its `Cargo.toml`.

`make lint` runs `sync_version.py --check`, so drift between any of those
files fails CI rather than surfacing later as a confusing lockfile error.

## The workflows

| File | Runs on | Does |
|---|---|---|
| `.github/workflows/ci.yml` | every PR; called by `tag.yml` for direct pushes | format / lint / type / test across Rust, TypeScript and Python, in four parallel jobs |
| `.github/workflows/tag.yml` | push to `main` | runs the CI gate first if the push was direct (a PR merge is already gated); then computes the next version; if there is one, commits the bump + changelog to `main` and pushes the `vX.Y.Z` tag |
| `.github/workflows/release.yml` | `v*` tag | builds both installers in parallel (`windows-latest` + `macos-14`), then a fan-in `publish` job creates the GitHub Release once with everything attached |

One hand-off in there is explicit rather than implicit: refs pushed with the
built-in `GITHUB_TOKEN` never trigger other workflows, so `tag.yml` ends by
dispatching `release.yml` at the tag itself. A `v*` tag pushed by hand (with
your own credentials) triggers `release.yml` the ordinary way.

CI runs mostly on Windows — the primary platform, whose code paths
(`runtime_dlls`, NSIS, the Windows process plumbing) the other runners
cannot exercise. Since the macOS target landed, the Windows-only Rust is
`#[cfg(windows)]`-gated and a small `rust-macos` job runs
`cargo check --workspace --all-targets` on an Apple Silicon runner, so a
Windows-focused PR cannot silently break the macOS release build's compile.

### The installers are the release

`release.yml` builds the two platforms as a matrix (`windows-latest` →
`Transcriber_<v>_x64-setup.exe`, `macos-14` → `Transcriber_<v>_aarch64.dmg`
plus the `Transcriber.app.tar.gz` updater archive) and will not publish a
release without both attached, checking that twice. Each leg resolves its
artifact name from `sync_version.artifact_name` — the same function
`build_installer.py` names the file with, so the workflow cannot drift from
the builder — and fails if the installer, its `.sha256` or
`build-manifest.json` is missing, empty, or implausibly small for a build
that exited 0. The release object is created exactly once, by a fan-in
`publish` job that runs only after both legs succeed — a failed leg leaves
no partial release. After publishing, it reads the release back from the API
and confirms the installers are actually attached, because
`gh release create` can create the release object and still have an upload
rejected.

The macOS app is not Apple-signed or notarized yet (no Developer ID), so
Gatekeeper blocks a plain double-click on first launch: right-click the app
→ Open → Open. The workflow marks where the Apple signing secrets slot in
once a certificate exists.

The tag now exists *before* the build, which is the price of the simple
tag-driven flow: an installer build that fails leaves a `vX.Y.Z` tag with no
release behind it. That state is visible (the Release workflow run is red)
and recoverable — re-run the Release workflow on the same tag. The `check`
job makes re-runs safe: a version that is already published is a no-op, a
tag whose tree disagrees with `version.txt` fails loudly, and a manual
dispatch aimed at a branch instead of a tag is refused.

### How `main` is gated without paying twice

A `pull_request` event builds `refs/pull/N/merge` — the branch already
merged into main. The PR run therefore tests the exact tree that merging
produces, and running the same gate again on the push to main would reach
the same answer at the cost of a second set of Windows runners.

So `ci.yml` does not trigger on `push: main` itself. Instead `tag.yml`,
which runs on every push to main, starts with a `gate` job that asks the
API whether the pushed commit is the merge result of a merged pull request
(`merge_commit_sha` match — reliable across merge, squash and rebase, where
a commit-message heuristic is not). A PR merge skips straight to tagging,
because its tree was already gated on the PR. A **direct push** bypassed
that, so `tag.yml` first calls `ci.yml` as a reusable workflow and only
tags if the full gate passes. Either way, no tag is cut from an untested
tree, and nothing is tested twice.

`tag.yml` also triggers on **state, not on a commit message**: git-cliff
reads the range since the most recent `v*` tag, so a squashed merge, a
rebase, or a re-run all compute the same answer, and a run that finds
nothing bump-worthy exits quietly. Two merges landing close together
serialize (the workflow's concurrency group), and the second run checks out
the tip of `main` — including the first run's bump commit and tag — so it
computes only what is left, never a version that is already taken.

## Updates

The installed app checks for a newer release once at launch and offers it;
nothing installs without a click. There is no background poll — this app is
opened to deal with a recording and then closed, and a timer would mean
network activity at a moment nobody asked for it.

How it fits together:

| Piece | Where |
|---|---|
| Signing keypair | private key in the `TAURI_SIGNING_PRIVATE_KEY` repo secret; public key in `tauri.conf.json` |
| Signed update artifacts + `.sig` | `createUpdaterArtifacts`, per platform: on Windows it signs the NSIS `-setup.exe` itself (Tauri v2 layout — no separate archive); on macOS it produces `Transcriber.app.tar.gz` + `.sig` (the `.dmg` is first-install media only) |
| `latest.json` | assembled by `release.yml`'s `publish` job with both platform entries (`windows-x86_64`, `darwin-aarch64`) and attached to the Release |
| The check | `state/useUpdate.ts` at launch, rendered by `UpdateNotice` |

`latest.json` is built in the publish job rather than by the bundler,
because only that job knows the URL the assets will end up at. It is written
from the signature files the builds actually produced, so a manifest can
never claim a signature that was not made for those bytes, and it is
assembled in Python rather than shell — `notes` is arbitrary prose from
CHANGELOG.md, and a manifest a client cannot parse reads as "no update
available" and fails silently forever.

Two things this deliberately does not do:

- **It is not code signing.** The minisign key proves an update came from
  this pipeline. It does nothing about SmartScreen, which will still warn on
  a fresh install — that needs a paid certificate.
- **There are no delta updates.** Each update downloads the whole ~90 MB
  installer. The model and the vault are untouched by it.

### If the signing key is ever lost

Every published `latest.json` was signed with it, and a client only accepts
an update whose signature matches the public key baked into the build it is
already running. Losing the key means shipping a new public key in a new
build, which existing installs cannot update themselves into — every user
reinstalls by hand once. Keep a copy somewhere durable, not only in the
repo secret.

## Running it by hand

```
make next-version     # what would the next release be called?
make release-prep     # write that version everywhere + regenerate CHANGELOG.md
make installer        # build dist/Transcriber_<version>_x64-setup.exe
                      # (on a Mac: dist/Transcriber_<version>_aarch64.dmg)
```

`make next-version` exits `3` when there is nothing to release. That is a
documented code, not a failure — see `scripts/prepare_release.py`.

## One-time setup on a fresh clone or a new remote

The bump is computed from the range since the most recent `v*` tag, so that
tag has to exist:

```
git tag -a v0.1.0 <sha of the commit that shipped 0.1.0> -m "Transcriber 0.1.0"
git push origin v0.1.0
```

`cliff.toml`'s `initial_tag` covers a clone that has no tags at all, but only
so the tooling fails with something intelligible rather than a tag-pattern
error — it is not a substitute for the real baseline tag.

Beyond the Tauri signing secrets (see [Updates](#updates)), the pipeline
needs no tokens: `tag.yml` and `release.yml` both use the workflow's own
`GITHUB_TOKEN`, with `contents: write` (both) and `actions: write`
(`tag.yml`, to dispatch the Release workflow) declared in the workflows
themselves.

## Runner cost

`ci.yml` is roughly 10–15 minutes on a warm Rust cache; `release.yml` is
longer (it bakes a relocatable CPython, installs the frozen dependency tree,
builds Rust in release mode and runs NSIS). Windows minutes bill at 2× on
private repositories — which is the main reason `tag.yml` runs on Linux and
the installer build happens only when a tag is actually cut rather than on
every merge.
