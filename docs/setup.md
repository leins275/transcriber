# Setup — Transcriber desktop app

This is the setup guide for `apps/desktop` (Tauri 2 + React) and the Rust
workspace it lives in. It states what this feature's implementation actually
verified on the operator's machine — not a generic Tauri guide.

## Host prerequisites

| Prerequisite | State on this host | Notes |
|---|---|---|
| MSVC toolchain | present — `C:\Program Files\Microsoft Visual Studio\18\Community` | The spec's Problem & context section records this as `...\2022`; the directory actually present on this machine is `18\Community` (a newer Visual Studio release that uses `18` as its top-level version folder, not `2022`). Either MSVC edition works for the `x86_64-pc-windows-msvc` target; what matters is that the "Desktop development with C++" workload (link.exe, Windows SDK libs) is installed under it. |
| Windows SDK | present — `10.0.26100.0` (`C:\Program Files (x86)\Windows Kits\10\Include\10.0.26100.0`) | |
| WebView2 runtime | present — `151.0.4129.93` (`C:\Program Files (x86)\Microsoft\EdgeWebView\Application\151.0.4129.93`) | Tauri 2 on Windows renders through the installed WebView2 runtime; no bundled Chromium. |
| Node / npm | present — Node v22.17.1, npm 11.5.1 | `npm` is the package manager for this feature (not pnpm/bun, per FR-1); `bun` 1.3.14 is also present on this host but unused. |
| `uv` | present — `C:\Users\feitr\.local\bin\uv.exe` (`uv 0.8.17`) | Used to run F2 (`services/transcription`) as the dev-mode sidecar; see `docs/config-contract.md` for the exact command. |
| **rustup / cargo** | **installed** (`~/.cargo/bin`, `stable-x86_64-pc-windows-msvc`) as of this task | The spec recorded rustup as **absent** at spec time — "must be installed before any work starts". It has since been installed (verified: `rustup show` reports `stable-x86_64-pc-windows-msvc` as the active, default toolchain with that target). A clean checkout on a machine without it must still install it first; see below. |

### Installing rustup (only if `cargo`/`rustup` are not already on `PATH`)

1. Install the MSVC "Desktop development with C++" workload (Visual Studio
   Installer — Community edition is sufficient) if not already present.
2. Install `rustup` from https://rustup.rs (or the standalone
   `rustup-init.exe`) and select the **`stable-x86_64-pc-windows-msvc`**
   toolchain — this is the default on Windows and is what this workspace
   builds against; no other toolchain or target is required.
3. Confirm with `rustup show`: it should report
   `stable-x86_64-pc-windows-msvc` as the active, default toolchain with
   target `x86_64-pc-windows-msvc` installed.
4. Confirm `cargo --version` and `rustc --version` resolve on `PATH` (a new
   shell may be needed after install so `~/.cargo/bin` is picked up).

## Clean-checkout sequence to a running window (FR-2)

From the repository root:

```
git clone <repo-url>
cd transcriber
cargo build -p transcriber-desktop
cd apps/desktop
npm install
npm run tauri dev
```

- `cargo build -p transcriber-desktop` is optional as a standalone step —
  `npm run tauri dev` triggers the same Rust build via
  `beforeDevCommand`/the Tauri CLI — but running it first surfaces any
  toolchain problem with a plain Rust error instead of interleaved
  Rust+Vite output, and it is a useful first-run sanity check.
- The **first** `npm run tauri dev` (or `cargo build`) is slow — Tauri's
  dependency tree is large — this is minutes, not seconds, and is expected;
  it is a one-time cost per machine/target directory, not a violation of
  NFR-3 (NFR-3's 3 s cold-start budget is about the **built** app's launch
  time, not a `cargo`/`vite` cold compile).
- `npm run tauri dev` opens the `Transcriber` window. With no
  `%APPDATA%\com.transcriber.desktop\config.json` present yet, the app shows
  the first-run folder-picker state (FR-18) rather than a drop zone.
