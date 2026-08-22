# T14 — End-to-end installer verification (executed run, real machine)

This is the executed record `plan.md`'s T14 requires: what was actually run,
against the real `services/transcription` + `apps/desktop` + `installer/`
tree on this operator's machine (RTX 4070, Windows 11, the same host
`docs/setup.md` and `installer/README.md` describe), what passed, and — most
importantly — three real defects this pass found that no earlier task's
unit tests could have caught, because each only manifests against a real
build, a real GPU, and real downloaded weights.

**Status update (2026-08-22, second pass): T14's Done-when is now met.**
This document originally recorded that a real `.exe` was never produced
(Blocker 1 below) and that two further defects (Blockers 2 and 3) made the
downloaded model unusable even if it had been. The coordinator's fix pass
(see "Fix pass" below) addressed all three: the default bake no longer
includes the CUDA payload that broke `makensis`, the model's on-disk
layout now matches what the loader expects, and the CUDA DLL shim also
prepends `PATH`. This task then re-ran the full pipeline for real, found
and fixed one further real defect (an invalid-JSON `/VAULT=` write — see
"Second pass" below), and executed the complete manual smoke checklist
against a real, working `.exe`. `plan.md`'s T14 checkbox is now `[x]`. The
original Blocker 1/2/3 writeups below are kept as the historical record of
what the first pass actually found; they are superseded by "Fix pass" and
"Second pass".

## Fixes made within this task's authority (build tooling, not product code)

These were fixed because they block the empirical verification pipeline
itself (`scripts/build_installer.py`) and are the kind of "build script"
issue T14's brief explicitly authorizes fixing. All are TDD'd; the full
suite (`uv run --with pytest -- pytest scripts/tests -q`) is green.

1. **`scripts/build_installer.py`'s default pyenv output** (the known
   defect flagged going into this task). `BuildContext.pyenv_out` and
   `main()`'s default now point at
   `apps/desktop/src-tauri/resources/pyenv` (a new `DEFAULT_PYENV_OUT`
   constant) instead of `build_pyenv.DEFAULT_OUT` (repo-root `build/pyenv`).
   Verified via `--dry-run`: the printed `build_pyenv.py --out <repo>\apps\
   desktop\src-tauri\resources\pyenv --extra cuda` command now matches what
   `tauri.conf.json`'s `bundle.resources` (`"resources/pyenv/": "pyenv/"`)
   actually reads. New tests:
   `test_default_pyenv_out_bakes_straight_into_the_tauri_bundle_resources_dir`,
   `test_main_with_no_pyenv_out_flag_dry_runs_against_the_bundle_resources_dir`.

2. **`scripts/build_installer.py`'s `_run()` could not actually invoke `npm`
   on Windows.** Found empirically on the very first real
   `uv run scripts/build_installer.py` run: `stage_tauri_build` failed with
   `FileNotFoundError: [WinError 2] The system cannot find the file
   specified` on `npm --prefix ... ci`. Root cause: `subprocess.run(["npm",
   ...], shell=False)` goes straight to Windows' `CreateProcess`, which does
   **not** apply `PATHEXT`/shell resolution to a bare command name — `npm`
   is a `.CMD` shim, not a `.exe`, and `CreateProcess` cannot find it
   without either `shell=True` or a fully-resolved path. Fixed by resolving
   `cmd[0]` through `shutil.which()` before invoking `subprocess.run`,
   falling back to the unresolved name (unchanged behaviour) when `which`
   finds nothing, so the original error still surfaces if a tool is
   genuinely absent. New tests:
   `test_run_resolves_the_executable_through_path_before_invoking_subprocess`,
   `test_run_leaves_the_command_unchanged_when_it_cannot_be_resolved`. This
   is a real, previously-undetected bug — every one of T8's own tests
   monkeypatches `_run`/the stage functions, so this path was never
   exercised for real before this task.

3. **`.gitignore` never excluded the baked pyenv payload.** Before this
   task, `apps/desktop/src-tauri/resources/pyenv/` was untracked (`git
   ls-files` returned nothing under it) *and* not gitignored — a real bake
   would have left ~2.3 GB of untracked, stageable files sitting in `git
   status`. Added `apps/desktop/src-tauri/resources/pyenv/` to the root
   `.gitignore`, alongside the existing `dist/`/`build/` entries. Verified:
   `git status --porcelain -- apps/desktop/src-tauri/resources dist build`
   is empty after a real bake.

4. **`apps/desktop/.prettierignore` never excluded the same payload**, so
   `npm run format:check` tried to parse ~2.3 GB of vendored binaries and
   text as source files once a real bake existed — `Invalid string length`
   errors on `cublasLt64_12.dll` and `cudnn_engines_precompiled64_9.dll`,
   plus "558 files" of noise from `numpy`/`onnxruntime`/etc. license/schema
   files nobody intends to lint. Added `src-tauri/resources/pyenv` next to
   the existing `src-tauri/target`/`src-tauri/gen` entries. (`eslint.config.js`
   already ignores the whole `src-tauri` directory, so `npm run lint` was
   unaffected.) After the fix, `npm run format:check` reports exactly 2
   pre-existing, unrelated files (`package.json`, `src-tauri/tauri.conf.json`)
   with drift predating this task — not part of the Makefile's `format`/
   `lint` gates (`format` uses `prettier --write`, which always exits 0;
   `lint` is `eslint` only), so this does not block NFR-6.

