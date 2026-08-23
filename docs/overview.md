# Overview

A local, Windows-first desktop app that turns dropped meeting recordings into
transcripts (faster-whisper large-v3, CUDA-first) and files them into a
per-project meetings vault. See `../IDEA.md` for the original product idea.

The repository is a three-payload monorepo:

```
apps/desktop/            Tauri 2 + React desktop app
services/transcription/  Python (uv) transcription service (FastAPI + CLI)
crates/vault/             Rust library: naming/routing rules for the vault
installer/                NSIS installer hooks + resources
scripts/                  Bootstrap, build orchestration, version sync
docs/                     Setup, config contract, smoke checklists
```

## Build and install

This is the short path from a clean clone to an installed app. Full detail —
host prerequisites actually observed on this machine, the dev inner loop, and
every direct (non-`make`) command — lives in `setup.md`.

1. **Bootstrap the machine** (installs/report Rust, Node, `uv`, GNU Make, the
   Tauri CLI; never touches system `python`, which this project never uses):

   ```
   powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
   ```

   Run `scripts/bootstrap.ps1 -Check -Json` first if you just want a report
   (always exits 0). Plain `bootstrap.ps1` attempts installs and exits
   non-zero if a required tool is still missing afterwards.

2. **Build the installer**:

   ```
   make installer
   ```

   `make` is not installed until step 1 has run once. The direct equivalent,
   runnable without `make` (PowerShell 5.1 has no `&&` — run these one at a
   time, in order):

   ```
   uv run scripts/sync_version.py --check
   uv run scripts/verify_locks.py --check
   uv run scripts/build_pyenv.py --out apps/desktop/src-tauri/resources/pyenv
   npm --prefix apps/desktop ci
   npm --prefix apps/desktop run tauri -- build -- --locked
   ```

   No `--extra cuda`: the default bake ships CPU-only (~414 MB, NSIS-
   compilable); the CUDA runtime (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`)
   is instead fetched at first run into the application folder's `runtime\`
   (see "CUDA runtime" below). `--` `--locked` on the tauri build ensures a
   drifted `Cargo.lock` fails the build loudly instead of silently
   re-resolving (FR-4).

   `scripts/build_installer.py` (what `make installer` calls) drives exactly
   this sequence and then collects the output; `uv run
   scripts/build_installer.py` with no arguments already bakes the pyenv
   straight into `apps/desktop/src-tauri/resources/pyenv/`, the same
   directory the Tauri bundle config (`bundle.resources`) reads it from, so
   no explicit `--pyenv-out` is needed for a normal release build.

   Output, at a deterministic path:

   ```
   dist/Transcriber_<version>_x64-setup.exe
   dist/Transcriber_<version>_x64-setup.exe.sha256
   dist/build-manifest.json
   ```

3. **Install**: run the produced `.exe`. It installs per-user to
   `%LOCALAPPDATA%\Programs\Transcriber\` with no UAC prompt (see
   `../installer/README.md` for the hook contract and silent-mode arguments).

### CUDA runtime (first run, not baked)

The installer ships CPU-only. On first launch, if an NVIDIA GPU is present,
the app downloads the pinned `nvidia-cublas-cu12`/`nvidia-cudnn-cu12` wheels
(~1.4 GB) straight from PyPI into the application folder's `runtime\`, with
the same progress/resume/verify UI as the model download
(`transcription.cuda_runtime`). This replaced baking `--extra cuda` into the
pyenv: the vendored, 32-bit `makensis.exe` cannot compile the ~2.3 GiB
payload that produced. A machine with no NVIDIA GPU (or a failed/declined
CUDA download) transcribes on CPU instead — best-effort, not a broken
install.

## QA

```
make format
make lint
make type
make test
```

Each fans out across Rust (`cargo`), TypeScript (`npm --prefix apps/desktop`)
and Python (`uv run --directory services/transcription`), plus the
build-system's own Python test suite (`scripts/tests/`). `make lint` also
runs `scripts/sync_version.py --check` and `scripts/verify_locks.py --check`.
See the root `Makefile` for the literal recipe lines and their direct,
non-`make` equivalents (every target is documented that way, since `make`
itself is not installed on a fresh clone until bootstrap runs).

## Bumping the product version

`version.txt` at the repo root is the single source of truth (FR-5). It
propagates to the Tauri config, `apps/desktop/package.json`, both Rust
crates' `Cargo.toml`, `services/transcription/pyproject.toml`, and the
installer artifact's filename.

```
uv run scripts/sync_version.py --set 1.2.3
uv run scripts/sync_version.py --check
```

`--check` is what `make lint` runs; it fails, naming the drifting file, if
any one manifest is edited by hand without going through `--set`.

## Configuration

The app and the service share one settings file,
`%APPDATA%\com.transcriber.desktop\config.json`. See `config-contract.md`
for the schema, the sidecar environment handshake, and how this supersedes
this feature's own spec's original FR-11 text (a config file "in the
application folder").

## More documentation

- `setup.md` — host prerequisites, dev inner loop, and the build/install
  procedure in full.
- `config-contract.md` — the settings file schema and sidecar handshake.
- `manual-smoke-checklist.md` — the app-behavior checklist (F1's
  drag-drop flow) plus the installer-specific checklist executed by hand
  against a real install.
- `../installer/README.md` — the NSIS hook contract, the vault-safety invariant,
  and the silent-install arguments.
