---
slug: tauri-desktop-app
base_ref: 9885a26
round: 3
---

# Evaluation report: Tauri 2 desktop app with drag-and-drop processing

## Verdict

| Severity | Open | Fixed | Accepted |
|---|---|---|---|
| blocker | 1 | 1 | 0 |
| major | 1 | 8 | 0 |
| minor | 1 | 8 | 1 |

Round 3 — final (fix budget exhausted; verification only). The round-2 fixes are real. **E1 is genuinely fixed**: `reveal_command_line` now builds `/select,"<path>"` as one raw string and `run_reveal_command` appends it with `CommandExt::raw_arg` instead of `Command::args`, and I re-ran the empirical check myself — launching `explorer.exe` with exactly that command-line tail against a fixture folder named `260812 - Security issue` opened `…/RevealCheck/ELS/260812%20-%20Security%20issue` (enumerated via `Shell.Application.Windows()`), not Documents; the window I opened was closed and the fixture deleted. E16 (BOM), E17 (no 30 s dead window after the folder pick), E19 (banner wording) and E20 (fake mode survives a root change) are all fixed as claimed, each with a regression test. E18 is half fixed — `lib.rs` now re-reads `state.settings` after the await, so the startup task no longer installs a root nobody asked for; the out-of-order-application half remains and stays open as a minor. All eight QA gates pass as I ran them: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings` (exit 0), `cargo test --workspace` (109 desktop unit + 8 integration, 1 ignored, plus the vault crate's 143 — all green), `npm run format:check`/`lint`/`type`/`test` (53 tests). **The fix pass introduced one new blocker, E21.** Making `set_meetings_root` return before the sidecar resolves (the E17 fix) was correct for the UI, but the *registry's* meetings-root is only swapped inside `apply_resolved_service`, which now runs in the background task afterwards. Between the operator picking their folder and F2's ready line (up to `READY_TIMEOUT` = 30 s, and `uv run` resolving a Python environment on first launch after install is exactly that case), the UI accepts drops while the job pipeline is still filing into the *previous* root — on first run, `%APPDATA%\com.transcriber.desktop`. F1's transfer moves and deletes the original, so this silently relocates the operator's recording outside the vault they just chose, marks the job `done`, and then refuses Reveal because the recorded path fails containment against the current root. E8 is unchanged: the GUI half of the smoke checklist still needs a human.

## Findings

### E1 [blocker] [correctness] [status: fixed]

- **Where**: `apps/desktop/src-tauri/src/commands.rs:221-251` (`reveal_command_line` / `run_reveal_command`), `:625-664` (`reveal_job_handler`)
- **Spec ref**: FR-15 — acceptance "The reveal control opens the correct folder in Explorer"
- **Verified independently this round, twice over.** The `Vec<String>` + `Command::args` construction is gone: `reveal_command_line` returns the single string `/select,"<path>"` and `run_reveal_command` appends it via `std::os::windows::process::CommandExt::raw_arg`, which puts it on the command line untouched. `reveal_job_handler` still strips the `\\?\` verbatim prefix before handing the path over (round-1 half). Empirical re-check with a spaces path, run by this evaluator rather than read from the fix narrative: `explorer.exe` started through `ProcessStartInfo.Arguments = '/select,"…\ELS\260812 - Security issue\transcript.json"'` (byte-identical to what `raw_arg` emits) opened a new window whose `LocationURL` was the fixture meeting folder itself — the round-2 failure mode (Documents) did not reproduce. Window closed, fixture removed. The automated regression `run_reveal_command_appends_the_tail_raw_so_the_select_switch_is_not_quoted_with_the_path_e1_regression` pins the distinction at the OS command-line level through `cmd.exe`'s `%CMDCMDLINE%`, which is the only layer at which the round-2 defect was visible. Fixed. The GUI step of clicking Reveal in the running app remains part of E8.

### E2 [major] [correctness] [status: fixed]

Unchanged from round 2. `Shared { root: RwLock, service: RwLock }` + `set_root_and_service` swap in place; jobs enqueued during startup survive resolution, asserted by `apply_resolved_service_preserves_a_job_enqueued_while_the_sidecar_was_still_starting`. (E21 below is about *when* that swap happens, not whether it orphans jobs.)

### E3 [major] [correctness] [status: fixed]

Unchanged. Malformed `config.json` falls back to first-run defaults and surfaces an actionable error instead of aborting before a window exists; verified live against the built binary in round 2.

### E4 [major] [correctness] [status: fixed]

Unchanged. `CollisionOutcome::{DuplicateRedrop, SuffixedFolder}` reach `JobSnapshot.message` and survive `apply_status`.

### E5 [major] [correctness] [status: fixed]

Unchanged. Poll-error-budget exhaustion flips the service banner through `ServiceUnavailableSink`.

### E6 [major] [correctness] [status: fixed]

Unchanged. `record_service_pid` + `taskkill /PID <pid> /T /F` reaps F2's real process tree. Residual (documented in the code): a hard kill of the app still orphans the Python grandchild.

### E7 [major] [spec-drift] [status: fixed]

Unchanged, and re-verified: `npm run format:check` reports "All matched files use Prettier code style!" and all eight gates are green, so `README.md`'s FR-19 claim matches reality.

### E8 [major] [spec-drift] [status: open]

- **Where**: `docs/manual-smoke-checklist.md:53-60`
- **Spec ref**: FR-21 (must) — "executed before the feature is called done"
- **Actual**: unchanged in substance. Steps 0 and 1 have real released-binary evidence. Step 6 (Reveal) now has real released-*code* evidence — the mechanism is proven against a folder with a space in its name — but not the in-app click. Steps 2, 3, 4, 5 and 7 are still **Pending**: they need a real folder-picker click, a real OS drag-drop and a real restart, none of which this environment can synthesize (no `tauri-driver`/WebDriver, FR-20 parked). The checklist records this honestly rather than ticking boxes, which is the right call, but FR-21 is not satisfied until a human runs it.
- **Suggested fix**: one human operator pass over steps 2–7 against `target/release/transcriber-desktop.exe`, recording date and observation per step. Note that such a pass is also the cheapest way to catch E21, which no unit test in the suite is positioned to see.

### E9 [minor] [correctness] [status: fixed]

Unchanged. `useJobs.enqueue` upserts via `insertIfUnknown`.

### E10 [minor] [spec-drift] [status: accepted]

Unchanged and accepted: the extension allowlist mirrors `vault::media` (`.avi` in, `.aac` out), pinned by `supported_extensions_mirrors_vaults_real_allowlist`. Amend FR-6's text, not the code.

### E11 [minor] [performance] [status: fixed]

Unchanged. `status()` applies `min(client timeout, 1.2 s)` per request. Residual arithmetic (1.5 s poll + 1.2 s worst case = 2.7 s vs NFR-4's 2 s) only reachable against a nearly-hung-but-reachable service.

### E12 [minor] [performance] [status: fixed]

Unchanged. Reveal runs under `spawn_blocking`; a join failure maps to `AppError::internal`.

### E13 [minor] [security] [status: fixed]

Unchanged. CSP set in `tauri.conf.json`, verified in round 2 against the packaged build's rendered UI.

### E14 [minor] [correctness] [status: fixed]

Unchanged. `JobRow` falls back `transcript_path ?? source_dest ?? meeting_dir`, mirroring `reveal_job_handler`.

### E15 [minor] [improvement] [status: fixed]

Unchanged. The multi-file test asserts submission *order*, not just count.

### E16 [major] [correctness] [status: fixed]

- **Where**: `apps/desktop/src-tauri/src/config.rs:131-146` (`load`), `:349-368` (test), `docs/config-contract.md:18-23`
- **Verified**: `load` now does `raw.strip_prefix('\u{feff}')` before `serde_json::from_str`, `a_utf8_bom_before_the_json_is_tolerated_e16_regression` writes real BOM bytes to a real file and asserts the load succeeds, and the contract document states "**Encoding: UTF-8, BOM optional**" with the installer-side rationale (`Set-Content -Encoding UTF8`, `Out-File`, .NET `File.WriteAllText(…, Encoding.UTF8)`, NSIS/WiX helpers). F4 can now write the file the way Windows tooling writes files by default. Fixed.

### E17 [major] [correctness] [status: fixed]

- **Where**: `apps/desktop/src-tauri/src/commands.rs:490-512` (`set_meetings_root_handler`), `:516-540` (`resolve_and_apply_meetings_root_service`), `:686-700` (the `#[tauri::command]` wrapper's background task)
- **Verified**: the handler now persists the setting and returns `SettingsResponse` immediately; resolving/(re)starting the sidecar moved into a spawned task driven by the command wrapper, reporting through the existing `service://status` event. `set_meetings_root_returns_before_resolving_the_sidecar_e17_regression` asserts the sidecar controller has recorded **zero** calls at return time, and two further tests cover the background half (restart with the new `TRANSCRIBER_ALLOWED_ROOTS`; no spawn when a `base_url` is configured). The up-to-30 s dead window after the folder pick is gone. Fixed — but see **E21**, which this change created.