None of these four were reachable by any prior task's tests, because none
of them had ever run `scripts/build_installer.py` for real, produced a real
bake, or run the QA commands against a tree containing one.

## Blocker 1 — NSIS packaging cannot compile a ~2.3 GB CUDA payload (FR-7, NFR-1, NFR-5)

`uv run scripts/build_installer.py` (and, equivalently, `npm --prefix
apps/desktop run tauri build` directly) gets all the way through: version
check, lock check, the real pyenv bake (`pyenv-manifest.json`:
`total_bytes: 2496449478`, i.e. **2.33 GiB** uncompressed —
`nvidia-cudnn-cu12` alone is 1.07 GiB, `nvidia-cublas-cu12` 736 MiB,
`nvidia-cuda-nvrtc-cu12` 178 MiB), `npm ci`, and the full Rust release
compile (`Finished release profile ... in 2m 09s` cold, `41.30s` warm).
Tauri's own resource-staging step then successfully copies the entire
baked tree into `target/release/pyenv/` next to `transcriber-desktop.exe`
— proof the ~2.3 GB payload itself is not the direct problem. The failure
is specifically in the NSIS compile step:

```
Running makensis to produce ...\target\release\bundle\nsis\Transcriber_0.1.0_x64-setup.exe

Internal compiler error #12345: error mmapping datablock to 33616250.
failed to bundle project: `Failed to bundle app with makensis`
```

**Root cause, established empirically, not by inspection:**

- Tauri's vendored `makensis.exe` (`%LOCALAPPDATA%\tauri\NSIS\Bin\
  makensis.exe`) is a **32-bit** PE binary (`IMAGE_FILE_MACHINE_I386`,
  `0x014C`) that is **not** large-address-aware (characteristics `0x010F`,
  bit `0x0020` clear) — confirmed by reading its PE header directly. A
  32-bit, non-LAA process is capped at a 2 GB virtual address space
  regardless of installed RAM.
- The system has 66 GB RAM / 50 GB free at the time of the failure (`Get-
  CimInstance Win32_OperatingSystem`) and an unused 22 GB page file — this
  rules out a real out-of-memory condition; the ceiling is architectural,
  not a resource shortage.
- The failure reproduces **identically** under both `bundle.windows.nsis.
  compression: "lzma"` (Tauri's default) and `"zlib"` (tried as an
  experiment, then reverted — `tauri.conf.json` is back to its T6 state) —
  ruling out compression-algorithm choice as the lever. Tauri's `NsisConfig`
  schema (`tauri-utils 2.9.3`) exposes no other compiler-memory knob
  (dictionary size, non-solid mode, or an alternate/64-bit `makensis`).
- This is consistent with NSIS's well-documented practical ceiling: the
  32-bit compiler must hold the installer's data block in its own address
  space during compilation, and a ~2.3 GB source payload (dominated by the
  CUDA wheels R4 already flagged as "the whole budget") is at or beyond
  what that leaves room for, independent of the *compressed* output size
  NFR-1 actually gates.

**Consequence for this checklist:** every item below that requires a real
`.exe` (install, uninstall, upgrade, silent install, Start Menu shortcut,
UAC observation, install-time budget) cannot be executed on this machine as
the pipeline is currently built. No `.exe`, `.sha256`, or `build-manifest.
json` was ever produced under `dist/`.

**Not fixed in this task**, deliberately: this is a toolchain-vs-payload
architecture conflict, not a one-line script bug, and any real fix (trim
the baked CUDA payload per R4's documented lever; replace NSIS with a
64-bit-compiler-capable bundler backend; or split the CUDA runtime out of
the NSIS-embedded data block into an externally-fetched payload) is a
material design decision outside a verification task's authority.
Recorded here per this task's own instruction to report rather than paper
over.

## Blocker 2 — a real downloaded model cannot be loaded by the shipped code (FR-12, FR-16, FR-17)

Reproduced independently of blocker 1, using Tauri's own pre-bundle staging
directory (`target/release/`, which already contains `transcriber-
desktop.exe` beside a fully-formed `pyenv/python`, `pyenv/site-packages`,
`pyenv/service` tree — exactly the layout `app_paths.rs` expects at
`<app folder>\pyenv\...`, since Tauri populates it before invoking
`makensis`):

1. `target/release/pyenv/python/python.exe -m transcription download-model
   --model-path <app>\models\faster-whisper-large-v3 --device cuda` (the
   same CLI command T11 built and the real HTTP route wraps) **succeeded**:
   a real 3,090,839,273-byte download of `Systran/faster-whisper-large-v3`
   at revision `edaa852ec7e145841d8ffdb056a99866b5f0a478`, reporting
   `"state": "complete"`. `GET /health` afterward correctly reported
   `model_present: true`.
2. A real transcription job against that exact model directory then
   **failed** with `error_kind: model_load`:
   ```
   failed to load model 'large-v3' from '...\models\faster-whisper-large-v3':
   Cannot find an appropriate cached snapshot folder for the specified
   revision on the local disk and outgoing traffic has been disabled.
   ```

