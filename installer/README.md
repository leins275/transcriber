# installer/ — NSIS installer hooks (T7)

This directory holds `installer_hooks.nsh`, the single file Tauri 2's
generated NSIS installer/uninstaller `!include`s and calls into via its four
hook macros. It is referenced from
`apps/desktop/src-tauri/tauri.conf.json`'s
`bundle.windows.nsis.installerHooks` (T6) as:

```json
"installerHooks": "../../../installer/installer_hooks.nsh"
```

(relative to `tauri.conf.json` itself: `src-tauri/` -> `desktop/` -> `apps/`
-> repo root -> `installer/installer_hooks.nsh`).

**Compiled and run for real (T14 "Second pass").** Tauri's vendored NSIS
toolchain compiles this file as part of `npm --prefix apps/desktop run
tauri build` — an early real attempt hit a 32-bit `makensis` limit against
the ~2.33 GiB `--extra cuda` payload (see "Fixed: the CUDA payload that
broke `makensis`" below), but the default, non-CUDA bake compiles cleanly
and has produced a real `dist/Transcriber_<version>_x64-setup.exe`, which
was then installed, upgraded, silently installed and uninstalled on the
operator's machine (`docs/verification-installer.md`'s "Second pass"
records the full run). Everything in this file also remains checked for
shape and internal consistency by `scripts/tests/test_installer_hooks.py`'s
static assertions, which is still the only thing exercised in CI (no
`makensis` there).

## The four hooks

