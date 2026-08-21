# Manual smoke checklist (FR-21)

This is the required evidence path for FR-21/FR-20: `tauri-driver` E2E is
parked (T15) as unproven on this host, so this checklist — executed against
the real, built app — is the committed substitute. Each step below is
followed by its **execution log**: date, who/what produced the evidence, and
the observed result. A step whose box is unchecked has not yet been executed
against the real app and is marked as such honestly, rather than assumed
passing.

## Preconditions

- A clean or reset `%APPDATA%\com.transcriber.desktop\config.json` (delete
  it, or point at a scratch `%APPDATA%` equivalent) so the run starts from
  first-run.
- `npm run tauri dev` (or a built binary) from `apps/desktop/`.
- Test files: `ELS - 260812 - Security issue.mp4` (correctly named),
  `random meeting.mp4` (badly named — no project code/date/title pattern),
  `notes.txt` (unsupported extension). Any small dummy media file works;
  F1's naming/routing logic does not inspect file contents.

## Checklist

- [x] **1. Launch the app.** Expected: the `Transcriber` window opens; with
      no `config.json` present, the first-run folder-picker state is shown
      (no drop zone, drops refused) rather than a crash or a silent default.
- [ ] **2. Set the meetings-root** via the first-run/Change… folder picker.
      Expected: the picker persists the chosen folder; the UI switches from
      the first-run state to the normal drop-zone + job-list view.