### E18 [minor] [correctness] [status: open]

- **Where**: `apps/desktop/src-tauri/src/lib.rs:143-168` (startup task), `apps/desktop/src-tauri/src/commands.rs:686-700` (settings task)
- **Spec ref**: FR-9 / FR-11 / FR-16
- **Half fixed.** The specific defect is gone: the startup task re-reads `state.settings` *after* the ready-line await and derives its root from that, so it can no longer install `%APPDATA%\com.transcriber.desktop` as the registry root over a folder the operator picked meanwhile. The comment at `lib.rs:150-160` says so and is accurate. What remains — and is explicitly acknowledged there — is ordering: two independent background tasks (startup, and one per `set_meetings_root`) can call `apply_resolved_service` in any order, so a late-finishing loser can overwrite the winner's `TranscriptionService` with an `UnavailableTranscriptionService` (typically when its own child was killed by the other's `restart`), leaving the session showing "service unavailable" with nothing scheduled to re-resolve. The most likely interleaving is benign (the killed task fails fast, before the survivor's ready line), which is why this stays minor. No test covers either half — `setup_app_state` is not unit-testable without a Tauri runtime.
- **Suggested fix**: a generation counter on `AppState` incremented by each resolution request, with `apply_resolved_service` ignoring a result from a stale generation.

### E19 [minor] [improvement] [status: fixed]

- **Where**: `apps/desktop/src/components/ServiceBanner.tsx:26-33`
- **Verified**: the unavailable banner now reads "Ingest keeps working; recordings already filed are safe. Re-drop them once the service is back." — true and actionable, and it no longer promises an automatic resume that no code performs. Fixed.

### E20 [minor] [correctness] [status: fixed]

- **Where**: `apps/desktop/src-tauri/src/commands.rs:290-300` (`AppState::fake_mode`), `:522-532`, `lib.rs:134-138`
- **Verified**: `resolve_and_apply_meetings_root_service` short-circuits in fake mode, keeping the installed `FakeService` while still moving the registry root, and `resolve_and_apply_meetings_root_service_keeps_the_fake_service_in_fake_mode_e20_regression` asserts both halves (no sidecar call; a subsequently enqueued file lands under the new root). FR-12's "runs the full drop→status→done flow with no service process running" now survives a settings change. Fixed.

### E21 [blocker] [correctness] [status: fixed]

- **Where**: `apps/desktop/src-tauri/src/commands.rs:490-512` (`set_meetings_root_handler` — persists settings, never touches the registry), `:432-446` (`apply_resolved_service` — the **only** caller of `JobRegistry::set_root_and_service`), `:686-700` (the swap now happens in a background task), `jobs.rs:293` (`let root = shared.root.read().await.clone()`), `src/App.tsx:138-147` + `:167-172` (drop zone shown as soon as `setMeetingsRoot` resolves)
- **Spec ref**: FR-9 (must) — acceptance "A correctly named recording ends up at `<root>/ELS/260812 - Security issue/source.mp4`"; FR-11 (must) — every write asserted inside the *configured* meetings-root; FR-18 — "becomes functional as soon as they do"
- **Expected**: once `set_meetings_root` returns, a dropped recording is filed under the folder the operator just chose.
- **Actual**: introduced by the E17 fix. `set_meetings_root_handler` writes `config.json` and returns; it does **not** update the job registry's root. That happens only in `apply_resolved_service`, which the command wrapper now runs in a spawned task *after* `resolve_service` — for the default first-run `SidecarPlan::Spawn` that awaits F2's ready line under `READY_TIMEOUT` (30 s), and the plan's own risk register names `uv run --directory …` resolving a Python environment on first use as the slow case. Meanwhile `App.tsx` has already left the first-run state, so drops are accepted; `enqueue_paths_handler` only checks that `settings.meetings_root.is_some()`, and the worker then calls `ingest::ingest(&stale_root, …)`. On first run the stale root is `state.config_dir` = `%APPDATA%\com.transcriber.desktop`, which `config::save` has just created, so `Vault::open` succeeds and `layout::init` builds a second vault there. Consequences, all silent: F1's transfer **moves** the recording (`successful_transfer_moves_bytes_and_removes_original`), so the original is deleted from wherever the operator dragged it from; `ingest`'s own `ensure_inside` check passes because it is measured against the same stale root, so FR-11's guarantee is satisfied only relative to a root nobody configured; the job reports `done` with a `transcript.json` path under `%APPDATA%`; and `reveal_job_handler`, which validates against the *current* settings root, then refuses with `outside_root` — the operator sees a completed job they cannot open. Changing an already-configured root has the same window with a less severe landing spot (the previous vault). No test can observe this: every existing test calls `resolve_and_apply_meetings_root_service` before enqueuing (see `…_e20_regression`, which enqueues only after the apply).
- **Suggested fix**: swap the registry root synchronously inside `set_meetings_root_handler` (`state.registry.read().await.set_root_and_service(new_root, current_service)` — the service instance is unchanged at that moment), and let the background task continue to own only the sidecar/service half. A regression test that enqueues a file between `set_meetings_root_handler` and `resolve_and_apply_meetings_root_service` and asserts the destination is under the new root fails today and would pass after.

## Coverage matrix

| Requirement | Implemented in | Tested by | Status |
|---|---|---|---|
| FR-1 (app builds/launches) | `Cargo.toml`, `src-tauri/tauri.conf.json`, `apps/desktop/package.json` | `cargo test --workspace`, `vitest run`; release binary launch verified round 2 | ✓ |
| FR-2 (setup doc) | `docs/setup.md`, `apps/desktop/README.md` | read-through; README's QA claims re-verified by running all eight gates this round | ✓ |
| FR-3 (capabilities allowlisted) | `capabilities/default.json` (4 permissions, justified) | review only | ✓ |
| FR-4 (window drag-drop, real paths) | `src/api.ts`, `App.tsx:94-127` | `api.test.ts`, `App.test.tsx`; no `dataTransfer`/`ondrop` anywhere | ✓ |
| FR-5 (three drop-zone states) | `components/DropZone.tsx` | `DropZone.test.tsx` (6), `App.test.tsx` | ✓ |
| FR-6 (extension allowlist, per-file rejection) | `paths.rs`, `ingest.rs:84`, `jobs.rs:294-304` | `paths.rs`, `ingest.rs`, `jobs.rs`, `e2e_flow.rs` | ✓ with accepted drift (E10) |
| FR-7 (Choose file…) | `api.ts`, `DropZone.tsx` | `api.test.ts`, `App.test.tsx` | ✓ |
| FR-8 (multi-file, sequential, one row each) | `jobs.rs` worker loop + mpsc | `jobs.rs` (non-overlap **and** order), `useJobs.test.ts` | ✓ |
| FR-9 (F1 ingest, unsorted, collision reported) | `ingest.rs`, `jobs.rs:312-318`, `collision_message` | `ingest.rs` (17), `jobs.rs`, `e2e_flow.rs` | **gap — files can land outside the configured root right after it is set (E21)** |
| FR-10 (ingest off the UI thread) | `ingest.rs:67` `spawn_blocking` | `jobs.rs` heartbeat, `e2e_flow.rs` 2 GiB (`#[ignore]`) | ✓ |
| FR-11 (canonicalize + containment in Rust) | `paths.rs:194` `ensure_inside` | `paths.rs` (13), `e2e_flow.rs`, `commands.rs` | gap — the check is correct but is applied against a stale root during E21's window |
| FR-12 (service seam, fake, base URL from settings) | `service/{mod,fake,http}.rs`, `AppState::fake_mode` | `fake.rs`, `http.rs` (wiremock), `commands.rs` incl. the E20 regression | ✓ |
| FR-13 (reachability surfaced, ingest survives) | `commands.rs`, `UnavailableTranscriptionService`, `jobs.rs:385-407`, `ServiceBanner.tsx` | `jobs.rs`, `e2e_flow.rs`, `commands.rs`, `ServiceBanner.test.tsx` (4) | ✓ |
| FR-14 (live status, verbatim failure message) | `jobs.rs:364-445`, `useJobs.ts` | `jobs.rs`, `useJobs.test.ts`, `JobRow.test.tsx` (14) | ✓ |
| FR-15 (transcript path + validated reveal) | `commands.rs:221-251`, `:625-664`, `JobRow.tsx` | `commands.rs` (incl. the raw-command-line regression), `e2e_flow.rs`; Explorer behaviour re-verified empirically this round | ✓ (in-app click still part of E8) |
| FR-16 (persisted, user-visible root) | `config.rs`, `commands.rs:490`, `SettingsBar.tsx` | `config.rs` (9 cases incl. BOM), `commands.rs`, `App.test.tsx` | gap — the setting persists, but the pipeline does not follow it immediately (E21) |
| FR-17 (config.json contract for F4) | `config.rs`, `docs/config-contract.md` | unknown-key round-trip, BOM regression | ✓ |
| FR-18 (first-run refuses drops) | `commands.rs:544-550`, `App.tsx:167-172`, `FirstRun.tsx` | `commands.rs`, `App.test.tsx`, `FirstRun.test.tsx` | gap — becomes functional immediately, but against the wrong root (E21) |
| FR-19 (format/lint/type/test) | npm scripts + cargo equivalents, `README.md` | all eight run this round: **green** (`fmt`, `clippy -D warnings`, 109+8 Rust tests, 53 UI tests) | ✓ |
| FR-20 (E2E harness, should) | — | — | parked per plan T15 |
| FR-21 (manual smoke checklist executed) | `docs/manual-smoke-checklist.md` | steps 0, 1 executed; 6 mechanism-proven; 2–5, 7 pending | gap (E8) |
| NFR-1 (<300 ms ack) | `jobs.rs` enqueue-before-IO | `jobs.rs` | ✓ |
| NFR-2 (responsive during 2 GB ingest) | `spawn_blocking` (ingest and reveal) | `e2e_flow.rs`, `jobs.rs` | ✓ (automated substitute) |
| NFR-3 (<3 s cold start) | `lib.rs:143` background sidecar spawn | none | gap — no measurement recorded (E8) |
| NFR-4 (<2 s staleness) | `jobs.rs` `POLL_INTERVAL`, `http.rs` status timeout | `jobs.rs`, `http.rs` slow-response case | ✓ (see E11 residual) |
| NFR-5 (loopback only) | `http.rs` + `reqwest` without TLS | non-loopback/https rejection cases | ✓ |
| NFR-6 (no panicking command handlers) | typed `AppError`; only two `expect`s outside tests, both on `StdMutex` poisoning | `error.rs`, `commands.rs`, `sidecar.rs`, live malformed-config run | ✓ |
| NFR-7 (Windows-only, no hard-coded separators) | `paths.rs` uses `Component`/`Prefix`; `CommandExt` import is Windows-only by construction, in the same module that already hard-codes `explorer.exe` | `ingest.rs` uses `MAIN_SEPARATOR_STR` | ✓ |
| NFR-8 (fixed identity) | `tauri.conf.json`, `docs/config-contract.md` | none automated | ✓ |

## Positive notes

- `paths.rs` is still the strongest part of the diff and was again left alone.
- The E1 fix is the right one rather than the convenient one: the defect lived one layer below the previous assertion, and the fix moved both the construction (`reveal_command_line`) and the launch (`run_reveal_command`) to a level where a test can actually see the command line the OS receives, via `cmd.exe`'s `%CMDCMDLINE%`.
- E16's fix is three characters of tolerance plus a test written against real BOM bytes on a real file, and the contract doc was updated in the same pass so F4 inherits the guarantee rather than the folklore.
- E20's fix put the fake-mode decision on `AppState` instead of re-deriving it, and its test asserts the *non-obvious* half (the registry root must still move even when the service substitution is skipped).
- E19 replaced a comforting falsehood with an accurate instruction — the kind of fix that is easy to skip because nothing fails without it.
- The smoke checklist held the line for a third round: steps 2–5 and 7 are still marked Pending with the reason, and step 6's entry distinguishes precisely between "mechanism proven against real Explorer" and "clicked in the running app".
- E18's partial fix carries an honest comment about what it does *not* solve, which is how the residual ordering race stayed visible instead of being buried.