- No F2 (Python transcription service) process needs to be running or
  installed for this to work: `npm run tauri dev` spawns it as a background
  sidecar (see `docs/config-contract.md`); if it never becomes ready the app
  still opens and shows the service as unavailable rather than failing to
  start (FR-13, NFR-3).

## QA commands

See `apps/desktop/README.md` for the full FR-19 command reference
(`format`/`lint`/`type`/`test`, Rust and frontend). In short, from the
repository root:

```
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

and from `apps/desktop/`:

```
npm run format:check
npm run lint
npm run type
npm run test
```

All eight commands above were run against this checkout as part of this
task and passed cleanly (see `apps/desktop/README.md` for the one caveat
around `npm run format:check` and the gitignored `src-tauri/gen/` directory
that a prior `tauri dev`/`tauri build` leaves behind).

## Build and install (F4 — windows-installer-build)

This section was added once F4 landed a root `Makefile`, `scripts/`
bootstrap/build tooling and `installer/`. It does not replace the dev inner
loop above — `npm run tauri dev` is still how you iterate on the app itself
— this is the one-command path from a clean clone to an installed `.exe`.

### Requirements reinterpreted by F4's batch decisions

`specs/windows-installer-build/spec.md`'s **FR-10** (choose the vault root
during setup, via a folder picker) and **FR-12** (model download with
progress/resume/verify/cancel) both read, on their face, as installer
behaviour. Per the spec's Q1 decision (`A`, recorded in its Decisions log)
and `plan.md`'s "Model acquisition" section, neither happens inside the NSIS
installer:

- The installer (`installer/installer_hooks.nsh`) stays a stock Tauri NSIS
  bundle with no custom pages. It only re-validates and writes a vault path
  when one is supplied via the silent-mode `/VAULT=` argument (FR-18); there
  is no interactive folder-picker dialog inside setup itself.
- Both the interactive vault pick and the whisper model download happen
  **after** install, in the app's first-run wizard (`ModelDownloadStep`,
  T13) — the folder picker is a React step, and the download runs through
  F2's HTTP download endpoints (`POST`/`GET`/`DELETE /v1/model/download`,
  T11) wrapping `huggingface_hub.snapshot_download` (T10), not any
  installer-side transfer logic.
- This is a deliberate cost documented at the spec gate: it satisfies FR-10
  and FR-12's acceptance criteria (a folder picker with validation; a
  download with progress, resume, verify, cancel) through the product
  itself rather than through the setup executable, in exchange for keeping
  the installer template unforked and avoiding a multi-gigabyte transfer
  inside an installer transaction. See `docs/manual-smoke-checklist.md`'s
  installer checklist, step 9, and `installer/README.md` for the exact
  installer-side boundary this leaves.

### Bootstrap

```
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

`scripts/bootstrap.ps1` detects Rust, Node, npm, `uv`, GNU Make and the Tauri
CLI, installs whatever is missing (never elevating silently), and never
treats the Windows Store's `python` stub as a real interpreter — this
project's Python is entirely `uv`-managed (see `docs/config-contract.md`).
`-Check -Json` reports without installing and always exits 0; plain
`bootstrap.ps1` exits non-zero if a required tool is still missing after the
attempt. `make` itself is not on PATH until this has run once.

### Building the installer

```
make installer
```

is the `Makefile` wrapper (see the root `Makefile`'s own comments for every
target's direct, non-`make` equivalent — PowerShell 5.1 has no `&&`, so run
each line separately) for:

```
uv run scripts/sync_version.py --check
uv run scripts/verify_locks.py --check
uv run scripts/build_pyenv.py --out apps/desktop/src-tauri/resources/pyenv
npm --prefix apps/desktop ci
npm --prefix apps/desktop run tauri -- build -- --locked
```