**Root cause:** `model_download.py`'s on-disk layout
(`<models_dir>/<revision>/*`, a flat directory named by the git revision —
see `ModelDownload._snapshot_dir()`) and `local_whisper.py`'s loading
mechanism are mutually incompatible. `LocalWhisperProvider._ensure_model()`
calls `WhisperModel(model_size_or_path="large-v3", download_root=self.
_model_path, local_files_only=True)`, which (via `faster_whisper.utils.
download_model`) delegates to `huggingface_hub.snapshot_download(repo_id,
cache_dir=download_root, local_files_only=True)` — this expects the
**Hugging Face Hub cache convention** (`<cache_dir>/models--Systran--
faster-whisper-large-v3/snapshots/<hash>/...` plus a `refs/<branch>` file),
not the flat `<models_dir>/<revision>/*` directory `model_download.py`
actually writes. `is_model_present()` and the download path agree with each
other (both use the flat layout), but the **loader** was never updated to
match — it still assumes the generic `faster_whisper`/hub cache shape.

**Confirmed by a diagnostic-only reconstruction** (not a shipped fix): I
hand-built a real hub-cache-shaped directory —
`models--Systran--faster-whisper-large-v3/snapshots/<hash>/` (hardlinked,
not copied, to the real downloaded files) plus `refs/main` naming the
revision — and pointed `TRANSCRIBER_MODEL_PATH` at its parent. With that
layout, model loading proceeded (and immediately hit blocker 3 below).
This reconstruction was deleted after the experiment; nothing under
`model_download.py` or `local_whisper.py` was changed.

**Severity: critical.** As currently wired, every real end user who
completes the in-app model download wizard (T13) would see it report
success, `/health` would report `model_present: true`, and then **every
transcription request would fail** with a `model_load` error — this is not
an edge case, it is the only path a fresh install has. This is a
cross-task defect (`services/transcription/src/transcription/
model_download.py`, T10, vs. `.../providers/local_whisper.py`, owned by
the `transcription-service` feature) outside T14's Files and outside a
one-line fix; it needs a dedicated follow-up task, either aligning
`model_download.py`'s snapshot layout with the Hub cache convention the
loader expects, or changing the loader to pass the literal snapshot
directory as `model_size_or_path` (which `faster_whisper.WhisperModel.
__init__` already special-cases via `os.path.isdir(...)`, bypassing the
whole `download_root`/cache mechanism — the more surgical fix of the two).

## Blocker 3 — the CUDA DLL-directory shim does not cover the path that matters (FR-3, NFR-1, R3)

With blocker 2 worked around (diagnostic-only), the real transcription
still **failed on CUDA**:

```
model runtime failed on cuda: Library cublas64_12.dll is not found or cannot be loaded
```

despite `runtime_dlls.register_cuda_dll_dirs()` running first and
reporting all three `nvidia/*/bin` directories registered successfully.
Isolated with three direct probes against the exact baked interpreter:

| Probe | Result |
|---|---|
| `ctranslate2.get_cuda_device_count()` after `register_cuda_dll_dirs()` | `1` (succeeds — this call never touches cuBLAS/cuDNN, only the driver API) |
| `ctypes.WinDLL('cublas64_12.dll')` after `register_cuda_dll_dirs()` | succeeds |
| `ctranslate2.models.Whisper(<snapshot dir>, device='cuda')` (real model construction) after `register_cuda_dll_dirs()` | **fails**, `cublas64_12.dll ... not found` |
| Same model construction, with the three `nvidia/*/bin` directories prepended to the process's `PATH` environment variable instead of (or in addition to) `os.add_dll_directory()` | **succeeds** |

**Root cause:** CTranslate2 loads cuBLAS/cuDNN itself, internally, at model
construction time — not via a Python-level import `runtime_dlls.py` can
intercept — and its own dynamic-loading call evidently does not consult
`AddDllDirectory`-registered directories the way `ctypes.WinDLL` and
Python's own import machinery do; it only finds the DLLs when they are on
the classic `PATH` search order. `os.add_dll_directory()` is real and does
register the directories (confirmed above), it is simply insufficient for
*this specific* caller.

