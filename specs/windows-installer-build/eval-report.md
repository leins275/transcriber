---
slug: windows-installer-build
base_ref: 8438661bfc34ddeed624fa6592af23e752473ec2
round: 3
---

# Evaluation report: Windows installer and build system

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 1 | 2 | 0 |
| major | 1 | 4 | 0 |
| minor | 1 | 6 | 2 |

Round 3, final (fix budget exhausted). Two of the three round-2 findings are
genuinely fixed and were re-reproduced here: E14 (both uninstall message
boxes now name `$INSTDIR\models` **and** `$INSTDIR\runtime` and quote 4.4 GB)
and E15 (`npm run format:check` exits 0 before a `sync_version.py --set
9.9.9` → `--set 0.1.0` round trip, exits 0 after it, and the manifests'
`git diff --stat` is byte-for-byte the same on both sides — `_detect_newline`
sniffs each file's raw bytes and `write_text(..., newline=...)` reproduces it,
with `.gitattributes` pinning `apps/desktop` JSON/TS to LF). E13 is **only
half fixed**: the in-session path works end to end (fake service → Rust seam →
`ModelDownloadStep`'s "GPU acceleration is not installed" alert with a Retry
that short-circuits the model phase — `test_a_retry_of_the_cuda_phase_skips_
the_model_phase_when_already_present` proves the short-circuit), but the
`cuda_runtime_present` field the fix added to `/health` and plumbed all the
way into the TypeScript `ModelDownloadStatus` type has **no consumer** — no
component reads it — so one app restart returns the operator to exactly the
round-2 state: model present, CUDA runtime missing, nothing rendered, no
retry, silent CPU inference forever.

