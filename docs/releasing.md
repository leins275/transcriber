# Releasing

How a change gets from a commit to an installer someone can download.

## The short version

1. Merge work into `main` with a [conventional commit](#commit-messages)
   subject.
2. CI opens (or updates) a PR titled `chore(release): X.Y.Z`.
3. Merge that PR when you want to ship.
4. CI tags `vX.Y.Z`, builds the Windows installer, and publishes a GitHub
   Release with the installer attached.

Nothing releases without step 3. Every merge to `main` before that just
accumulates into the pending release PR.

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
| `.github/workflows/ci.yml` | every PR | format / lint / type / test across Rust, TypeScript and Python, in four parallel jobs |
| `.github/workflows/release-pr.yml` | push to `main` | computes the next version; opens or force-updates the `chore/release` PR |
| `.github/workflows/release.yml` | push to `main` | if `version.txt` has no matching `v*` tag: builds the installer, tags, publishes the Release |

CI runs on Windows only, and that is not caution: the Rust side imports
`std::os::windows::process::CommandExt` and shells out to `explorer.exe`, so
it does not compile on Linux at all.

### The installer is the release

`release.yml` will not publish a release without the installer attached, and
checks that twice. Before tagging, it resolves the artifact name from
`sync_version.artifact_name` — the same function `build_installer.py` names
the file with, so the workflow cannot drift from the builder — and fails if
the `.exe`, its `.sha256` or `build-manifest.json` is missing, empty, or
implausibly small for an NSIS build that exited 0. After publishing, it reads
the release back from the API and confirms the installer is actually
attached, because `gh release create` can create the release object and
still have an upload rejected.

The first check sits *before* the tag push, for the same reason the tag comes
after the build: a tag pointing at a release with no installer makes a
version look shipped when nothing is installable. The second necessarily runs
after publishing — it is the one that catches an upload the API accepted the
release for and then dropped.

### Why CI does not run on `main`

A `pull_request` event builds `refs/pull/N/merge` — the branch already
merged into main. The PR run therefore tests the exact tree that merging
produces, and running the same gate again on the push to main reaches the
same answer at the cost of a second set of Windows runners.

That leaves one real gap: anything pushed straight to `main`, bypassing a
pull request, is never gated. **Close it with branch protection**, not by
paying for every run twice:

```
gh api -X PUT repos/:owner/:repo/branches/main/protection   -F required_pull_request_reviews.required_approving_review_count=0   -F required_status_checks.strict=true   -F 'required_status_checks.contexts[]=Rust'   -F 'required_status_checks.contexts[]=Frontend'   -F 'required_status_checks.contexts[]=Python service'   -F 'required_status_checks.contexts[]=Build system'   -F enforce_admins=false   -F restrictions=null
```

`strict=true` is the load-bearing part: it requires a branch to be up to
date with main before merging, which is what makes "the PR run tested the
merge result" true rather than merely usually true. Without it, main can
move after a PR is validated and the merge produces a tree nothing built.

`release.yml` triggers on **state, not on a commit message**: it reads
`version.txt` and asks whether `v<version>` is already tagged. A squashed
merge, a rebase, a hand-edited `version.txt` or a re-run all behave the same
way, and re-running it on an already-released commit is a no-op rather than a
duplicate release. It also tags *after* the build succeeds — a tag on a
commit whose installer never built is worse than no tag, because it makes a
version look released when nothing exists to install.

## Updates

The installed app checks for a newer release once at launch and offers it;
nothing installs without a click. There is no background poll — this app is
opened to deal with a recording and then closed, and a timer would mean
network activity at a moment nobody asked for it.

How it fits together:

| Piece | Where |
|---|---|
| Signing keypair | private key in the `TAURI_SIGNING_PRIVATE_KEY` repo secret; public key in `tauri.conf.json` |
| Signed archive + `.sig` | produced by `createUpdaterArtifacts` during the normal NSIS build |
| `latest.json` | assembled by `release.yml` and attached to the Release |
| The check | `state/useUpdate.ts` at launch, rendered by `UpdateNotice` |

`latest.json` is built in the release job rather than by the bundler,
because only that job knows the URL the assets will end up at. It is written
from the signature file the build actually produced, so a manifest can never
claim a signature that was not made for those bytes, and it is assembled in
Python rather than shell — `notes` is arbitrary prose from CHANGELOG.md, and
a manifest a client cannot parse reads as "no update available" and fails
silently forever.

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

`release.yml` needs no secrets: it uses the workflow's own `GITHUB_TOKEN`.
`release-pr.yml` needs **Settings → Actions → General → Workflow permissions
→ Allow GitHub Actions to create and approve pull requests**, or its
`create-pull-request` step is refused.

## Runner cost

`ci.yml` is roughly 10–15 minutes on a warm Rust cache; `release.yml` is
longer (it bakes a relocatable CPython, installs the frozen dependency tree,
builds Rust in release mode and runs NSIS). Windows minutes bill at 2× on
private repositories — which is the main reason `release-pr.yml` runs on
Linux and the installer build happens once per release rather than once per
merge.
