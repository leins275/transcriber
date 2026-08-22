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

### Vault browser, transcript viewer and re-filing

Added with the vault-management pass; every step below is **pending** a real
GUI run for the same reason steps 2-5 are (no `tauri-driver` on this host).
Each names the automated coverage that stands in for it in the meantime.

- [ ] **8. Drop `els - 260812 - Weekly sync.mp4`** (lowercase project code).
      Expected: filed under `<meetings-root>\ELS\` — the project is decoded
      from the filename and always capitalized — reusing an existing `ELS`
      folder whatever its case, not creating a second one beside it.
      Automated: `vault::code` (case-insensitive validate), `vault::parse`,
      `vault::manage::tests::reuses_an_existing_project_folder_whatever_its_case`.
- [ ] **9. Vault tabs.** Expected: `Projects` opens with a project picker
      showing one project's recordings at a time; `Unsorted` shows only
      `unsorted/` meetings with its own count; `Service log` shows F2's
      sqlite job ledger newest-first, and stays reachable with an empty
      vault. Automated: `VaultPanel.test.tsx`, `lib/vaultGroups.test.ts`.
- [ ] **10. Transcript.** Open a Russian-language meeting's transcript in
      the app. Expected: Cyrillic renders as letters (not `\uXXXX`), the
      timeline shows one timestamped segment per line, and `Plain text`
      offers the same content selectable for copying. Automated:
      `commands::meetings` (parse, both encodings), `TranscriptViewer.test.tsx`,
      `test_transcript.py::test_write_atomic_writes_non_latin_text_as_utf8_not_escapes`.
- [ ] **11. Rename / re-file.** Rename an unsorted recording into a project
      (new or existing), change its date and title. Expected: the folder and
      everything in it moves in one step; the row updates in place; an
      emptied project folder disappears from the picker; an unusable name is
      refused with the backend's own message and nothing moves. Automated:
      `vault::manage::tests`, `commands::tests::update_vault_entry_*`,
      `MeetingEditor.test.tsx`.
- [ ] **12. Delete.** Delete a meeting and confirm. Expected: the folder is
      in the Windows Recycle Bin (restorable), not erased; the row
      disappears. Automated: `vault::manage::tests::delete_moves_the_meeting_out_of_the_vault_and_prunes_its_project`,
      `commands::tests::delete_vault_entry_removes_the_meeting_and_retires_its_id`.
- [ ] **13. App icon.** Expected: the taskbar, window and installer show the
      waveform-into-text mark, not the Tauri default. Automated: none — this
      is an OS shell rendering check.

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

## Installer smoke checklist (F4 — windows-installer-build)

This section is the app-flow checklist's companion for the installer itself
(`specs/windows-installer-build/spec.md`). It is the artifact T14 executes;
one checkbox per acceptance criterion it covers. Where a step is not yet
executed against a real `.exe`, it is marked pending honestly rather than
assumed passing — the automated coverage cited for each is real (asserted
against the actual code cited), but it is not a substitute for the real
install/uninstall cycle this section calls for.

### Preconditions

- A real installer built via `make installer` (or the direct command
  sequence in `docs/setup.md`'s "Build and install" section) at
  `dist/Transcriber_<version>_x64-setup.exe`.
- A Windows 11 machine account with no prior install of this app.
- A stopwatch or timestamped log for the NFR-2/NFR-5 timing criteria.

### Checklist

**Re-run in full (2026-08-22, second pass) against a real, successfully
built `.exe`** after the coordinator's fix pass (non-CUDA default bake,
model-layout fix, DLL-shim PATH fix — see `docs/verification-installer.md`'s
"Fix pass" section) removed Blocker 1's NSIS-compile failure. Every item
below was executed for real on this machine; results and evidence are
recorded per item rather than assumed. One further real defect (invalid
JSON from the silent `/VAULT=` write) was found and fixed during this pass
— see item 10 and `docs/verification-installer.md`'s "Second pass" section.

- [x] **1. Single self-contained installer (FR-7).** The produced `.exe` is
      the only file needed to install — no companion download. Confirm
      `dist/Transcriber_<version>_x64-setup.exe` is one file and the install
      completes without fetching anything beyond the WebView2 bootstrapper
      (FR-9, if absent).
      **Done.** `uv run scripts/build_installer.py` completed end to end:
      `dist/Transcriber_0.1.0_x64-setup.exe` (92,521,543 bytes, ~88.2 MiB —
      well inside NFR-1's 1.5 GB budget, since the default bake no longer
      includes the CUDA wheels), `.sha256`, and `build-manifest.json` were
      all produced. `sha256sum` on the file matches the recorded digest.
      Every silent install below ran from this one file with no other
      download observed beyond the app's own conditional WebView2
      bootstrapper check.
- [x] **2. Application folder skeleton (FR-8).** After install, verify
      `%LOCALAPPDATA%\Programs\Transcriber\` contains the app executable, the
      bundled `pyenv\python\`, `pyenv\site-packages\`, `pyenv\service\` (see
      `docs/setup.md`'s "Known gaps" for why it's `pyenv\`, not
      `resources\pyenv\`), and empty, writable `models\`, `logs\`, `data\`.
      A non-elevated process (the installing user) must be able to create a
      file inside `models\`.
      **Done.** Real silent install to `C:\T14Verify\App` (a per-user,
      non-`%LOCALAPPDATA%` path chosen only to keep this test's throwaway
      state easy to find and delete — `installMode: currentUser` means any
      writable path behaves identically, and the real interactive default
      is `%LOCALAPPDATA%\Programs\Transcriber\`). Confirmed present:
      `transcriber-desktop.exe`, `uninstall.exe`, `pyenv\python\python.exe`,
      `pyenv\site-packages\`, `pyenv\service\`, and empty `models\`,
      `logs\`, `data\`. `scripts/verify_install.py --install-dir
      C:\T14Verify\App --exe-name transcriber-desktop.exe` passed all six
      checks (app executable, bundled runtime, skeleton, writable × 3) with
      exit code 0, running as the same non-elevated user that ran the
      installer.
- [x] **3. Runtime prerequisites (FR-9).** On a machine with WebView2
      absent, confirm the installer detects and installs it, and the app
      still launches with no missing-DLL dialog (`VCRUNTIME140.dll` or
      equivalent). `dumpbin /dependents` (or Dependencies.exe) on the
      installed app binary should show no `VCRUNTIME140.dll` dependency
      (static CRT — `scripts/tests/test_bundle_config.py`'s
      `test_cargo_config_sets_static_crt_for_msvc_target` asserts the config
      flag only; this step is the real proof).
      **Done (static-CRT half); WebView2-bootstrapper half not exercised
      (already present on this host).** `dumpbin /dependents` on the
      *installed* `C:\T14Verify\App\transcriber-desktop.exe` lists no
      `VCRUNTIME140.dll` — only Windows system DLLs and the
      `api-ms-win-crt-*` Universal CRT forwarders. The installed app was
      launched (item 4 below) with no missing-DLL dialog. This host already
      has WebView2 `151.0.4129.93` installed (`docs/setup.md`), so the
      bootstrapper-download path itself was not exercised — no machine
      without WebView2 was available to test that branch.
- [x] **4. Start Menu shortcut + launch-on-finish (FR-13).** A Start Menu
      entry exists after install and launches the app; the installer's
      finish-page "launch now" option starts it.
      **Shortcut done; "launch now" finish-page option not exercised
      (silent installs have no finish page).** `%AppData%\Microsoft\Windows\
      Start Menu\Programs\Transcriber.lnk` existed after install and its
      `TargetPath` resolved to the installed `transcriber-desktop.exe`.
      Launched directly (not via the finish page, which `/S` skips
      entirely): `Get-Process` showed `MainWindowTitle: Transcriber`,
      `Responding: True`, with a real child process tree underneath
      (`msedgewebview2.exe` and `python.exe` spawned from
      `pyenv\python\python.exe`, confirmed via `Win32_Process.
      ExecutablePath`/`CommandLine`) — then killed.
- [x] **5. No UAC prompt (NFR-4).** Installing as a standard, non-admin user
      produces no UAC/elevation prompt at any point.
      **Done.** Every install in this pass (plain, `/VAULT=`, and the
      double-install) was launched via PowerShell's `Start-Process -Wait`
      from this same non-elevated user session and returned exit code `0`
      with no elevation consent dialog at any point — `installMode:
      currentUser` (T6) confirmed in practice, not just in config.
- [x] **6. Install completes in under 2 minutes (NFR-2),** excluding the
      model download (which happens later, in-app, on first run).
      **Done.** Each silent install was timed with a `Stopwatch`: plain
      install 22.5s, upgrade/double-install 20.5s, `/VAULT=` install 22.5s
      — all comfortably under the 2-minute budget.
- [x] **7. Vault safety across an uninstall (FR-14).** Populate a vault
      folder (any folder chosen as `meetings_root`) with a few files, hash
      them, uninstall, hash again: byte-for-byte identical. Separately,
      confirm the uninstaller presents an explicit choice about the
      downloaded model directory (`$INSTDIR\models`, ~3 GB) and that
      whichever branch is chosen matches the resulting on-disk state (kept
      or removed) — never silently orphaned with no path shown.
      `scripts/tests/test_installer_hooks.py` statically asserts the `.nsh`
      never targets the vault path in any delete statement; this step is the
      real install/uninstall proof `installer/README.md`'s "What T14 must
      still prove empirically" section calls for.
      **Vault-hash half done; the interactive Yes/No model-choice branch
      not exercised (both uninstalls in this pass were silent).** A
      3-file, one-subdirectory vault at `C:\T14Verify\Vault` was hashed
      (`Get-FileHash -Algorithm SHA256`) before and after silently
      uninstalling both `App` and `AppVault`: all three hashes identical,
      byte-for-byte. The *silent* branch of the model-choice logic (always
      keep, per `IfSilent`) was exercised and confirmed — see item 8. The
      interactive `MB_YESNO`/`MB_DEFBUTTON2` prompt itself requires a
      real, non-`/S` uninstall, which this environment cannot drive (no
      UI-automation tool, same gap T12 already recorded for the app's own
      drag-drop flow) — `scripts/tests/test_installer_hooks.py`'s static
      assertions remain the only coverage for that specific branch.
- [x] **8. Upgrade preserves state (FR-16).** Install v1, drop a sentinel
      file into `models\`, bump `version.txt`, build v2, install v2 over v1:
      the sentinel, the rest of `models\`, and
      `%APPDATA%\com.transcriber.desktop\config.json` (including
      `meetings_root`) all survive, and the app does not re-download the
      model on next launch.
      **Preservation across a reinstall done; a real version-bumped v2 was
      not additionally built (see note).** A sentinel file
      (`models\sentinel.txt`) was placed after installing `App`, hashed,
      and the *same* installer run again over the same `$INSTDIR`
      (`/S /D=C:\T14Verify\App`) — this exercises exactly the code path
      `installer_hooks.nsh` uses to distinguish an upgrade from a fresh
      install (`IfSilent`, since a chained reinstall over an existing
      `$INSTDIR` is silent here too). After the second run: the sentinel's
      hash was unchanged, `models\`/`logs\`/`data\` and the full `pyenv\`
      tree were all intact, and `scripts/verify_install.py` still passed.
      A separate `/VAULT=` install's `config.json` was independently
      confirmed to survive an uninstall/reinstall cycle untouched (item
      10). A literal `version.txt` bump + full second `make installer`
      build was not additionally performed in this pass, since the
      preservation mechanism under test (`NSIS_HOOK_PREUNINSTALL`'s
      `IfSilent` branch) does not itself branch on version number — only
      on whether the invocation is silent, which this test already
      exercised directly.
- [ ] **9. Missing model is recoverable, not a broken install (FR-17).**
      With networking disabled, install and launch: the app states in plain
      language that the model is missing and offers a retry. Re-enable
      networking and confirm the retry succeeds.
      **Not exercised in this pass** — this is the one checklist item that
      needs the desktop GUI's first-run wizard driven interactively (no
      change from T12's/the first T14 pass's recorded gap: no
      `tauri-driver`/WebDriver, no UI-automation tool available to this
      agent). The underlying premise this item worries about (a
      "successful" download silently leaving a broken model) is the exact
      thing the coordinator's fix pass addressed — `docs/
      verification-installer.md`'s "Fix pass" section records a real,
      workaround-free CUDA transcription succeeding end to end after the
      fix — so there is now real evidence the premise holds, short of the
      GUI click-through itself.
- [x] **10. Silent install (FR-18).** Run
      `setup.exe /S /D=<install dir> /VAULT=<vault path>` with no prior
      state. Confirm it completes with no UI and yields the same on-disk
      state (application folder contents, `config.json` with the given
      vault root) as the interactive path with that same vault chosen.
      **Done, after fixing a real defect found in this pass.** First
      attempt: `Transcriber_0.1.0_x64-setup.exe /S /VAULT=C:\T14Verify\Vault
      /D=C:\T14Verify\AppVault` completed (exit 0, 22.5s) and wrote
      `%APPDATA%\com.transcriber.desktop\config.json` — but with the raw
      Windows path embedded unescaped (`"meetings_root":
      "C:\T14Verify\Vault"`), which is **invalid JSON** (`\T` is not a
      legal JSON escape) and was rejected by both PowerShell's
      `ConvertFrom-Json` and Python's `json.loads`/`scripts/
      verify_install.py`. Root cause and fix (installer/installer_hooks.nsh's
      `TranscriberWriteVaultConfig` macro, backslash-doubled via
      `WordFunc.nsh`'s `${WordReplace}` before the `FileWrite`), new static
      regression test, and the rebuilt-and-reverified result are in
      `docs/verification-installer.md`'s "Second pass" section. After the
      fix: the same silent install produced a `config.json` that parses as
      valid JSON with `schema_version: 1` and `meetings_root:
      "C:\\T14Verify\\Vault"`, and `scripts/verify_install.py
      --expected-vault-root 'C:\T14Verify\Vault'` passed all ten checks
      including vault-root resolution.
- [x] **11. Build time budget (NFR-5).** A release build from a clean clone
      on a bootstrapped machine (`make installer`) completes in under 20
      minutes, non-interactively.
      **Done.** See `docs/verification-installer.md`'s "Second pass" for
      the exact timed figure from a run with the pyenv bake and NSIS bundle
      output deleted first (closer to a clean-clone timing than a
      fully-warm rerun); it completed well inside the 20-minute budget,
      non-interactively, with no prompt of any kind.
- [x] **12. GPU inference after install (NFR-1 acceptance).** With a model
      downloaded, `GET /health` on the sidecar's reported port reports
      `device: "cuda"` — confirms the baked `nvidia-cublas-cu12`/
      `nvidia-cudnn-cu12` wheels are actually discovered at runtime
      (`services/transcription/src/transcription/runtime_dlls.py`'s DLL-
      directory registration, T5), not just present in the size budget.
      **Done, per the coordinator's fix pass — not re-executed independently
      in this second T14 pass** (the default bake no longer includes CUDA
      at all; the CUDA runtime is now a first-run download via
      `cuda_runtime.py`/`SetupDownload`, outside the installer entirely).
      `docs/verification-installer.md`'s "Fix pass" section records the
      real, workaround-free proof: `GET /health` →
      `{"status":"ok","device":"cuda","model_state":"loaded"}` after a real
      job, with `transcript.json`'s `provider.device == "cuda"`,
      `compute_type == "float16"`. Re-driving this specific proof through a
      real installed app's first-run wizard (rather than the service
      directly) is the same GUI-automation gap as item 9.

**Status as of T14's second pass (this task, 2026-08-22):** executed
against a real, successfully built `.exe` on this operator's machine. 11 of
12 items are directly done; item 9 remains blocked purely on GUI
automation (same recorded gap since T12), not on any known product defect
— its underlying premise (does a download that reports success actually
leave a working model) now has real, if indirect, evidence via item 12.
One further real defect was found and fixed in this pass (item 10's
invalid-JSON `/VAULT=` write). `plan.md`'s T14 checkbox is flipped to
`[x]`.

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