The fix pass also exposed, but did not introduce, a new blocker in the same
flow. `ModelDownloadStep.handleStart` decides whether to poll from the state
carried by the `POST /v1/model/download` response, and on the production
`SetupDownload` path that state is deterministically `idle`, not
`downloading`: `CudaRuntimeDownload.start()` performs an
`already_present()` filesystem probe *before* flipping `self.state`, which
yields the GIL back to the request thread. Re-reproduced with the real
classes and a no-network transport, 15/15 `idle` on the GPU path versus 15/15
`downloading` on the model-only path. The consequence on the operator's own
machine is that clicking "Start download" renders no bytes, no percent and no
Cancel for the entire ~4.4 GB acquisition — FR-12's two acceptance criteria
("shows progress in bytes and percent … at least once a second", "Cancel
stops the transfer") are not met on the only path a fresh GPU install takes.
This is precisely the gap `docs/verification-installer.md` itself flags as
smoke item 9 "Not exercised — GUI-only gap" and the `desktop` profile warns
about ("`make test` … does not prove the app launches — drive the affected
flow").

All QA lanes are green on the final tree and no regression was introduced by
the fix pass: `scripts/tests` 95 passed / 6 skipped / 9 deselected, service
`pytest` exit 0 (300 passed, 2 skipped) with `ruff format --check`, `ruff
check` and `mypy src` clean, `cargo test --workspace` 149+8+17+… all `0
failed` with `cargo fmt --all --check` and `clippy --workspace --all-targets
-- -D warnings` exit 0, vitest 80 passed, `npm run lint`/`type`/`format:check`
exit 0.

## Findings

### E1 [blocker] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/cuda_runtime.py:201,324`,
  `services/transcription/src/transcription/runtime_dlls.py`
- **Spec ref**: FR-12 acceptance, FR-17, NFR-1 acceptance
- **Round 3 re-check**: unchanged and intact — `register_cuda_dll_dirs()` is
  still called both in the `already_present()` short-circuit (line 201) and
  immediately after `_extract_nvidia_trees` (line 324), still covered by
  `test_cuda_runtime.py::test_start_re_registers_cuda_dll_dirs_after_extraction_so_no_restart_is_needed`.

### E2 [blocker] [spec-drift] [status: fixed]

- **Where**: `apps/desktop/src-tauri/src/config.rs:200,213` (`is_inside`,
  `probe_writable`), `commands.rs`, `api.ts::chooseMeetingsFolder`
- **Spec ref**: FR-10 and acceptance; FR-14
- **Round 3 re-check**: both helpers still present and still run before any
  persistence; the `defaultPath` forwarding into `open()` survives the fix
  pass (`api.test.ts` case still green).

### E3 [major] [correctness] [status: fixed]

- **Where**: `installer/installer_hooks.nsh:180-199,240-255`
- **Spec ref**: FR-16, NFR-2/NFR-5
- **Round 3 re-check**: `$INSTDIR\runtime` relocate/restore both still
  present, tied to `$R7`, still asserted by
  `test_installer_hooks.py::test_upgrade_preserves_cuda_runtime_by_relocating_out_of_instdir`.

### E4 [major] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/api/model_routes.py:75-116,208-240`
- **Spec ref**: FR-17, spec "Out of scope: CPU-only optimization"
- **Round 3 re-check**: `_nvidia_gpu_present()` gate, the `CANCELLED`
  abort/`ERROR` continue split, and the auto-only CPU fallback in
  `local_whisper.py` are all unchanged and still covered.

### E5 [major] [spec-drift] [status: fixed]

- **Where**: `scripts/build_installer.py`
- **Spec ref**: FR-4; plan T8
- **Round 3 re-check**: re-reproduced — `uv run scripts/build_installer.py
  --dry-run` still prints `npm --prefix … run tauri -- build -- --locked`.

### E6 [major] [correctness] [status: fixed]

- **Where**: `README.md`, `docs/setup.md`, `installer/README.md`
- **Spec ref**: FR-6, plan T12
- **Round 3 re-check**: unchanged. Residual nit still standing (not
  reopened): `scripts/tests/test_build_pyenv.py:11`'s docstring still says
  "the release bake additionally passes `--extra cuda`". New residual of the
  same class: nothing in `docs/` documents the `/health` fields
  `model_present` / `cuda_runtime_present` that are now a cross-process
  contract between F2 and the Rust seam — `docs/config-contract.md` covers
  only `config.json`.

### E7 [minor] [security] [status: fixed]

- **Where**: `services/transcription/src/transcription/cuda_runtime.py:234-249`
- **Round 3 re-check**: unchanged; the corrupt-pre-existing-wheel test still
  passes.

### E8 [minor] [correctness] [status: fixed]

- **Where**: `apps/desktop/src/lib/modelDownload.ts:34`
- **Round 3 re-check**: `MODEL_DOWNLOAD_POLL_INTERVAL_MS = 1000` still.
  Stale comment still there: `ModelDownloadStep.tsx:33-34` continues to claim
  "production uses F3's existing 1.5s job-poll cadence".

### E9 [minor] [correctness] [status: accepted]

- **Where**: `services/transcription/src/transcription/api/model_routes.py:171-177`
- **Spec ref**: FR-12 (progress display)
- **Actual**: `SetupDownload.total_bytes` under-reports during phase one, so
  the percentage climbs toward 100 % and then collapses when the model phase
  starts.
- **Disposition (unchanged)**: fixing it reorders the phases' error semantics
  or changes the wire shape. Left for a follow-up. Note that E16 makes this
  moot in practice today — on the GPU path no percentage is rendered at all.

### E10 [minor] [correctness] [status: accepted]

- **Where**: `installer/installer_hooks.nsh:167-199`
- **Spec ref**: FR-14, FR-16
- **Actual**: `Rename` cannot move a directory across volumes and its return
  value is never checked, so an install placed by `/D=` on a different drive
  from `%APPDATA%` loses the relocated payload to the core uninstall section.
- **Disposition (unchanged)**: a redesign of the mechanism in a file that
  cannot be compiled on this machine; the operator's Q4-A install is
  same-volume by construction.

### E11 [minor] [performance] [status: fixed]

- **Where**: `scripts/build_installer.py`, `scripts/verify_install.py`
- **Round 3 re-check**: unchanged (chunked hashing, `shutil.copyfile`).

### E12 [minor] [improvement] [status: fixed]

- **Where**: `.gitignore`
- **Round 3 re-check**: after running every suite in this round, `git status`
  shows zero `__pycache__`/`.pyc` entries.

### E13 [major] [correctness] [status: fixed]

- **Where**: `services/transcription/src/transcription/app.py:165-176`
  (`cuda_runtime_present`), `apps/desktop/src-tauri/src/commands/model.rs:67,87`,
  `apps/desktop/src/lib/modelDownload.ts:26`,
  `apps/desktop/src/components/ModelDownloadStep.tsx:105-122`
- **Spec ref**: FR-12 ("working cancel and retry"), FR-17 ("a failed …
  download never leaves a broken install … offers a retry"), NFR-1 acceptance
  ("a local transcription job … runs on `cuda`")
- **What is genuinely fixed**: the in-session path. `SetupDownload.cuda_warning`
  now reaches the UI through every layer — `/v1/model/download` body → `http.rs`
  `ModelDownloadResponse.cuda_warning` (`#[serde(default)]`, decode test at
  `http.rs:885`) → `ModelDownloadStatusView` → `ModelDownloadStep`'s
  `role="alert"` notice, verbatim, with a "Retry GPU setup" button
  (`ModelDownloadStep.test.tsx:52`, `App.test.tsx:349`, and the Rust seam test
  `fake.rs:698` asserting the fake flips `cuda_runtime_present` and carries the
  message unchanged). The re-POST is now cheap and safe: `SetupDownload.start()`
  short-circuits `self._model.already_present()` to `COMPLETE` instead of
  re-fetching ~3 GB from byte zero, asserted by
  `test_setup_download.py::test_a_retry_of_the_cuda_phase_skips_the_model_phase_when_already_present`.
- **What is still open**: `cuda_warning` lives only on the in-memory
  `SetupDownload` instance. The fix added `/health.cuda_runtime_present`
  specifically for the durable case — its own comment says "so the UI can
  detect 'model present, CUDA runtime missing' even outside an active
  `cuda_warning` (e.g. a fresh process after an earlier run's failed
  download)" — carried it through `ServiceHealth`, `ModelDownloadStatusView`
  and the TS `ModelDownloadStatus` type … and then never read it. A grep for
  `cuda_runtime_present` across `apps/desktop/src` returns only the type
  declaration and two test fixtures. So after the app is closed and reopened,
  the fresh sidecar has no `SetupDownload`, the status body carries no
  `cuda_warning`, `ModelDownloadStep` hits `if (status.model_present) { if
  (!status.cuda_warning) return null; }` and renders nothing. Nothing else in
  the UI surfaces the resolved device either (`ServiceHealth` does not carry
  `/health.device`). Net effect on the operator's GPU-first machine: identical
  to round 2, one restart later — permanent silent CPU inference with full
  `large-v3`, ~1.4 GB of resumable `.incomplete` wheels on disk and no way to
  resume them.
- **Suggested fix**: change the guard to
  `if (!status.cuda_warning && status.cuda_runtime_present !== false) return null;`
  and render the same notice (the Retry already does the right thing). While
  there, align the two gates: `app.py` gates `cuda_runtime_present` on
  `_nvidia_gpu_present()` alone, whereas `build_setup_download` also skips the
  CUDA phase on non-Windows and on an explicit `device: "cpu"` — so a
  CPU-configured host with an NVIDIA card would report `false` and, once the
  field is consumed, prompt for a runtime it will never download.

### E14 [minor] [correctness] [status: fixed]

- **Where**: `installer/installer_hooks.nsh:158-160,236-239`
- **Spec ref**: FR-14 ("never silently orphaning gigabytes on disk")
- **Round 3 verification**: both message boxes now name both directories.
  The uninstall prompt reads "Also delete the downloaded transcription model
  and GPU runtime files (about 4.4 GB total) at … `$INSTDIR\models` …
  `$INSTDIR\runtime`", and the keep-branch confirmation reads "The downloaded
  model and GPU runtime files were kept at … `$INSTDIR\models` …
  `$INSTDIR\runtime`". The quoted size moved from 3 GB to 4.4 GB, which
  matches ~3 GB of weights plus ~1.4 GB of extracted CUDA DLLs.
  Residual, accepted: the keep-branch confirmation is nested inside the
  `models\` restore branch, so an uninstall where `runtime\` exists but
  `models\` never landed (cancel during the model phase) restores ~1.4 GB with
  no closing message — the prompt itself did name the directory, so FR-14's
  "no way to find it" clause is still satisfied.

### E15 [minor] [improvement] [status: fixed]

- **Where**: `scripts/sync_version.py:66-76,86-92,104-115,151-155`,
  `.gitattributes`
- **Spec ref**: NFR-6, FR-5
- **Round 3 verification**: re-reproduced end to end on this tree.
  `npm --prefix apps/desktop run format:check` → exit 0; `sync_version.py
  --set 9.9.9` → `--check` OK → `format:check` → exit 0; `--set 0.1.0` →
  `--check` OK → `format:check` → exit 0, and `git diff --stat` for
  `package.json` / `tauri.conf.json` / both `Cargo.toml`s / `pyproject.toml`
  is identical before and after the round trip (no line-ending churn).
  `_detect_newline` sniffs raw bytes and every write passes `newline=` through,
  `version.txt` included; `.gitattributes` adds `* text=auto` plus explicit
  `eol=lf` for `apps/desktop/**/*.{json,ts,tsx,md}`.

### E16 [blocker] [correctness] [status: fixed]

- **Where**: `apps/desktop/src/components/ModelDownloadStep.tsx:87-94`
  (`handleStart`), `services/transcription/src/transcription/api/model_routes.py:279-301`
  (`ModelDownloadManager.start`), `services/transcription/src/transcription/cuda_runtime.py:191-206`
- **Spec ref**: FR-12 acceptance ("The model download shows progress in bytes
  and percent and updates at least once a second"; "Cancel stops the transfer
  and leaves the install in a defined, retryable state"), plan T13 Done-when
- **Expected**: clicking "Start download" in the first-run wizard shows a live
  byte/percent readout and a Cancel control for the duration of the transfer.
- **Actual**: the component starts polling only if the state carried by the
  `POST` response is `downloading`/`verifying`
  (`if (isInProgress(next.state)) beginPolling()`), and on the production
  path that state is `idle`. `ModelDownloadManager.start()` spawns the worker
  thread and then immediately reads `download.state`; `SetupDownload.start()`
  begins with `CudaRuntimeDownload.start()`, whose first real statement is the
  `already_present()` filesystem probe — a GIL-releasing syscall that lets the
  request thread run before `self.state = DOWNLOADING` is reached. F2's own
  test concedes the ambiguity (`test_model_api.py:118`: `assert body["state"]
  in {"downloading", "idle"}`); the frontend does not.
  Re-reproduced here with the real `SetupDownload`/`CudaRuntimeDownload`/
  `ModelDownload` classes and a no-network transport: **15/15 `idle` on the
  GPU (SetupDownload) path, 15/15 `downloading` on the model-only path**, and
  10/10 `idle` again when the CUDA runtime is already present. So on the
  operator's machine (Windows + `nvidia-smi` on PATH ⇒ `build_setup_download`
  returns a `SetupDownload`) the wizard sets `status` to an `idle` snapshot,
  renders the "Start download / Skip for now" screen again and never polls,
  while ~4.4 GB downloads invisibly in the background: no bytes, no percent,
  no Cancel, and no completion transition — the panel stays frozen until the
  app is restarted. A second click on "Start download" happens to recover
  (the manager's `state is DOWNLOADING` guard then returns a live status and
  polling begins), but nothing tells the operator to do that.
  A related dead end rides on the same line: a `POST` that returns `complete`
  (the CUDA phase short-circuiting, or a finished transfer) with
  `model_present` still `false` matches none of the render branches, leaving a
  panel containing only the `<h2>` and no controls at all.
- **Why it was not caught**: `ModelDownloadStep.test.tsx`'s fake command layer
  returns `downloading` from `start()`, and `docs/verification-installer.md`
  records the wizard itself as never driven against a live sidecar (smoke item
  9, "Not exercised — GUI-only gap"; the real 3 GB download was driven through
  the CLI). This is exactly the `desktop` profile's Verification rule.
- **Suggested fix**: do not trust the transient POST state — call
  `beginPolling()` unconditionally after a successful `start()` (the poller
  already self-stops on the first non-in-progress status, so a genuinely
  finished transfer costs one extra request), and render an explicit branch
  for `complete`-with-`model_present:false`. Optionally also make
  `ModelDownloadManager.start()` set the phase state to `DOWNLOADING` before
  spawning the thread, so the wire contract stops permitting `idle`.

### E17 [minor] [correctness] [status: fixed]

- **Where**: `apps/desktop/src/components/ModelDownloadStep.tsx:105-122`
- **Spec ref**: FR-12 ("visible progress indicator … working cancel and
  retry")
- **Actual**: the E13 "Retry GPU setup" button calls the same `handleStart`,
  which sets `status` to the fresh `SetupDownload`'s snapshot — whose
  `cuda_warning` is `null` by construction. With `model_present` still `true`,
  the render guard returns `null`, so the notice disappears the instant it is
  clicked and the ~1.4 GB CUDA re-download runs with no progress, no cancel,
  no success confirmation and (per E16) no polling to bring the outcome back.
  A retry that fails again is therefore silent until the next process restart
  — where E13's missing `cuda_runtime_present` consumer makes it silent
  forever.
- **Suggested fix**: falls out of E13 + E16 — consume `cuda_runtime_present`
  in the guard and poll unconditionally; the notice then shows progress and
  re-renders on failure.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (monorepo layout) | repo tree: `apps/desktop/`, `services/transcription/`, `installer/`, `scripts/`, `Makefile` | `test_makefile_targets.py`, `test_installer_hooks.py` | ✓ |
| FR-2 (Makefile fanout) | `Makefile:14-56` | `test_makefile_targets.py::test_fanout_target_invokes_all_three_languages`, `::test_make_dry_run_resolves` | ✓ |
| FR-3 (bootstrap) | `scripts/bootstrap.ps1` | `test_bootstrap.py` (7 cases incl. Store-stub row) | ✓ |
| FR-4 (frozen locks) | `scripts/verify_locks.py`, `build_pyenv.check_lock_fresh`, `npm ci`, `tauri -- build -- --locked` | `test_locks.py`, `test_build_pyenv.py::test_bake_fails_loudly_when_the_lock_is_stale`, `test_build_installer.py::test_stage_tauri_build_passes_locked_through_to_the_tauri_cli` | ✓ |
| FR-5 (version SoT) | `version.txt`, `scripts/sync_version.py` (+ newline preservation) | `test_version.py` (6 cases); re-reproduced round trip vs `format:check` | ✓ (E15 fixed) |
| FR-6 (one-command build) | `scripts/build_installer.py`, `Makefile:61` | `test_build_installer.py` | ✓ |
| FR-7 (single self-contained `.exe`) | `tauri.conf.json` `bundle.targets: ["nsis"]` | `test_bundle_config.py`; real 88.24 MiB build in `docs/verification-installer.md` | ✓ |
| FR-8 (app folder contents) | `installer_hooks.nsh:84-88`, `bundle.resources`, `app_paths.rs` | `test_installer_hooks.py`, `test_verify_install.py`, `app_paths.rs` unit tests | ✓ |
| FR-9 (WebView2 + CRT) | `webviewInstallMode: downloadBootstrapper`, `.cargo/config.toml` crt-static | `test_bundle_config.py` (2 cases); `dumpbin` evidence | ✓ (WebView2-absent branch untested — no such host) |
| FR-10 (vault pick + validation) | `config.rs::set_meetings_root` (+`is_inside`, `probe_writable`), `api.ts::chooseMeetingsFolder`, `installer_hooks.nsh:99-115` | `config.rs` tests (7), `api.test.ts` defaultPath case, `test_installer_hooks.py::test_silent_mode_parses_vault_option` | ✓ |
| FR-11 (config contract, superseded) | `docs/config-contract.md`, `sidecar.rs`, `app_paths.rs` | `sidecar.rs` production/env tests, `test_verify_install.py::test_resolve_vault_root_*` | ✓ (health-field contract undocumented — see E6 residual) |
| FR-12 (model download) | `model_download.py`, `cuda_runtime.py`, `api/model_routes.py`, `ModelDownloadStep.tsx` | `test_model_download.py`, `test_cuda_runtime.py`, `test_setup_download.py` (15), `test_model_api.py`, `ModelDownloadStep.test.tsx` (9) | **gap — E16** (no progress/cancel rendered on the GPU path), E13, E17, E9 |
| FR-13 (shortcuts, launch on finish) | stock Tauri NSIS template | `test_bundle_config.py::test_nsis_does_not_disable_start_menu_or_desktop_shortcut`; Start Menu verified manually | ✓ (finish-page "launch now" never exercised) |
| FR-14 (uninstall, vault untouched) | `installer_hooks.nsh:136-255`, `config.rs::is_inside` | `test_installer_hooks.py` (unrooted-delete, explicit model choice, runtime relocate), `config.rs` inside-app-folder test; manual vault hash comparison | ✓ (E14 fixed; E10 accepted) |
| FR-15 (checksum + manifest) | `build_installer.collect` | `test_build_installer.py` (2 cases) | ✓ |
| FR-16 (upgrade preserves state) | `installer_hooks.nsh` relocate/restore incl. `runtime\`; `SetupDownload` model short-circuit | `test_installer_hooks.py` (2 upgrade cases), `test_setup_download.py::test_a_retry_of_the_cuda_phase_skips_the_model_phase_when_already_present`; manual sentinel test | ✓ |
| FR-17 (missing model recoverable) | `/health.model_present` + `.cuda_runtime_present`, `ModelDownloadStep` skip/retry, `SetupDownload` continue-on-error | `test_model_api.py::test_health_reports_cuda_runtime_present_only_when_a_gpu_is_on_the_machine`, `test_setup_download.py`, `ModelDownloadStep.test.tsx`, `App.test.tsx:349` | gap — E13 (durable CUDA-missing state has no surface), E16 |
| FR-18 (silent install) | `installer_hooks.nsh:89-115` | `test_installer_hooks.py` (2 cases); real `/S /VAULT=` run | ✓ |
| FR-19 (CI workflow) | — | — | parked (spec "could"; T15 out of the wave graph) |
| NFR-1 (≤1.5 GB, no PyTorch, cuda) | `build_installer.gate_artifact_size`, no-extras bake, `cuda_runtime.py` first-run fetch | `test_build_installer.py` size-gate cases, `test_build_pyenv.py::test_no_pytorch_anywhere_in_the_tree`; 88.24 MiB artifact, real `device: cuda` job recorded | gap — the `cuda` half depends on a CUDA download whose failure is durable-invisible (E13) and whose progress is invisible (E16) |
| NFR-2 (<2 min install) | — | manual: 22.5 s / 20.5 s / 22.5 s | ✓ |
| NFR-3 (resume + verify) | `model_download.py`, `cuda_runtime.py` | `test_model_download.py` resume cases, `test_cuda_runtime.py` resume + corrupt-wheel cases | ✓ |
| NFR-4 (no UAC) | `nsis.installMode: currentUser` | `test_bundle_config.py::test_nsis_install_mode_is_current_user`; three real non-elevated installs | ✓ |
| NFR-5 (<20 min build) | `build_installer.py` | manual: 298 s end to end | ✓ |
| NFR-6 (QA targets exit 0) | `Makefile` | re-run this round: scripts 95 passed/6 skipped, service pytest + ruff + mypy clean, cargo test/fmt/clippy clean, vitest 80 passed, npm lint/type/format:check exit 0 | ✓ |
| NFR-7 (no Windows-isms in shared schema) | `docs/config-contract.md`, `app_paths.rs`, `runtime_dlls.py` no-ops off Windows | `test_setup_download.py::test_build_setup_download_skips_cuda_runtime_on_non_windows`, `test_runtime_dlls.py` | ✓ (but `/health.cuda_runtime_present` is not platform-gated — see E13's fix note) |

## Positive notes

- E15's fix is the right shape: sniff the file's own convention and reproduce
  it, rather than forcing LF and hoping git agrees. Keep `_detect_newline` —
  do not "simplify" it to a hard-coded `newline="\n"`, which would break the
  CRLF files (`.nsh`, `.ps1`) the same script family touches.
- The `SetupDownload` model short-circuit is exactly the right scope: it
  changes only the retry's cost, is asserted by a dedicated test, and leaves
  `ModelDownload.start()`'s own semantics alone.
- E14 names both directories *and* corrected the quoted size. Small, but it is
  the difference between an honest prompt and a technically-true one.
- The E2 validation shape (reject before `create_dir_all`, real write probe,
  case-insensitive prefix compare with the trailing-separator guard) and
  `_nvidia_gpu_present()`'s deliberate choice of a different question from
  `ctranslate2.get_cuda_device_count()` both survive unchanged and should stay
  that way.
- `docs/verification-installer.md` remains an unusually honest record — smoke
  items 9 and 12 are marked "Not exercised" rather than ticked, and E16 is
  precisely the defect hiding behind item 9. The document told the truth; the
  gap was simply never closed.