No `--extra cuda`: the default bake is CPU-only (~414 MB, and NSIS-
compilable — see "Known gaps" below for why that matters); the CUDA
runtime is instead fetched at first run into the application folder's
`runtime\` (`transcription.cuda_runtime`, `docs/verification-installer.md`'s
"Blocker 1"). The two `--` before `--locked` are both required: the first
stops `npm` itself from parsing `--locked` as one of its own CLI flags, the
second is Tauri CLI's own marker for "forward this to the underlying
`cargo build`" (FR-4 — a drifted `Cargo.lock` must fail the build loudly,
never silently re-resolve).

`scripts/build_installer.py` (`make installer`'s actual recipe line, via
`uv run scripts/build_installer.py`) drives this same stage order —
version check → lock check → pyenv bake → tauri build → collect → a 1.5 GB
size gate (NFR-1) — non-interactively, aborting with a distinct exit code
per stage on failure. `--dry-run` prints every command it would run and
touches nothing. Output lands at a fixed path:

```
dist/Transcriber_<version>_x64-setup.exe
dist/Transcriber_<version>_x64-setup.exe.sha256
dist/build-manifest.json
```

`build-manifest.json` records the product version, git commit, and the
resolved versions of all three payloads (FR-15).

### Known gaps

- ~~`scripts/build_installer.py` invoked with no arguments ... bakes the
  pyenv to the repo-root `build/pyenv/` by default~~ **Fixed by T14**:
  `BuildContext`'s default `pyenv_out` (and `main()`'s default when
  `--pyenv-out` is omitted) now points at
  `apps/desktop/src-tauri/resources/pyenv` directly — verified via
  `uv run scripts/build_installer.py --dry-run`. See
  `docs/verification-installer.md` for the fix and its tests.
- The installed application-folder layout is `<install dir>\pyenv\python\`,
  `<install dir>\pyenv\site-packages\`, `<install dir>\pyenv\service\` —
  i.e. `bundle.resources` nests the baked tree directly under `pyenv\` at
  the install root, **not** under `<install dir>\resources\pyenv\...` as
  `plan.md`'s Architecture-overview prose states (that prose predates T6's
  actual `bundle.resources` target mapping). `apps/desktop/src-tauri/src/app_paths.rs`
  is the module that resolves this in Rust and is the authority if the two
  disagree again.
- ~~`installer/installer_hooks.nsh` has now been run through the real
  `makensis` compiler for the first time (T14) — and the real build cannot
  currently get past that step~~ **Fixed**: that failure (`Internal compiler
  error #12345: error mmapping datablock` against the ~2.33 GiB `--extra
  cuda` payload) was resolved by moving the CUDA runtime out of the
  installer and into a first-run download (see the CPU-only default bake
  above) — the default, non-CUDA bake (~414 MB) compiles cleanly.
  `docs/verification-installer.md`'s "Second pass" section records a real
  `dist/Transcriber_<version>_x64-setup.exe` built end to end and the full
  manual smoke checklist executed against it (install, upgrade, silent
  `/VAULT=`, uninstall vault-hash comparison). See "Blocker 1" in that same
  file for the original diagnosis this fixed, and "Blocker 2"/"Blocker 3"
  for two further, independent defects found in the model-download-to-GPU-
  inference path (also since fixed; see the eval/fix-pass history in that
  file for what remains only partially re-verified as of the current
  round).

### Bumping the product version

`version.txt` at the repo root is the single source of truth (FR-5); it
propagates to `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/package.json`,
`apps/desktop/src-tauri/Cargo.toml`, `crates/vault/Cargo.toml`,
`services/transcription/pyproject.toml`, and the installer artifact's
filename.

```
uv run scripts/sync_version.py --set 1.2.3
uv run scripts/sync_version.py --check
```

`--check` (what `make lint` runs) fails, naming the drifting manifest, if any
one of them is edited by hand instead of through `--set`.

## Known gaps

- `installer/`, `scripts/`, and packaging (`tauri.conf.json`'s bundle block
  beyond the identity fields fixed here) were F4's scope and have since
  landed — see "Build and install (F4)" above rather than treating them as
  outstanding.
