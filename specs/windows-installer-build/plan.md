---
slug: windows-installer-build
status: approved   # draft | approved
base_ref: <git sha, recorded at plan approval>
---

# Plan: Windows installer and build system

## Architecture overview

This feature is the delivery layer over three payloads that already exist on this branch (F1+F2+F3 merged). It adds no product capability of its own except the model acquisition path; everything else is orchestration, bundling and packaging.

### Repo layout (batch-fixed, binding)

```
transcriber/
  Makefile                  <- THIS feature (QA fanout + build entry points)
  version.txt               <- THIS feature (single source of truth, FR-5)
  Cargo.toml                <- F3 (workspace root: crates/vault + apps/desktop/src-tauri)
  crates/vault/             <- F1 (Rust library crate)
  services/transcription/   <- F2 (Python package, uv, pyproject.toml + uv.lock)
  apps/desktop/             <- F3 (package.json + src/ React) and src-tauri/ (Rust)
  installer/                <- THIS feature (NSIS hook script + installer resources)
  scripts/                  <- THIS feature (bootstrap, build, pyenv bake, verification)
  docs/                     <- THIS feature (build/install docs, config contract, smoke checklist)
```

**Path caveat, binding on every task**: this plan was written before F1/F2/F3 plans existed, so the leaf file names inside `crates/vault/`, `services/transcription/src/transcription/` and `apps/desktop/src-tauri/src/` are the spec-implied names, not verified ones. Every implementer's first action is to list the real tree and map its declared **Files** onto the actual module names, staying strictly inside the declared *directories*. The directory-level contract is what makes the waves safe; a leaf rename inside a declared directory is allowed, moving into an undeclared directory is not.

### Build pipeline (FR-6)

```
make installer
  └─ scripts/build_installer.py            (non-interactive, deterministic output)
       1. scripts/sync_version.py --check     version.txt -> all 4 manifests   (FR-5)
       2. scripts/verify_locks.py --check     locks committed + frozen         (FR-4)
       3. scripts/build_pyenv.py              bake relocatable uv env          (Q2-A)
            uv python install 3.12
            uv export --frozen --no-dev  ->  uv pip install --python <baked> --target
            output: apps/desktop/src-tauri/resources/pyenv/{python/, site-packages/}
            plus:   apps/desktop/src-tauri/resources/service/  (F2 source tree)
       4. cargo/npm: npm --prefix apps/desktop run tauri build (frozen installs)
       5. collect  -> dist/Transcriber_<version>_x64-setup.exe
                      dist/Transcriber_<version>_x64-setup.exe.sha256   (FR-15)
                      dist/build-manifest.json                          (FR-15)
       6. gate: installer size <= 1.5 GB                                (NFR-1)
```

### Installed layout (per-user, no UAC — Q4-A)

```
%LOCALAPPDATA%\Programs\Transcriber\
  Transcriber.exe                app + webview assets
  resources\pyenv\python\        baked CPython 3.12 (uv-managed, relocated)
  resources\pyenv\site-packages\ frozen deps incl. nvidia-cublas-cu12 / nvidia-cudnn-cu12
  resources\service\             F2 source tree
  models\   logs\   data\        created by the installer hook, writable, empty (FR-8)
  uninstall.exe
%APPDATA%\<bundle-identifier>\config.json     settings (F3 owns the schema)
```

### Configuration contract (supersedes spec FR-11)

Authoritative per the batch decision: **one** JSON file, `%APPDATA%\<bundle-identifier>\config.json`, schema owned by F3 (its FR-17: at minimum `meetings_root` and the service base URL, plus a schema version). This feature does **not** introduce a second config file in the application folder. Instead:

- The app resolves the application folder (`app_paths` module, T9) and passes it to the sidecar explicitly on spawn: `--config <%APPDATA%\...\config.json>` plus `TRANSCRIBER_APP_DIR=<app folder>` and `TRANSCRIBER_MODEL_PATH=<app folder>\models\faster-whisper-large-v3` (F2's FR-16 already accepts a config path and `TRANSCRIBER_*` overrides).
- The service never guesses the app folder; the parent passes it. Fallback, only when the env var is absent (developer runs the service standalone): the directory of the running executable.
- The installer's silent mode (`/VAULT=`) writes `meetings_root` into that same `%APPDATA%` file (T7), so a silent install lands in exactly the state the in-app wizard would produce.

### Model acquisition (Q1-A + batch decision)

The installer is a **stock Tauri NSIS bundle** — no forked template, no custom pages. Weights are fetched on first run by the app, through the Python side:

```
React first-run wizard step        Rust commands           F2 HTTP (loopback + token)
  ModelDownloadStep         ->  model_download_start   ->  POST   /v1/model/download
  (bytes, %, cancel, retry) <-  model_download_status  <-  GET    /v1/model/download
                            ->  model_download_cancel  ->  DELETE /v1/model/download
                                                            |
                                          huggingface_hub.snapshot_download(
                                            "Systran/faster-whisper-large-v3",
                                            local_dir=<app folder>/models/...)
```

`huggingface_hub` gives resume (`.incomplete` blobs), per-file digest verification and cancellation for free (NFR-3); we add progress aggregation, a cancel token, and a `verify()` that re-checks the completed snapshot before marking the model usable. The same code is reachable as `transcription-service download-model` for headless use (F2's `cli` profile contract: JSON on stdout, diagnostics on stderr, distinct exit codes).

### Runtime prerequisites (FR-9)

- **WebView2**: `bundle.windows.webviewInstallMode = { type: "downloadBootstrapper" }` — detects and installs when absent, keeps the installer small.
- **MSVC CRT**: `-C target-feature=+crt-static` in `apps/desktop/src-tauri/.cargo/config.toml`, so no `VCRUNTIME140.dll` dependency. Fallback if crt-static breaks a native dependency: bundle the redistributable DLLs as bundle resources (recorded in T6).

## Risks

- **R1 — Tauri NSIS uninstall vs. the 3 GB `models/` directory (FR-14, FR-16).** Tauri's generated uninstaller removes the install directory; an upgrade runs the old uninstaller first. If it recursively deletes `$INSTDIR`, every upgrade re-downloads 3 GB and every uninstall silently eats the model. T7 owns this and its **Done when** requires an *empirical* double-install and an uninstall, not a reading of the template. This is the single highest-risk item in the plan.
- **R2 — Relocatability of the baked Python environment (Q2-A's stated cost).** A venv's `pyvenv.cfg` and console scripts carry absolute paths. T4 therefore avoids a venv: bundled CPython + `uv pip install --target` + launch via `python -m transcription`, and its test physically copies the built tree to a different absolute path and runs it there.
- **R3 — CUDA DLL discovery.** CTranslate2 loads cuBLAS/cuDNN from the process DLL search path; pip-installed `nvidia-*` wheels put them in `site-packages/nvidia/*/bin`, which Windows will not search by default. T5 adds an explicit `os.add_dll_directory` shim executed before faster-whisper import, and NFR-1's acceptance ("`/health` reports `cuda`") is verified in T14.
- **R4 — NFR-1 size budget.** cuBLAS + cuDNN wheels are 700 MB–1 GB compressed and are the whole budget. T8 gates the build on 1.5 GB and T5 must exclude anything that transitively pulls PyTorch. If the gate trips, the documented lever is dropping `nvidia-cudnn-cu12`'s unused sublibraries, not adding a second installer.
- **R5 — Path drift from F1/F2/F3.** No sibling plan existed at planning time. Mitigated by the directory-level Files contract and the mandatory "verify the real tree first" step; a wave that discovers a materially different layout should stop and report rather than inventing a parallel structure.
- **R6 — `make` is absent on the operator's machine.** Every Makefile target is a thin wrapper over commands that must also be runnable directly; T1 documents the direct equivalents inline and T2's bootstrap installs GNU Make. The SDD probe (`make -n <target>`) only starts passing after bootstrap has run.
- **R7 — No toolkit plugin is installed.** `devops-toolkit` and `frontend-toolkit` are absent from `C:\Users\<user>\.claude\plugins\cache\its-marketplace\` (only `sdd` and `workflow-toolkit` are there). Every **Skills** line below will fail to resolve. Implementers must degrade gracefully and say so in their report rather than silently substituting generic judgment — this is especially load-bearing for T13, where `frontend-toolkit:internal-ui` is *mandatory*.

## Waves

| Wave | Tasks |
|---|---|
| 1 | T1, T2, T3, T4 |
| 2 | T5, T6, T7, T8 |
| 3 | T9, T10 |
| 4 | T11, T12 |
| 5 | T13 |
| 6 | T14 |
| — | T15 (parked, FR-19 could-have — out of the wave graph) |

Build-system tests live in `scripts/tests/` and run with `uv run --with pytest -- pytest scripts/tests -q` (no new package, no new dependency manager — uv is already the project's Python tool). There is deliberately **no** `scripts/tests/conftest.py`: each test module is self-contained and derives the repo root as `Path(__file__).resolve().parents[2]`, so parallel implementers never collide on a shared fixture file.

## Tasks

### [x] T1: Root Makefile QA fanout + frozen-lock verification  [deps: —]

- **Files**: `Makefile`, `scripts/verify_locks.py`, `scripts/tests/test_makefile_targets.py`, `scripts/tests/test_locks.py`
- **Test first**: `scripts/tests/test_makefile_targets.py` — cases: all of `format`/`lint`/`type`/`test`/`installer`/`bootstrap` are declared and `.PHONY` (FR-2); each of `format`/`lint`/`type`/`test` has a recipe line invoking Rust (`cargo`), TypeScript (`npm --prefix apps/desktop`) and Python (`uv run --directory services/transcription`) (FR-2 "visibly executes all three"); `make -n <target>` exits 0 for each of the four when `make` is on PATH, and the case skips with a recorded reason when it is not (NFR-6, and the SDD detection probe). `scripts/tests/test_locks.py` — cases: `Cargo.lock`, `apps/desktop/package-lock.json` and `services/transcription/uv.lock` all exist and none is matched by a `.gitignore` rule (FR-4); `verify_locks.py --check` exits 0 on the committed tree and non-zero when a lock file is removed (FR-4).
- **Implement**: one recipe command per line (GNU Make aborts on the first nonzero exit, which is exactly FR-2's fail-fast requirement — no `&&` chains, no `-` prefixes). `lint` also runs `scripts/sync_version.py --check` and `scripts/verify_locks.py --check`. `test` includes the build-system lane `uv run --with pytest -- pytest scripts/tests -q`. Above each target, a comment gives the literal commands to run directly without make (R6). Targets `installer` and `bootstrap` delegate to `scripts/build_installer.py` and `scripts/bootstrap.ps1`, which land in later waves — `make -n` resolves regardless.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed — degrade gracefully and report)*
- **Done when**: the four QA targets and `installer` resolve under `make -n`; running each target directly as its underlying commands succeeds on the merged tree; introducing a deliberate lint error in each of the three languages in turn makes `make lint` exit non-zero (FR-2 acceptance); `uv run --with pytest -- pytest scripts/tests -q` passes.

### [x] T2: Developer bootstrap script  [deps: —]

- **Files**: `scripts/bootstrap.ps1`, `scripts/tests/test_bootstrap.py`
- **Test first**: `scripts/tests/test_bootstrap.py` — cases: `bootstrap.ps1 -Check -Json` runs non-interactively and emits one JSON array on stdout with a row per prerequisite (`rust`, `node`, `npm`, `uv`, `make`, `tauri-cli`), each carrying `present`, `found_version`, `install_command` (FR-3); on this machine the `rust` and `make` rows report `present=false` with a concrete install command (FR-3 acceptance); a `python` row reports the Microsoft Store stub as `stub` — not `present` — and its remedy points at uv-managed CPython, never at the stub (FR-3 "does not silently fall back"); `-Check` exits 0 even with gaps, plain `bootstrap.ps1` exits non-zero if a required tool is still missing after the install attempt.
- **Implement**: PowerShell, detection first and installation second. Detect via `Get-Command` plus a version probe; treat an empty `python --version` as the stub. Install: `rustup-init` (download + `-y --default-toolchain stable-x86_64-pc-windows-msvc`), GNU Make via `winget install GnuWin32.Make` with a scoop fallback and a printed manual command if neither package manager is present, `cargo install tauri-cli --locked`. Print a final summary table and the exact remaining manual steps. Never elevate silently.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: `-Check` output matches the operator's real machine state; after a real run, `cargo --version` and `make --version` both succeed in a fresh shell (FR-3 acceptance); re-running is idempotent and fast.

### [x] T3: Product version single source of truth  [deps: —]

- **Files**: `version.txt`, `scripts/sync_version.py`, `scripts/tests/test_version.py`, `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/package.json`, `apps/desktop/src-tauri/Cargo.toml`, `crates/vault/Cargo.toml`, `services/transcription/pyproject.toml`
- **Test first**: `scripts/tests/test_version.py` — cases: `version.txt` holds one semver line and it is the value present in all four manifests (FR-5); `sync_version.py --check` exits 0 on a synced tree and non-zero naming the drifting file when any manifest is edited (FR-5); `sync_version.py --set 9.9.9` then `--check` passes and every manifest reads `9.9.9`, and re-running `--set` is idempotent (byte-identical output); the resolved installer artifact name computed by `sync_version.py --print-artifact-name` embeds the version (FR-5 acceptance: filename moves with the bump).
- **Implement**: `version.txt` is the SoT. `sync_version.py` rewrites only the version field in each manifest (json module for the two JSON files preserving key order and 2-space indent; a targeted regex anchored to `[package]`/`[project]` for the TOMLs so no other content moves). Modes: `--check`, `--set X.Y.Z`, `--print`, `--print-artifact-name`. It also exposes `read_version()` for import by `build_installer.py`.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: all four manifests agree with `version.txt`; `make lint` (which calls `--check`) fails when any one of them is edited by hand; the app's about/version surface, the Tauri bundle version and the artifact name all derive from the single value.

### [x] T4: Bake a relocatable Python runtime + dependency tree  [deps: —]

- **Files**: `scripts/build_pyenv.py`, `scripts/tests/test_build_pyenv.py`
- **Test first**: `scripts/tests/test_build_pyenv.py` — cases (the heavy build is one `session`-scoped fixture, marked `slow`, skipped when `uv` is absent): the built tree contains `python/python.exe` and a `site-packages/` holding `faster_whisper`, `ctranslate2`, `huggingface_hub`, `nvidia/cublas` and `nvidia/cudnn` (FR-8, NFR-1); **no** `torch`/`torchaudio`/`nvidia-cudnn-cu11` anywhere in the tree (NFR-1 "no PyTorch"); the build fails loudly if `uv export --frozen` reports the lock is stale (FR-4); **relocatability** — the whole tree is copied to a *different* absolute path under `tmp_path` and `python.exe -m transcription --help` there exits 0 (Q2-A's stated failure mode, R2); no file inside the tree contains the build-time absolute source path as an ASCII string.
- **Implement**: `uv python install 3.12` then copy the managed install into `build/pyenv/python/`. Deliberately **no venv** (a `pyvenv.cfg` `home=` is absolute): `uv export --frozen --no-dev --no-emit-project --format requirements-txt` from `services/transcription/`, then `uv pip install --python build/pyenv/python/python.exe --target build/pyenv/site-packages --no-deps -r <that file>`. Copy the F2 source tree into `build/pyenv/service/`. Emit `build/pyenv/pyenv-manifest.json` (python version, package list with versions, total bytes) for T8's build manifest. Idempotent, non-interactive, `--out` overridable.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: the relocation test passes; the tree runs the service's `--help` from an arbitrary directory with no system Python on PATH; the emitted manifest lists no PyTorch and the total size is recorded.

### [x] T5: Service payload dependencies — CUDA runtime wheels, huggingface_hub, DLL shim  [deps: T3]

- **Files**: `services/transcription/pyproject.toml`, `services/transcription/uv.lock`, `services/transcription/src/transcription/runtime_dlls.py`, `services/transcription/tests/test_runtime_dlls.py`
- **Test first**: `services/transcription/tests/test_runtime_dlls.py` — cases: `register_cuda_dll_dirs()` calls `os.add_dll_directory` for every existing `site-packages/nvidia/*/bin` directory, using a faked site-packages layout under `tmp_path` (R3); it is a no-op returning an empty list when no `nvidia/` tree exists, and never raises on a CPU-only machine (spec: CPU is best-effort fallback); it is idempotent across repeated calls; it is invoked before the first faster-whisper import (assert the service entry point calls it, by import-order inspection or a monkeypatched sentinel — the GPU-free test pattern F2 already established).
- **Implement**: add `huggingface_hub`, `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` to the project dependencies (Windows-only markers where uv supports them) and re-lock with `uv lock`; assert nothing pulls `torch`. `runtime_dlls.py` walks `Path(nvidia.__file__).parent/*/bin` (falling back to a scan of `sys.path` entries) and registers each with `os.add_dll_directory`, returning the registered paths for logging. Call it once from the service entry point and from the CLI entry point, before any provider import.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: `uv sync --frozen` resolves; `uv run --directory services/transcription pytest -q` passes with no GPU and no network; `uv tree` shows no `torch`; on the operator's machine `GET /health` reports `device: "cuda"` once a model is present (deferred proof to T14, but the shim's log line must name the registered directories).

### [x] T6: Tauri bundle configuration — resources, NSIS per-user install, WebView2, CRT  [deps: T3, T4]

- **Files**: `apps/desktop/src-tauri/tauri.conf.json`, `apps/desktop/src-tauri/.cargo/config.toml`, `scripts/tests/test_bundle_config.py`
- **Test first**: `scripts/tests/test_bundle_config.py` — cases: `bundle.active` is true and `bundle.targets` is exactly `["nsis"]` (FR-7 single installer executable); `bundle.resources` includes the baked `pyenv` and `service` trees produced by T4 (FR-8); `bundle.windows.nsis.installMode == "currentUser"` (NFR-4, Q4-A — no UAC); `nsis.installerHooks` points at `../../../installer/installer_hooks.nsh` and that path resolves (FR-14/16/18 wiring); `bundle.windows.webviewInstallMode.type == "downloadBootstrapper"` (FR-9); Start Menu shortcut enabled and desktop-shortcut option present (FR-13); `version` equals `version.txt` (FR-5); `.cargo/config.toml` sets `-C target-feature=+crt-static` for `x86_64-pc-windows-msvc` (FR-9 missing-DLL criterion); `productName`/`identifier` are unchanged from F3's values (F3 NFR-8 — changing them orphans the installed settings file).
- **Implement**: edit F3's existing `tauri.conf.json` in place — do not regenerate it. Add the `bundle` block fields above plus `publisher`, `shortDescription`, and the `nsis` `languages`/`displayLanguageSelector: false` so the build stays non-interactive. Record in a comment (or in `docs/build.md` via T12) that the CRT fallback, if `crt-static` breaks a native dep, is bundling the VC redist DLLs as resources.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: `npm --prefix apps/desktop run tauri build` produces one `.exe` under `apps/desktop/src-tauri/target/release/bundle/nsis/`; installing it produces no UAC prompt; the app launches with no missing-DLL dialog; `dumpbin /dependents` (or Dependencies.exe) on the app binary shows no `VCRUNTIME140.dll`.

### [x] T7: NSIS installer hooks — app folder skeleton, upgrade preservation, uninstall policy, silent mode  [deps: —]

- **Files**: `installer/installer_hooks.nsh`, `installer/README.md`, `scripts/tests/test_installer_hooks.py`
- **Test first**: `scripts/tests/test_installer_hooks.py` (static contract assertions over the `.nsh`, since NSIS cannot be unit-tested — the behavioural proof is this task's Done-when and T14) — cases: a `NSIS_HOOK_POSTINSTALL` macro creates `models\`, `logs\` and `data\` under `$INSTDIR` (FR-8); a `NSIS_HOOK_PREUNINSTALL`/`POSTUNINSTALL` macro exists and no macro references the vault path or `meetings_root` in any delete statement (FR-14 "never touches the vault, under any code path" — assert zero `Delete`/`RMDir` lines whose argument is not rooted at `$INSTDIR` or the app's own `%APPDATA%` folder); the uninstall path presents an explicit choice for `models\` and branches on it rather than deleting unconditionally (FR-14); an upgrade path guard preserves `$INSTDIR\models` (FR-16); `/VAULT=` and `/D=` are parsed in silent mode and `/VAULT=` results in a `config.json` write under `$APPDATA\<identifier>\` (FR-18); the written JSON contains `meetings_root` and a schema version key matching F3's schema (FR-10/11 as superseded by the batch config contract).
- **Implement**: a single `installer_hooks.nsh` with Tauri 2's four hook macros. Post-install: `CreateDirectory` the three subfolders; if `${GetOptions} /VAULT=`, validate the path is non-empty, creatable and **not** under `$INSTDIR`, then write/merge `config.json`. Pre-uninstall: detect the upgrade case (Tauri passes the preserve flag when the new installer invokes the old uninstaller) and skip the model directory; in the genuine-uninstall case, ask "also delete the downloaded model (~3 GB) at `$INSTDIR\models`?" and honour the answer, defaulting to *keep* and printing the path so nothing is silently orphaned. `installer/README.md` documents the hook contract, the silent-install invocation and the exact vault-safety invariant.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: install → uninstall leaves a populated vault folder byte-for-byte unchanged (compare hashes, FR-14 acceptance); install v1, put a sentinel file in `models\`, install v2 over it → the sentinel and `config.json` survive and no re-download is triggered (FR-16 acceptance); `setup.exe /S /D=<dir> /VAULT=<path>` completes with no UI and yields the same on-disk state as the interactive path (FR-18 acceptance).

### [x] T8: One-command release build — orchestration, checksum, manifest, size gate  [deps: T3, T4]

- **Files**: `scripts/build_installer.py`, `scripts/tests/test_build_installer.py`
- **Test first**: `scripts/tests/test_build_installer.py` — cases: the pipeline stage list is exactly the documented order (version check → lock check → pyenv bake → tauri build → collect → gate) and each stage failure aborts with a distinct non-zero exit code (FR-6 "exits non-zero if any payload fails"); `--dry-run` prints every command it would run and touches nothing, and no command in it is interactive (FR-6, NFR-5); given a fixture `.exe` and a fixture pyenv manifest, `collect()` writes `dist/Transcriber_<version>_x64-setup.exe`, a matching `.sha256` whose digest verifies, and `dist/build-manifest.json` containing product version, git commit, and the resolved versions of all three payloads (FR-15); the size gate raises when the artifact exceeds 1.5 GB and passes at 1.5 GB exactly (NFR-1).
- **Implement**: pure-Python driver invoked as `uv run scripts/build_installer.py` (PEP 723 inline metadata if any dependency is needed; prefer stdlib only). Frozen everywhere: `npm ci` for the app, `uv sync --frozen` / `uv export --frozen` for the service, `--locked` for cargo (FR-4). Deterministic output path is `dist/`. Fail loudly on a stale lock rather than resolving. No prompts, no TTY assumptions.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: one command from a clean clone on a bootstrapped machine produces the installer, its checksum and the manifest at the documented path in under 20 minutes, non-interactively (FR-6, NFR-5); the size gate reports the real artifact size and it is ≤ 1.5 GB (NFR-1); re-running with the network disabled after one successful build still succeeds from cache (FR-4 acceptance).

### [x] T9: App-folder resolution and sidecar launch against the bundled runtime  [deps: T5, T6]

- **Files**: `apps/desktop/src-tauri/src/app_paths.rs`, `apps/desktop/src-tauri/src/lib.rs`, `apps/desktop/src-tauri/src/service.rs`
- **Test first**: Rust `#[cfg(test)]` modules in `app_paths.rs` and `service.rs` — cases: `app_dir()` resolves to the directory of the running executable and `models_dir()`/`logs_dir()`/`data_dir()` hang off it, with a dev-mode override so `tauri dev` uses the repo tree (FR-8, FR-11-as-superseded); `model_dir()` returns the `<app folder>\models\faster-whisper-large-v3` path and never a path outside the app folder even when fed a crafted override (desktop profile: path traversal, every command argument is untrusted); `sidecar_command()` builds an argv of `<app folder>\resources\pyenv\python\python.exe -m transcription serve --port 0 --config <appdata config path>` with env `TRANSCRIBER_APP_DIR` and `TRANSCRIBER_MODEL_PATH` set, asserted as a structured value without spawning a process; when `resources\pyenv` is missing the launch returns a typed error naming the missing path rather than panicking (F3 NFR-6).
- **Implement**: extend, do not replace, F3's existing sidecar module. The spawn switches from whatever F3 used in development to the bundled interpreter resolved through `app_paths`; keep F3's stdout ready-line parsing (`{"event":"listening","port":…,"token":…}`) untouched. Registering the module in `lib.rs` is the only edit to that file — keep it to the `mod`/`invoke_handler` lines to minimise conflict surface with T13.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: `cargo test --workspace` passes; a `tauri build` install launches the app, the sidecar starts from the bundled interpreter with no system Python on PATH, and `GET /health` on the reported port answers 200 (F2 FR-2); killing the app kills the sidecar.

### [x] T10: Model download core — resume, verify, cancel, progress  [deps: T5]

- **Files**: `services/transcription/src/transcription/model_download.py`, `services/transcription/tests/test_model_download.py`
- **Test first**: `services/transcription/tests/test_model_download.py` (no network, no GPU, no real weights — monkeypatch `huggingface_hub`, per F2's established GPU-free pattern) — cases: `start()` reports progress as `{downloaded_bytes, total_bytes, percent, file, state}` and the callback fires at least once per second during a simulated transfer (FR-12 "bytes and percentage, at least once a second"); a transfer interrupted at ~50% and restarted resumes from the existing `.incomplete` blob rather than from 0 (FR-12, NFR-3 — assert the resume offset passed to the fake transport); `verify()` fails on a truncated or digest-mismatched file and the model is **not** marked usable, leaving no partial file presented as complete (FR-12, NFR-3); `cancel()` stops within one chunk and leaves a defined, resumable state (FR-12); on success the snapshot lands under the configured `<app folder>/models/` path and a `.ready` marker (or equivalent state file) records the verified revision (FR-12 acceptance); a target path outside the configured app folder is rejected as `invalid_request` (F2 FR-9 path allowlist, cli profile path-traversal rule).
- **Implement**: wrap `huggingface_hub.snapshot_download` for `Systran/faster-whisper-large-v3` with `local_dir=<models dir>`, a `tqdm`-class replaced by a progress sink, and a cancellation event checked between callbacks. Pin the revision so verification has something to compare against. Verification re-walks the snapshot and checks sizes plus the hub-reported digests; only then write the ready marker the service reads before loading. Failures map onto F2's existing error taxonomy — never a bare 500 traceback.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: `uv run --directory services/transcription pytest -q` passes offline in under 30 s; a real download of the ~3 GB snapshot killed at roughly 50% and restarted resumes near 50% (FR-12 acceptance, executed once by hand and recorded).

### [x] T11: Expose the downloader over HTTP and the CLI  [deps: T10]

- **Files**: `services/transcription/src/transcription/api/model_routes.py`, `services/transcription/src/transcription/cli.py`, `services/transcription/src/transcription/app.py`, `services/transcription/tests/test_model_api.py`
- **Test first**: `services/transcription/tests/test_model_api.py` (FastAPI `TestClient`, no lifespan, fake downloader) — cases: `POST /v1/model/download` returns `202` immediately with a handle and does not block (FR-12); `GET /v1/model/download` returns `{state, downloaded_bytes, total_bytes, percent, error_kind?, error_message?}` and progresses monotonically (FR-12); `DELETE /v1/model/download` cancels and the subsequent status reads `cancelled` and is retryable (FR-12); a second `POST` while one is running does not start a parallel transfer; `GET /health` reports the model as absent before download and present after, so the app can detect a missing model without guessing (FR-17); all three endpoints require the bearer token and refuse a non-loopback origin (F2 FR-9); the CLI `transcription-service download-model --out <dir>` prints exactly one JSON object on stdout, progress on stderr, exits 0 on success and a distinct documented non-zero code per failure class (cli profile Verification, F2 FR-10).
- **Implement**: thin routing over T10's module — no download logic in the route handlers. Register the router on F2's existing app factory (that is the only edit to `app.py`). The CLI subcommand reuses the same core in-process.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: `uv run --directory services/transcription pytest -q` passes; `uv run --directory services/transcription transcription-service download-model --help` works and one real invocation against a temp dir behaves as documented (cli profile: run the actual command before claiming done); `/health` distinguishes model-present from model-absent.

### [x] T12: Build, install and configuration documentation + manual smoke checklist  [deps: T1, T2, T7, T8]

- **Files**: `README.md`, `docs/build.md`, `docs/config-contract.md`, `docs/manual-smoke-installer.md`
- **Test first**: `scripts/tests/test_makefile_targets.py` and `scripts/tests/test_bundle_config.py` already assert the machine-checkable facts; this task's verification is documentary. Its checklist file **is** the artifact under test — `docs/manual-smoke-installer.md` must enumerate one checkbox per acceptance criterion it covers (FR-7, FR-8, FR-9, FR-13, FR-14, FR-16, FR-17, FR-18, NFR-2, NFR-4) and T14 executes it. Do not add a test that merely asserts a file exists.
- **Implement**: `README.md` gains a short "Build and install" section pointing at `docs/build.md` (clean clone → `scripts/bootstrap.ps1` → `make installer`, plus the direct non-make commands per R6, plus the output paths and the version-bump procedure). `docs/config-contract.md` states the authoritative settings contract from the Architecture overview above — one `config.json` in `%APPDATA%\<identifier>\`, F3 owns the schema, the app passes `--config` and `TRANSCRIBER_APP_DIR`/`TRANSCRIBER_MODEL_PATH` to the sidecar, the executable-directory fallback — and explicitly records that spec FR-11's "config in the application folder" is superseded. `docs/manual-smoke-installer.md` is the executable-by-a-human checklist.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: a reader following `docs/build.md` alone reaches a built installer; every superseded or reinterpreted spec requirement (FR-11, FR-10/12 relocated into the app) is called out in writing; the checklist covers each listed criterion with a concrete observable step.

### [x] T13: First-run model-download step and missing-model recovery in the app  [deps: T9, T11]

- **Files**: `apps/desktop/src/components/ModelDownloadStep.tsx`, `apps/desktop/src/components/ModelDownloadStep.test.tsx`, `apps/desktop/src/lib/modelDownload.ts`, `apps/desktop/src/lib/modelDownload.test.ts`, `apps/desktop/src-tauri/src/commands/model.rs`, `apps/desktop/src-tauri/src/lib.rs`
- **Test first**: `ModelDownloadStep.test.tsx` (Vitest + React Testing Library, fake command layer) — cases: with no model present the first-run flow shows the download step after F3's existing vault-folder step, stating in plain language that the model is missing (FR-17); starting a download renders bytes **and** percent and updates as status polls arrive (FR-12); cancel returns the step to an idle, retryable state and says so (FR-12); a failed download shows the service's error message verbatim plus a Retry control, and the app remains usable — no broken install, no blocked window (FR-17 "a failed or skipped download never leaves a broken install"); a "Skip for now" path leaves the app functional with a persistent, non-modal missing-model notice that exposes the same retry (FR-17); with the model already present the step is skipped entirely (FR-16 — an upgrade must not re-download). `commands/model.rs` `#[cfg(test)]` — cases: the three commands validate their arguments and return typed errors rather than panicking (F3 NFR-6), and none accepts a caller-supplied filesystem path — the destination comes from `app_paths` only (desktop profile: IPC authorization, path traversal).
- **Implement**: extend F3's existing first-run flow with one additional step; do not build a parallel wizard. `commands/model.rs` proxies to the T11 endpoints over the existing authenticated loopback client; the frontend polls status on the same 1–2 s cadence F3 already uses for jobs (F3 Q3-A). Edit `lib.rs` only to register the new commands.
- **Skills**: `frontend-toolkit:internal-ui` — **mandatory** for this task. The spec flags it conditionally ("if Q1 places setup UI inside the React app"); Q1 resolved to A, so the condition is met and this is the one UI task in the feature. **The plugin is not installed in this environment** — the implementer must state that explicitly in their report rather than silently substituting generic UI judgment, and must match the density and conventions of F3's existing components instead.
- **Done when**: `npm --prefix apps/desktop run test` and `cargo test --workspace` pass; the real app, launched with an empty `models\` directory, walks the vault-pick → model-download flow to completion against the real service (desktop profile Verification: `make test` does not prove the app launches — drive the flow); killing the download mid-way and retrying resumes; with networking off the app launches, states the model is missing, and the retry succeeds once networking is restored (FR-17 acceptance).

### [x] T14: End-to-end installer verification on the operator's machine  [deps: T6, T7, T8, T9, T12, T13]

- **Files**: `scripts/verify_install.py`, `scripts/tests/test_verify_install.py`, `docs/verification-installer.md`
- **Test first**: `scripts/tests/test_verify_install.py` — unit cases over the checker's pure functions against a synthetic install tree under `tmp_path`: it flags a missing `models\`/`logs\`/`data\` directory (FR-8); it flags a non-writable app folder and passes on a writable one (FR-8 acceptance); it flags an installer artifact over 1.5 GB (NFR-1); it flags a `config.json` that is invalid JSON, lacks `meetings_root`, or lacks a schema version (FR-10/11); it resolves the vault root the way the service resolves it and reports a mismatch against the value the user picked (FR-11 acceptance "a script reading the config the way the service will read it resolves the same vault root").
- **Implement**: `verify_install.py` takes an install directory plus the installer artifact and prints a pass/fail table with a non-zero exit on any failure — app executable, bundled runtime, service tree, the three empty directories, writability by a non-elevated process, artifact size, checksum match, config resolution, and `GET /health` reporting `device: "cuda"` (NFR-1 acceptance). `docs/verification-installer.md` records the run: the executed `docs/manual-smoke-installer.md` checklist with results, timings for NFR-2 (< 2 min install) and NFR-5 (< 20 min build), the UAC observation (NFR-4), the double-install upgrade result and the uninstall vault-hash comparison (R1, FR-14/16), and the silent-install run (FR-18).
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: a real `make installer` → install → launch → first-run wizard → real transcription cycle completes on the operator's machine; `verify_install.py` exits 0 against that install; every checklist item in `docs/manual-smoke-installer.md` is ticked with an observation recorded; the uninstall leaves a populated vault byte-for-byte identical (hash-compared, FR-14); `make format`, `make lint`, `make type`, `make test` all exit 0 on the final tree (NFR-6).

### [!] T15: CI workflow building the installer on a Windows runner  [deps: T8]

- **Files**: `.github/workflows/release.yml`
- **Test first**: n/a — verification is a green run on a tagged commit.
- **Implement**: `windows-latest` runner, tag trigger, `scripts/bootstrap.ps1` then `scripts/build_installer.py`, upload the `.exe`, its `.sha256` and `build-manifest.json` to the release.
- **Skills**: `devops-toolkit:devops-rollout-plan` *(not installed)*
- **Done when**: a tagged commit produces a release with the installer and its checksum attached.
- **Parked**: FR-19 is a *could*-have and the operator asked for a lean pass. The repository has no CI today and the build is 20 minutes of GPU-irrelevant wheel downloads; the local build path (T8) is the supported one for the MVP. Unpark by moving this into a wave after T8.

## QA expectations

Make targets created by **T1** of this feature (none existed before — the repository had no `Makefile` and `make` is not installed until `scripts/bootstrap.ps1` runs):

| Target | Rust | TypeScript | Python | Build system |
|---|---|---|---|---|
| `format` | `cargo fmt --all` | `npm --prefix apps/desktop run format` | `uv run --directory services/transcription ruff format .` | — |
| `lint` | `cargo clippy --workspace --all-targets -- -D warnings` | `npm --prefix apps/desktop run lint` | `uv run --directory services/transcription ruff check .` | `sync_version.py --check`, `verify_locks.py --check` |
| `type` | `cargo check --workspace` | `npm --prefix apps/desktop run typecheck` | `uv run --directory services/transcription mypy src` | — |
| `test` | `cargo test --workspace` | `npm --prefix apps/desktop run test` | `uv run --directory services/transcription pytest -q` | `uv run --with pytest -- pytest scripts/tests -q` |

Extra targets: `make installer` (T8), `make bootstrap` (T2).

- **`make` is absent until bootstrap runs.** Every target is a one-command-per-line wrapper and the direct equivalents are documented inline and in `docs/build.md` (R6). Implementers who cannot run `make` must run the underlying commands and say so.
- **Known-slow, not flaky**: `scripts/tests/test_build_pyenv.py` bakes a real environment (several minutes, hundreds of MB) — it is marked `slow` and gated on `uv` being present. The Python suite in `services/transcription/` stays offline and GPU-free per F2's FR-15; the model-download tests never touch the network.
- **Skills note (R7)**: `devops-toolkit` and `frontend-toolkit` are **not installed** — every `Skills` line above resolves to nothing today. The gap must be surfaced in the final report. Independently, the service-side tasks (T5, T10, T11) modify F2's package, whose own spec makes `testing-toolkit:python-testing-patterns` applicable to its pytest suite; that plugin is also absent. This plan's `Skills` fields list only what this feature's spec authorises.
- **Discipline skills** (`workflow-toolkit:*` TDD and verification) are invoked by every implementer on every task and are deliberately not repeated per task.