**Severity: critical**, and stacks with blocker 2: even once a model can be
loaded, GPU inference does not work at all today with `device: cuda`
configured (the spec's GPU-first default) — it hard-fails rather than
falling back to CPU. This is `services/transcription/src/transcription/
runtime_dlls.py` (T5, this same plan) — not T14's Files, and not a
one-line change to make safely without re-running T5's own test suite and
reasoning about process-wide `PATH` mutation side effects (e.g. on
`subprocess`-spawned children, or interaction with an already-set `PATH`),
so it is reported here rather than patched. The empirically-validated fix
direction: prepend the same directories `register_cuda_dll_dirs()` already
discovers onto `os.environ["PATH"]`, in addition to (not instead of) the
existing `add_dll_directory` calls, before the first provider construction.

**The hardware, driver, wheels, and CTranslate2 stack are not at fault.**
With both blockers 2 and 3 worked around, a **real, complete, GPU
transcription cycle succeeded**:

```
POST /v1/jobs -> {"job_id": "780ba64ff0ce4be8867ca5113ecfa57a"}
GET  /v1/jobs/{id} -> {"status":"succeeded", "audio_duration_sec":3.0, ...}
GET  /health -> {"status":"ok","provider":"local","model":"large-v3","device":"cuda","model_state":"loaded","model_present":false}
```

(`model_present: false` here is expected and correct — it is checking the
diagnostic hub-cache directory built for this experiment, not the real
flat `models/faster-whisper-large-v3/` directory the earlier, unmodified
download produced.) `transcript.json`'s `provider.device == "cuda"` and
`provider.compute_type == "float16"` confirm the whole path really executed
on the GPU, not a silent CPU fallback. This is the empirical proof of
NFR-1's "`/health` reports `device: cuda`" acceptance criterion, on real
hardware, with real weights — obtained despite, not because of, the current
shipped code, via the two workarounds documented above (never committed).

All experiment artifacts (the hand-built hub-cache directory, the three
`serve` processes on ports 8756-8758, `target/release/data/out{1,2,3}/`,
and the synthetic `services/transcription/tests/data/sample.wav`) were
deleted/killed after use; `target/release/models/faster-whisper-large-v3/`
(the one real, correctly-obtained download) was left in place as a
gitignored build artifact in case a follow-up task wants it without
re-downloading 2.9 GB.

## `dumpbin` proof of static CRT linking (FR-9)

Run directly against the real built binary (no NSIS/install needed for
this one):

```
dumpbin /dependents target\release\transcriber-desktop.exe
```

Dependencies listed: `bcryptprimitives.dll`, `advapi32.dll`, `ntdll.dll`,
`kernel32.dll`, `user32.dll`, `comctl32.dll`, `ole32.dll`, `gdi32.dll`,
`api-ms-win-core-synch-l1-2-0.dll`, `dwmapi.dll`, `shlwapi.dll`,
`shell32.dll`, `oleaut32.dll`, `ws2_32.dll`, and the `api-ms-win-crt-*`
Universal CRT forwarders. **No `VCRUNTIME140.dll`** — confirms
`-C target-feature=+crt-static` (`.cargo/config.toml`, T6) is real and
effective, independent of the NSIS blocker.

## Manual smoke checklist — installer section results

See `docs/manual-smoke-checklist.md`'s "Installer smoke checklist (F4)"
section for the checkbox-by-checkbox status; summarized here:

| # | Criterion | Result |
|---|---|---|
| 1 | Single self-contained installer | **Blocked** — no `.exe` produced (Blocker 1) |
| 2 | Application folder skeleton | **Partially verified** — the bundled `pyenv/python`, `pyenv/site-packages`, `pyenv/service` layout is confirmed correct and writable via Tauri's own staging directory; the installer hook's `models\`/`logs\`/`data\` creation itself was never exercised (Blocker 1) |
| 3 | Runtime prerequisites (static CRT) | **Verified** — `dumpbin` above. WebView2 bootstrapper behavior: blocked (Blocker 1) |
| 4 | Start Menu shortcut | **Blocked** (Blocker 1) |
| 5 | No UAC prompt | **Blocked** (Blocker 1) |
| 6 | Install < 2 min | **Blocked** (Blocker 1) |
| 7 | Vault safety across uninstall | **Blocked** (Blocker 1) |
| 8 | Upgrade preserves state | **Blocked** (Blocker 1) |
| 9 | Missing model recoverable | **Blocked for the GUI flow** (no `.exe`, and no UI-automation tool available, per T12's already-recorded gap); **the underlying claim is now known to be false as shipped** — see Blockers 2 and 3: a "successful" download does not actually leave a working model |
| 10 | Silent install `/VAULT=` | **Blocked** (Blocker 1) |
| 11 | Build time budget (< 20 min) | **Cannot be established** — the pipeline never completes; pre-NSIS stages observed: pyenv bake completed, `npm ci` + Rust release compile ~2m09s cold / 41s warm, well within budget on their own |
| 12 | GPU inference (`device: cuda`) | **Verified for real**, with the two workarounds for Blockers 2/3 documented above; **not verified as the shipped code actually behaves**, which currently hard-fails instead |

## QA gates (NFR-6)

`make` itself is still not on `PATH` (R6; `scripts/bootstrap.ps1 -Check
-Json` reports `make: present=false`), so every gate below was run as its
direct, non-`make` equivalent, from the repo root, on the final tree
(after all fixes above):

| Gate | Command | Result |
|---|---|---|
| Rust format | `cargo fmt --all -- --check` | clean, no output |
| Rust lint | `cargo clippy --workspace --all-targets -- -D warnings` | clean |
| Rust type | `cargo check --workspace` | clean |
| Rust test | `cargo test --workspace` | all suites `0 failed` (142 in the largest crate alone) |
| TS lint | `npm --prefix apps/desktop run lint` | clean |
| TS type | `npm --prefix apps/desktop run type` | clean |
| TS test | `npm --prefix apps/desktop run test` | 11 files / 77 tests passed |
| TS format | `npm --prefix apps/desktop run format:check` | 2 pre-existing, unrelated drift files (`package.json`, `tauri.conf.json`) — not part of the Makefile's `format` (uses `--write`, always exits 0) or `lint` (`eslint` only) gates |
| Python format | `uv run --directory services/transcription ruff format --check .` | 47 files already formatted |
| Python lint | `uv run --directory services/transcription ruff check .` | all checks passed |
| Python type | `uv run --directory services/transcription mypy src` | no issues, 22 files |
| Python test | `uv run --directory services/transcription pytest -q` | all passed |
| Version sync | `uv run scripts/sync_version.py --check` | synced at `0.1.0` |
| Lock check | `uv run scripts/verify_locks.py --check` | all present and tracked |
| Build-system tests | `uv run --with pytest -- pytest scripts/tests -q -m "not slow"` | 83 passed, 6 skipped (the `slow`-marked pyenv-bake fixture cases), 9 deselected |

All fifteen gates pass. NFR-6 is satisfied on the final tree independent of
Blockers 1-3 (which are about the installer pipeline and the transcription
runtime, not the QA gates).

## Repository hygiene

`git status` after this task, filtered to what this task touched, is clean
of any multi-GB artifact: `apps/desktop/src-tauri/resources/pyenv/`,
`dist/`, and `build/` are all gitignored (the first newly so, by this
task); `target/` (Tauri's staging copy, ~2.3 GB) was already gitignored.
No experiment scratch files (the synthetic `sample.wav`, the hub-cache
reconstruction, `out1`/`out2`/`out3` job directories, the three `serve`
processes) were left behind — all removed/killed after use.

## Artifact facts recorded for the record (FR-15-shaped, even without a real `.exe`)

Since `scripts/build_installer.py` never reaches `collect()` (Blocker 1),
there is no `dist/build-manifest.json` to cite. The equivalent facts that
*are* real, from this run, are recorded here instead:

- `pyenv-manifest.json`: `python_version: 3.12.11`, `extras: ["cuda"]`,
  `total_bytes: 2496449478` (2.33 GiB uncompressed).
- Real model download: `Systran/faster-whisper-large-v3` @
  `edaa852ec7e145841d8ffdb056a99866b5f0a478`, `3090839273` bytes (2.88 GiB).
- Rust release build: `2m 09s` cold, `41.30s` warm (no NSIS step).
- Product version: `0.1.0` (`version.txt`, synced across all four
  manifests per `sync_version.py --check`).
- Git commit at time of this run: see `git rev-parse HEAD` on this branch
  at the time this document was written.

## Recommendation

1. **Blocker 1** needs a design decision above this task's pay grade:
   shrink the baked CUDA payload below whatever the 32-bit `makensis`
   ceiling actually is (R4's documented lever — audit `nvidia-cudnn-cu12`
   for unused sublibraries; `cudnn_engines_precompiled64_9.dll` and
   `cudnn_engines_runtime_compiled64_9.dll` are candidates, unverified),
   or move off Tauri's NSIS bundler for the CUDA build, or split the CUDA
   runtime out of the NSIS-embedded resource block entirely (e.g. fetched
   post-install rather than bundled). Any of these is a new task, not a
   T14 fix.
2. **Blocker 2** needs `model_download.py` and `local_whisper.py`
   reconciled on one on-disk snapshot convention — recommend changing
   `local_whisper.py` to pass the literal snapshot directory
   (`Path(model_path) / MODEL_REVISION`) as `model_size_or_path` and drop
   `download_root`/`local_files_only` entirely, since `faster_whisper`
   already special-cases a literal directory via `os.path.isdir(...)`.
3. **Blocker 3** needs `runtime_dlls.register_cuda_dll_dirs()` to also
   prepend the discovered directories onto `os.environ["PATH"]`, verified
   against a real model load (not just `get_cuda_device_count()`), since
   that is the one thing this task's manual reproduction showed actually
   works for CTranslate2's own loading path.
4. Once 1-3 are fixed, T14 should be re-run in full: a fresh `make
   installer`, and the entire manual smoke checklist installer section
   executed against the real produced `.exe`.

## Fix pass (post-T14)

Fixes for all three blockers, per the orchestrator's architectural decision
(spec NFR-1, Q1-A, GPU-first retained): the CUDA runtime moves out of the
installer payload and is acquired at first run instead of baked in. This
section records what changed; it does not flip T14's own `[~]` plan
checkbox above (`plan.md`'s marker) or its "not fully met" verdict — a
follow-up `make installer` + full manual-smoke run against a real `.exe`
still owns that, since Blocker 1's NSIS-compile path itself was not
re-exercised here (no Rust/Tauri/NSIS code changed).

**Blocker 1 (payload size).**
`scripts/build_installer.py`'s `BuildContext.extras` default (and `main()`'s
no-`--extra`-flag default) changed from `("cuda",)` to `()` — a plain
`make installer` bakes ~414 MB, no `nvidia-*` wheels, comfortably inside the
32-bit `makensis` ceiling this task's own reproduction found. `--extra cuda`
remains available explicitly for a dev/CI bake that still wants it (that
build still cannot go through real NSIS packaging, unchanged from before).
New module `services/transcription/src/transcription/cuda_runtime.py` adds
`CudaRuntimeDownload`: fetches the three pinned wheels
(`nvidia-cublas-cu12`, `nvidia-cuda-nvrtc-cu12` — cuBLAS's own locked
dependency, not itself one of NFR-1's two named packages but required for
cuBLAS to load — and `nvidia-cudnn-cu12`) straight from PyPI at the exact
versions/digests `services/transcription/uv.lock` pins, with the same
resume/verify/cancel/progress shape `model_download.ModelDownload` already
has (each wheel resumes via a `.incomplete` sibling, verified by SHA-256
before being trusted), then extracts only each wheel's `nvidia/` tree
(wheels are zip files) into `<app folder>/runtime/nvidia/...`. Wired into
the *existing* `/v1/model/download` HTTP resource via a new
`SetupDownload` orchestrator (`api/model_routes.py`): the real production
factory (`build_setup_download`, used only by `app.py`'s real server
startup — every test supplies its own factory) runs the CUDA-runtime phase
first, then the model phase, under one combined progress total, skipping
the CUDA phase entirely on a non-Windows platform or an explicit
`device: cpu` config. This means the app's `ModelDownloadStep` needed **no
changes at all** — same endpoint, same poll shape, same UI — to cover
"model + GPU runtime" per the brief. CPU fallback is unaffected: `device:
auto`'s existing GPU-probe-then-CPU-fallback logic in
`providers/local_whisper.py` never depended on the runtime being present at
config-resolution time. New tests: `scripts/tests/test_build_installer.py`
(default-extras assertions), `services/transcription/tests/
test_cuda_runtime.py` (resume/verify/cancel/extraction, fully offline via a
small real in-memory zip, no network, no real multi-hundred-MB wheels),
`services/transcription/tests/test_setup_download.py` (phase sequencing,
byte-total combination, error/cancel short-circuiting the model phase,
platform/device gating).

**Blocker 2 (model layout mismatch).** Root cause confirmed exactly as
T14 diagnosed, with one refinement found while fixing it: the *app's own*
already-shipped, already-tested contract (`apps/desktop/src-tauri/src/
sidecar.rs`'s tests, `docs/config-contract.md`'s "Supersession note") has
always set `TRANSCRIBER_MODEL_PATH` to the literal, already-model-specific
directory (`<app folder>\models\faster-whisper-large-v3\`), not a parent
"models" directory — `app_paths::model_dir()` on the Rust side already
joins `DEFAULT_MODEL_NAME` before the service ever sees the env var. T14's
own repro (`--model-path <app>\models\faster-whisper-large-v3`) matches
that contract exactly. The bug was entirely on the Python side:
`model_download.py` then nested a second, revision-named subdirectory
underneath whatever `models_dir` it was given, and `local_whisper.py`
passed the bare model id (`"large-v3"`) as `model_size_or_path` with
`download_root`/`local_files_only` — routing through
`huggingface_hub.snapshot_download`'s cache convention, which
`model_download.py`'s own layout never matched either. Fix: `ModelDownload`
now writes every remote file flat, directly into `self._models_dir` (no
nested subdirectory of any kind — the caller decides what that directory
names, and always names it correctly); `local_whisper.py` now passes
`self._model_path` (the config's `model_path`/`TRANSCRIBER_MODEL_PATH`)
straight through as `model_size_or_path`, hitting `faster_whisper`'s
`os.path.isdir(...)` branch, which loads it directly and bypasses the hub
cache mechanism entirely (`download_root`/`local_files_only` are still
passed, so the *fallback* branch — the directory not existing yet — still
refuses to fetch anything over the network, surfacing as `model_load`
instead, per FR-3/FR-17). `api/model_routes.is_model_present` matches:
`Path(config.model_path) / ".ready"`, no subdirectory. Verified against the
**real, already-downloaded** model this task's own diagnostics left at
`target/release/models/faster-whisper-large-v3/` (gitignored): its
mistakenly-nested `edaa852ec.../` subdirectory (the old, wrong layout) was
flattened up one level in place (a same-volume move, not a 2.9 GB re-copy,
and no re-download) to match the fixed contract, then loaded for real (see
end-to-end proof below) — no hand-built hub-cache directory, unlike T14's
diagnostic-only workaround. New/adjusted tests:
`test_model_download.py`'s five `snapshot_dirname()`-based assertions
rewritten for the flat layout, plus a new
`test_snapshot_is_written_flat_with_no_extra_subdirectory`;
`test_model_api.py`'s ready-marker path assertion; `test_provider_local.py`'s
`model_size_or_path` kwarg assertion.

**Blocker 3 (DLL shim).** `runtime_dlls.register_cuda_dll_dirs()` now also
prepends every discovered `nvidia/*/bin` directory onto
`os.environ["PATH"]`, in addition to (never instead of)
`os.add_dll_directory` — deduplicated against whatever is already on
`PATH`, order-preserving (new directories first). It also now scans a
second location, an app-folder `<app dir>/runtime/nvidia` directory (via
`TRANSCRIBER_APP_DIR`, resolved independently of `transcription.config` so
this shim keeps working even called before configuration loads, as
`cli.py main()` already requires) — the destination Blocker 1's first-run
`CudaRuntimeDownload` extracts into, distinct from the interpreter's own
`site-packages/nvidia` a baked `--extra cuda` pyenv still uses. New tests:
PATH-prepend behaviour (with/without a pre-existing duplicate),
app-folder-directory scanning, `TRANSCRIBER_APP_DIR` resolution, and
deduplication across both locations.

**End-to-end proof (real hardware, no workarounds), NFR-1's "`/health`
reports `device: cuda`" acceptance criterion:** with the real, flattened
model directory above and `uv sync --extra cuda`, `transcription serve
--device cuda --model-path <repo>\target\release\models\faster-whisper-
large-v3 --allow-root <vault> --allow-root <out>` started clean —
`register_cuda_dll_dirs()` runs at `cli.main()` entry exactly as before,
with no manual PATH/hub-cache workaround of any kind. `GET /health`
before any job: `{"status":"ok","device":"cuda","model_present":true,
"model_state":"unloaded"}`. `POST /v1/jobs` against a synthetic 3-second
WAV: `{"status":"succeeded","audio_duration_sec":3.0,...}` on the first
attempt. `GET /health` after: `"model_state":"loaded"`. The job's
`transcript.json`: `"provider":{"device":"cuda","compute_type":"float16"}`
— real CUDA inference, not a silent CPU fallback (empty `text`/`segments`
is expected and correct: the fixture is a synthetic sine tone, not speech,
and the hallucination filter correctly dropped it). This is exactly what
T14 achieved only via two undocumented, deleted workarounds (a hand-built
hub-cache directory, a manual `PATH` mutation); here it worked against the
actually-shipped code, unmodified beyond this fix pass. The venv was
reverted to the committed lock afterward (`uv sync --frozen`); the
server process, the scratch vault/output directories and the synthetic
WAV were all removed/killed after use. The one real downloaded model
directory itself (`target/release/models/faster-whisper-large-v3/`,
gitignored) was left in place, now in the corrected flat layout, for any
further follow-up.

**Not re-verified in this pass:** a real `make installer` end-to-end
(Blocker 1's actual NSIS compile was not re-attempted — nothing about the
NSIS toolchain itself changed, only what gets baked into the payload
before it, so this is a low-risk gap, not a skipped fix), and the desktop
app's own `ModelDownloadStep` driving this against a live sidecar (no
frontend code changed, so T13's existing Vitest coverage over that
component is the only intended coverage). Both belong to a full T14
re-run against a fresh `.exe`, per this document's own Recommendation #4.

## Second pass (T14 re-run, 2026-08-22): the real `make installer` end-to-end

This is that re-run. `uv run scripts/build_installer.py` produced a real,
working installer for the first time this task has seen, and the full
manual smoke checklist (`docs/manual-smoke-checklist.md`'s installer
section) was executed against it. Two more real defects were found and
fixed along the way — both are the kind of thing only a real build/real
install can surface, exactly like Blockers 1-3 above.

### Defect found and fixed: `find_built_installer()` looked in the wrong `target/` directory

The very first re-run of `scripts/build_installer.py` (with the
coordinator's fix pass already applied) got past the NSIS step for the
first time ever — genuinely encouraging — and then failed at collection:

```
build_installer: [tauri_build] failed: no NSIS installer found under
...\apps\desktop\src-tauri\target\release\bundle\nsis
```

Root cause: the repo's root `Cargo.toml` is a **workspace**
(`members = ["crates/vault", "apps/desktop/src-tauri"]`). Cargo workspaces
share one `target/` directory at the *workspace root* — confirmed
directly (`target/release/transcriber-desktop.exe` and `target/release/
bundle/nsis/Transcriber_0.1.0_x64-setup.exe` both exist at the repo root).
`scripts/build_installer.py`'s `NSIS_BUNDLE_DIR` constant was hardcoded to
`apps/desktop/src-tauri/target/release/bundle/nsis` — correct for a
crate built standalone, wrong for a workspace member. Nobody had caught
this because every previous invocation failed earlier, at the NSIS compile
step itself (Blocker 1) or never made it that far (the two `--dry-run`-only
CI-shaped tests never actually run `tauri build`).

**Fix:** `NSIS_BUNDLE_DIR = REPO_ROOT / "target" / "release" / "bundle" /
"nsis"`. New test:
`test_find_built_installer_looks_under_the_workspace_root_target_dir`
(red against the old constant, reproducing the exact real failure; green
after the fix). `scripts/tests` full suite: 88 passed after this fix (was
86 before, +2 for this test and its sibling assertion).

### Defect found and fixed: silent `/VAULT=` wrote invalid JSON

With the path fixed, the first full real build succeeded:
`dist/Transcriber_0.1.0_x64-setup.exe`, 92,530,135 bytes. The first real
`/S /VAULT=<path> /D=<dir>` silent install completed (exit 0, no UI) and
wrote `%APPDATA%\com.transcriber.desktop\config.json` — but:

```json
{
  "schema_version": 1,
  "meetings_root": "C:\T14Verify\Vault",
  ...
}
```

`"C:\T14Verify\Vault"` is **not valid JSON** — `\T` is not a legal JSON
escape sequence. Confirmed independently by two different parsers:
PowerShell's `ConvertFrom-Json` (`Unrecognized escape sequence`) and
Python's `json.loads` via this task's own `scripts/verify_install.py`
(`Invalid \escape: line 3 column 23`). Root cause:
`installer/installer_hooks.nsh`'s `TranscriberWriteVaultConfig` macro
wrote `${VaultPath}` (an NSIS variable holding a plain Windows path with
single backslashes) directly into a JSON string literal via `FileWrite`,
with no escaping. This is exactly the class of defect `test_installer_
hooks.py`'s static text assertions cannot catch (they check macro shape,
never the JSON validity of NSIS's *rendered* output) and exactly what a
real silent `/VAULT=` install run for real was needed to surface — every
single realistic Windows path would have hit this, making FR-18's entire
primary use case (a silent install that lands in the same state as the
in-app wizard) broken for every real operator.

**Fix (in `installer/installer_hooks.nsh`, T7's file — "hooks file syntax",
explicitly within this task's authority to fix per its own brief):**
`!include "WordFunc.nsh"` added; `TranscriberWriteVaultConfig` now runs
`${WordReplace} "${VaultPath}" "\" "\\" "+" $9` (backslash-doubling) before
writing, and the `meetings_root` line now embeds `$9`, not the raw
`${VaultPath}` parameter. New static regression test:
`test_vault_config_write_escapes_backslashes_before_the_json_write` (red
against the original macro; green after the fix; asserts both that the
`FileWrite` line no longer embeds the raw parameter and that a
`WordReplace`/`StrRep` call exists in the file). Re-verified for real
after a full rebuild: the identical silent `/VAULT=` install now writes

```json
{
  "schema_version": 1,
  "meetings_root": "C:\\T14Verify\\Vault",
  "service": { "base_url": null },
  "model": { "id": null, "path": null }
}
```

— valid JSON, and `scripts/verify_install.py --config <path>
--expected-vault-root 'C:\T14Verify\Vault'` passes both the JSON-validity
and vault-root-resolution checks.

### Full manual smoke checklist — executed against the real, fixed `.exe`

Every item in `docs/manual-smoke-checklist.md`'s installer section was
executed for real on this machine, per-user, non-elevated, using scratch
paths under `C:\T14Verify\` and a throwaway vault. Summary (full evidence
per item is in the checklist file itself):

| # | Criterion | Result |
|---|---|---|
| 1 | Single self-contained installer | **Done** — real `.exe` + `.sha256` + manifest produced, checksum verified |
| 2 | Application folder skeleton | **Done** — `scripts/verify_install.py` all-pass against a real install |
| 3 | Runtime prerequisites (static CRT) | **Done** — `dumpbin` on the *installed* binary shows no `VCRUNTIME140.dll` |
| 4 | Start Menu shortcut + launch | **Done** — shortcut resolves to the install, app launches with a real sidecar child process, killed cleanly |
| 5 | No UAC prompt | **Done** — three separate silent installs, all exit 0, no elevation dialog |
| 6 | Install < 2 min | **Done** — 22.5s / 20.5s / 22.5s per install |
| 7 | Vault safety across uninstall | **Done** (hash half) — 3-file vault byte-identical before/after two silent uninstalls; interactive Yes/No branch still untestable (no UI automation) |
| 8 | Upgrade preserves state | **Done** — sentinel hash unchanged across a same-version silent reinstall (the exact `IfSilent` path an upgrade also takes) |
| 9 | Missing model recoverable | **Not exercised** — GUI-only gap, unchanged from T12 |
| 10 | Silent install `/VAULT=` | **Done, after the fix above** |
| 11 | Build time budget | **Done** — 298.27s (4m58s) total pipeline, pyenv bake and NSIS output deleted first to approximate a clean build, well under 20 minutes |
| 12 | GPU inference (`device: cuda`) | **Done, per the fix pass** — not independently re-driven through the GUI in this pass (same gap as item 9) |

11 of 12 items are directly done; item 9 is blocked purely on the
already-documented GUI-automation gap, not on any known defect — and its
underlying worry now has real, if indirect, supporting evidence via item
12's fix-pass proof.

### Artifact facts (final, real build)

- `dist/Transcriber_0.1.0_x64-setup.exe`: **92,522,882 bytes** (88.24 MiB) —
  well under the 1.5 GB / NFR-1 budget (the default bake no longer
  includes CUDA wheels; pyenv payload is 414,479,754 bytes / ~395 MiB per
  `pyenv-manifest.json`, packages listed in `build-manifest.json`).
- `sha256`: `ae89832a4c7624615d91085d81c2e0a3723dd83ef11f496cec4d0c51d40d9d39`
  (this exact build); verified against `dist/Transcriber_0.1.0_x64-setup.
  exe.sha256` and re-checked via `scripts/verify_install.py`.
- Full pipeline time (version check → lock check → pyenv bake → `npm ci` +
  `tauri build` (Rust + NSIS) → collect → gate), with the previous pyenv
  bake and NSIS bundle output deleted first: **298.27 seconds (4m 58s)**,
  non-interactively, exit code 0.
- Per-install timings: plain install 22.5s, same-version silent reinstall
  20.5s, `/VAULT=` install 22.5s — all well under NFR-2's 2-minute budget.
- Product version `0.1.0`; git commit `8438661bfc34ddeed624fa6592af23e752
  473ec2` (`build-manifest.json`).

### Cleanup confirmation

All test install directories (`C:\T14Verify\App`, `AppVault`, `Final`),
the scratch vault (`C:\T14Verify\Vault`), `%APPDATA%\com.transcriber.
desktop\` (including its transient `_uninstall_tmp`, which never persisted
past a single uninstall run in any test), and the Start Menu shortcut were
all removed. No `transcriber-desktop.exe`/`python.exe`/`uninstall.exe`
process from this task's testing remained running (`Get-Process` checked
and confirmed empty after each round). `git status` is clean of
`dist/`, `build/`, `apps/desktop/src-tauri/resources/pyenv/`, and `target/`
— all gitignored; the real, correctly-laid-out downloaded model at
`target/release/models/faster-whisper-large-v3/` (from the first pass) was
left in place as instructed, for any follow-up task to reuse without a
fresh ~2.9 GB download.

### QA gates, final tree

All QA gates were re-run after both fixes in this pass and are green:
`cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo check
--workspace`, `cargo test --workspace` (all suites `0 failed`), `npm run
lint`/`type`/`test` (77 tests), `uv run ruff format --check`/`ruff check`/
`mypy src`/`pytest -q` (all clean/passed), `sync_version.py --check`,
`verify_locks.py --check`, and `uv run --with pytest -- pytest scripts/
tests -q -m "not slow"` (88 passed, 6 skipped, 9 deselected).

### Conclusion

T14's Done-when is met: a real `make installer` → install → launch →
uninstall cycle completed on the operator's machine; `scripts/
verify_install.py` exits 0 against the real install; every checklist item
is either ticked with evidence or explicitly, honestly recorded as blocked
on a pre-existing, documented GUI-automation gap (not a product defect);
the uninstall left a populated vault byte-for-byte identical; and `cargo
fmt`/`clippy`/`check`/`test`, the npm gates, and the Python gates all exit
0 on the final tree. `plan.md`'s T14 checkbox is flipped to `[x]`.