- [ ] **3. Drop `ELS - 260812 - Security issue.mp4`.** Expected: a job row
      appears within ~300 ms of the drop (before ingest completes); it
      transitions `pending → ingesting → queued → running → done` without
      user action; on `done` it shows the resolved `transcript.json` path
      under `<meetings-root>\ELS\260812 - Security issue\`.
- [ ] **4. Drop `random meeting.mp4`** (a badly-named recording). Expected:
      the job is still accepted and files under `<meetings-root>\unsorted\`
      per F1's rule, with `classification: unsorted` reflected in the row.
- [ ] **5. Drop `notes.txt`.** Expected: a rejection naming the file and its
      unsupported extension; no write occurs anywhere under the
      meetings-root for this file.
- [ ] **6. Reveal** the completed job from step 3. Expected: Windows
      Explorer opens with the meeting folder (and, per FR-15's
      implementation, the file selected), and no other window or path is
      opened.
- [ ] **7. Restart the app** (close and relaunch `tauri dev`/the binary).
      Expected: the meetings-root chosen in step 2 is still shown — no
      re-prompt for first-run, no reset to a default.

## Execution log

| Step | Status | Evidence | Date | Notes |
|---|---|---|---|---|
| 0. `npm run tauri build` produces a binary that launches (FR-1's second acceptance criterion) | **Done** | Eval-fix pass (round 1, E8) ran `npm run tauri build -- --no-bundle` from a clean `apps/desktop/`, producing `target/release/transcriber-desktop.exe` (`Finished \`release\` profile [optimized] target(s) in 1m 48s`). The binary was launched directly (not `tauri dev`) with no pre-existing `%APPDATA%\com.transcriber.desktop\config.json`; `Get-Process transcriber-desktop` showed `MainWindowTitle: Transcriber`, `Responding: True`, and a screenshot confirmed the first-run state (`Choose a meetings folder to begin.` / `Choose folder…`) rendering correctly. | 2026-08-21 | `--no-bundle` skips MSI/NSIS installer packaging (not installed on this host, and F4's concern, not this feature's); the release binary itself is what FR-1 requires and is what was launched. |
| 1. Launch → first-run state | **Done** | Originally: T11 launched via `npm run tauri dev`. Re-confirmed in the same eval-fix pass above against the actual **release build** (not `tauri dev`), including verifying the real F2 sidecar spawns underneath it: `Get-CimInstance Win32_Process` showed the process tree `transcriber-desktop.exe → uv.exe → transcription-service.exe → python.exe → python.exe`, confirming the sidecar-spawns-a-real-grandchild-process shape E6 (process-tree-on-exit) depends on. | 2026-08-21 | — |
| 2. Set the meetings-root | **Pending** | Not automatable in this environment: no `tauri-driver`/WebDriver client is installed (FR-20 is parked per the plan) and no computer-use/UI-automation tool is available to this agent to click the folder picker and select a real folder. | — | Outstanding for a human operator or a future `tauri-driver` E2E pass. |
| 3. Drop `ELS - 260812 - Security issue.mp4` | **Pending** | Same automation gap as step 2 — a real OS drag-drop cannot be synthesized without `tauri-driver`. Covered indirectly: `apps/desktop/src-tauri/tests/e2e_flow.rs` (fake service) and, since this eval-fix pass, `commands::tests::apply_resolved_service_preserves_a_job_enqueued_while_the_sidecar_was_still_starting` (jobs.rs), which is the automated equivalent of dropping a file during the exact sidecar-starting window this step exercises. | — | Outstanding for post-merge/F4 execution with a real model configured. |
| 4. Drop `random meeting.mp4` → `unsorted/` | **Pending** | Same automation gap. Covered by automated integration coverage over the fake service (`ingest.rs`, `e2e_flow.rs`). | — | Same real-F2/real-GUI caveat as step 3. |
| 5. Drop `notes.txt` → rejection | **Pending** | Same automation gap. Covered by automated coverage (`paths::`, `ingest::`, `e2e_flow.rs`). | — | No real-F2 dependency, but still requires a real drag-drop gesture this agent cannot synthesize. |
| 6. Reveal opens the correct folder | **Done (automated substitute), GUI confirmation still pending** | Round 2 found the round-1 fix insufficient: `reveal_args` built `/select,<path>` as a `Vec<String>` fed through `std::process::Command::args`, which quotes any argument containing a space as one token — every F1 meeting folder is `<date> - <Title>`, so the emitted command line always wrapped the switch and path together (`"/select,C:\...\transcript.json"`), which Explorer parses as unrecognized and opens Documents instead of the target (E1, round 2). Fixed by restructuring the reveal path around `reveal_command_line` (a single raw string) and `run_reveal_command`, which now appends it via `std::os::windows::process::CommandExt::raw_arg` instead of `Command::args`, so only the path is quoted and the switch stays bare (`/select,"C:\...\transcript.json"`). Verified twice against a real fixture folder named `260812 - Security issue` (a space in the name, matching F1's real naming): (1) built and ran a throwaway example binary (`apps/desktop/src-tauri/examples/reveal_probe.rs`, deleted after use) linking the crate's actual `reveal_command_line`/`run_reveal_command`/`EXPLORER_PROGRAM` — not a hand-copied reproduction — against the fixture; `Shell.Application.Windows()` showed the opened Explorer window's `LocationURL` pointing at the fixture folder itself, not Documents or any parent; the window was then closed. (2) `commands.rs::run_reveal_command_appends_the_tail_raw_so_the_select_switch_is_not_quoted_with_the_path_e1_regression` pins the same distinction at the OS command-line level (via `cmd.exe`'s own `%CMDCMDLINE%`) as an automated regression test. | 2026-08-21 | The *mechanism* FR-15 depends on (Explorer opening the right folder with the file selected) is now demonstrated against a real folder with a space in its name — the exact shape that broke it. What remains pending is only the GUI step of clicking "Reveal" in the running app after a real drag-drop, which still requires a human operator or `tauri-driver` (see step 3's caveat). |
| 7. Restart persists meetings-root | **Pending; re-confirmed adjacent evidence** | Same automation gap — no way to restart a real GUI session unattended in this environment beyond the single-launch verification in step 0/1. Covered at the settings-module level by `config.rs`'s `save_then_load_preserves_the_value_across_a_simulated_restart` (now also covering a BOM'd `config.json`, E16). Additionally re-verified this pass: launched the freshly built release binary with a real `%APPDATA%\com.transcriber.desktop\config.json` pointing at a scratch meetings-root (written directly, simulating a prior session having persisted it), and read the window's UIA tree — it rendered the configured root's exact path under "Settings" and the drop-zone/job-list view, not first-run, confirming a *previously persisted* root is honored on launch with the current code. This is the "restart loads the persisted value" half of step 7; it is not the same as this app instance itself performing the round-trip persist-then-restart end to end, which still needs a human operator to drive the folder picker (step 2) first. | Outstanding for post-merge/F4 execution. |

**Honest summary as of this eval-fix pass (2026-08-21, round 2):** steps 0
and 1 continue to have real, released-binary evidence. Step 6 now has real,
released-code evidence too: with E1 actually fixed (raw command-line
construction via `CommandExt::raw_arg` instead of `Command::args`), Explorer
was confirmed opening the correct folder — including the space-in-name case
that broke the round-1 fix — via both a live Explorer-window check and an
automated regression test at the OS command-line level. Step 7 gained
adjacent evidence (a persisted root loads correctly into the normal
drop-zone view on a fresh launch of the rebuilt binary). Steps 2–5 remain
**pending**: they require a real drag-drop gesture or folder-picker click,
and this environment has neither `tauri-driver`/WebDriver (FR-20 is parked)
nor a computer-use/UI-automation tool this agent can drive against a native
Win32/WebView2 window or its native folder-picker common dialog. This is
recorded honestly as a gap rather than assumed passing.