| Macro | Runs | What it does here |
|---|---|---|
| `NSIS_HOOK_PREINSTALL` | before files are copied into `$INSTDIR` | reserved (no-op) |
| `NSIS_HOOK_POSTINSTALL` | after files are copied | creates `models\`, `logs\`, `data\` (FR-8); parses `/VAULT=` in silent mode and writes `config.json` (FR-18) |
| `NSIS_HOOK_PREUNINSTALL` | before the core uninstall Section removes files | decides upgrade-vs-real-uninstall, asks about the model directory, relocates `models\`/`logs\`/`data\` out of `$INSTDIR` |
| `NSIS_HOOK_POSTUNINSTALL` | after the core uninstall Section has run | restores (or, on explicit opt-in, discards) the relocated folders |

## The vault-safety invariant (FR-14)

The meetings vault is a user-chosen folder that is validated at
selection time to be **outside** `$INSTDIR` (FR-10). Nothing in this file
ever references the vault path, `meetings_root`, or any path supplied by
the user as a delete target — `scripts/tests/test_installer_hooks.py`
asserts this by grepping the whole file for those substrings, and by
checking that every `Delete`/`RMDir` line's argument is rooted at
`$INSTDIR` or the app's own `%APPDATA%\<identifier>\` folder. The vault is
safe by construction: this installer's code paths simply never reach it.

## R1 — the 3 GB `models\` directory vs. the uninstaller

Tauri's generated uninstaller removes the install directory; an upgrade
runs the *old* version's uninstaller first, before the new version's files
are copied in. Whether that core uninstall Section recursively wipes
`$INSTDIR` or only deletes the files it explicitly installed is not
something this implementer could verify without a real build — `makensis`
is absent here, and T7's own **Done when** requires an *empirical*
double-install and uninstall rather than a reading of the template
(R1 in `plan.md`).

The mitigation, defensive against either behaviour: `NSIS_HOOK_PREUNINSTALL`
always relocates `$INSTDIR\models`, `$INSTDIR\logs`, `$INSTDIR\data` and
`$INSTDIR\runtime` out to `%APPDATA%\<identifier>\_uninstall_tmp\`
**before** the core uninstall Section runs, regardless of upgrade vs. real
uninstall (`runtime\` is the first-run CUDA runtime download's
destination — see "CUDA runtime is a first-run download, not baked"
below — added once that download existed, so an upgrade never forces a
second ~1.4 GB re-fetch). `logs\` and `data\` are then always restored in
`NSIS_HOOK_POSTUNINSTALL`. `models\` and `runtime\` are restored too,
*unless* this was a genuine, interactive uninstall and the user explicitly
answered "Yes, delete the model" — tracked across the two hooks in `$R7`
and applied to both directories together (they are both large, re-fetchable
payloads).

Upgrade vs. real uninstall is distinguished with NSIS's built-in
`IfSilent`: the automatic "replace the old version" step that a new
install's upgrade path runs against the old uninstaller is the standard
NSIS convention for a silent (`/S`) chained uninstall. No user is present
during that step, so the silent branch never prompts and always preserves
everything. A real, interactive uninstall (Control Panel, or the
installer's own uninstall entry point) asks with a `MB_YESNO` message box,
defaulting to **No** (keep) via `MB_DEFBUTTON2`, so nothing is ever
silently orphaned with no way to find it.

### What T14 proved empirically (Second pass)

This design started as a considered, documented mitigation for a risk that
could not be resolved by reading Tauri's template alone. It has since been
run for real, end to end, against the fixed (non-CUDA-baked) build — full
evidence for each item is in `docs/verification-installer.md`'s "Second
pass" section and `docs/manual-smoke-checklist.md`'s installer section:

1. **Compiles.** `installer_hooks.nsh` has valid NSIS syntax under the real
   `makensis` Tauri's bundler vendors — confirmed by a real
   `dist/Transcriber_<version>_x64-setup.exe` being produced.
2. **`IfSilent` distinguishes the upgrade case.** Confirmed: a same-version
   silent reinstall (the exact `IfSilent` path an upgrade also takes)
   preserved a sentinel file with no prompt.
3. **Double-install preserves the model and the sentinel.** Confirmed — see
   item 2; the model directory and `config.json` both survived.
4. **A real uninstall's vault is untouched.** Confirmed: a 3-file vault was
   byte-for-byte identical before and after two silent uninstalls. The
   interactive Yes/No branch specifically is still untested (no UI
   automation available on this machine).
5. **The explicit model choice branches correctly both ways** — exercised
   via the silent/`IfSilent` path (always "keep"); the interactive
   `MB_YESNO` prompt itself is the same untested branch as item 4.
6. **Silent install with `/VAULT=`.** Confirmed for real, after fixing a
   JSON-escaping defect the first real run surfaced (backslashes in the
   vault path were written unescaped — see
   `docs/verification-installer.md`'s "Second pass" for the fix): `setup.exe
   /S /D=<dir> /VAULT=<path>` completes with no UI and produces valid,
   correctly-resolving `config.json`.

## Silent-mode arguments (FR-18)

```
setup.exe /S /D=C:\Users\<user>\AppData\Local\Programs\Transcriber /VAULT=D:\Meetings
```

- `/D=<dir>` is NSIS's own built-in install-directory flag: it must be the
  **last** argument, unquoted, and is parsed by NSIS itself before any hook
  runs — nothing in this file handles it.
- `/VAULT=<path>` is this feature's own option, parsed in
  `NSIS_HOOK_POSTINSTALL` via `${GetOptions}`. It is validated the same way
  the in-app vault picker validates a path (FR-10): non-empty, creatable,
  and not inside `$INSTDIR`. A valid path is written to
  `%APPDATA%\<identifier>\config.json` with `schema_version: 1` and
  `meetings_root` set, matching F3's schema
  (`apps/desktop/src-tauri/src/config.rs`) so the app reads exactly what a
  silent install produced. An invalid path is rejected with a message box
  and otherwise ignored (the install itself still succeeds).

This is a full overwrite of `config.json`, not a merge — a fresh install
has nothing to merge with, and `/VAULT=` is a developer/silent-mode
convenience for repeated reinstalls (FR-18's "reinstall repeatedly during
development"), not a general-purpose config editor. Plain reinstall/upgrade
*without* `/VAULT=` never touches `config.json` at all (it lives outside
`$INSTDIR`, so nothing in this file's upgrade path can reach it) — that is
what makes FR-16's "vault root survives an upgrade" true unconditionally.

## CUDA runtime is a first-run download, not baked

The installer's payload is CPU-only. `NSIS_HOOK_POSTINSTALL` never touches
CUDA at all; the app fetches the pinned `nvidia-cublas-cu12`/
`nvidia-cudnn-cu12` wheels (~1.4 GB) into `$INSTDIR\runtime\` on first
launch, if an NVIDIA GPU is present (`transcription.cuda_runtime`). The
only thing this file does about that directory is protect it across an
upgrade/uninstall the same way it protects `models\` — see "R1" above.
Nothing in this file drives the download itself; that is entirely the
app's/service's responsibility, not the installer's.

## Fixed: the CUDA payload that broke `makensis`

An early real `tauri build` (T14, first pass) baked `--extra cuda`
straight into the pyenv, producing a ~2.33 GiB uncompressed payload.
Tauri's vendored `makensis.exe` — a 32-bit, non-large-address-aware
compiler — failed against it with `Internal compiler error #12345: error
mmapping datablock`, independent of system RAM or the compression
algorithm chosen (`lzma` vs. `zlib`, both tried); see
`docs/verification-installer.md`'s "Blocker 1" for the full diagnosis.
**Fix:** move the CUDA runtime out of the baked payload and into the
first-run download described above — the default, non-CUDA bake (~414 MB)
compiles cleanly. `docs/verification-installer.md`'s "Second pass" section
records the real, working build this produced and the full manual smoke
checklist executed against it (install, upgrade, silent `/VAULT=`,
uninstall vault-hash comparison) — that is the current, accurate state of
this file's own verification, superseding every "not yet proven" caveat
above.
